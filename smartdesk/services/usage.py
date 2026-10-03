"""Atomic monthly AI-reply usage reservations (Phase 3.3A foundation).

The billable unit is **one AI reply**: one OpenAI-requiring receptionist reply.
Canned/local replies never reserve.  A reservation holds one unit of the
tenant's monthly capacity while the OpenAI call is in flight, then is either
*committed* (call succeeded; token counts recorded) or *released* (call failed
or was abandoned; capacity returned).

This module is NOT wired into WhatsApp or Voice yet, and the legacy
conversation-count limit in ``channels/webhooks.py`` is untouched.

Transaction contract (important)
--------------------------------
Like the other services here, these functions never commit.  The caller owns
the transaction and MUST keep it short -- the intended flow is::

    BEGIN;  reserve_ai_reply(...);  COMMIT        # milliseconds
    ... OpenAI call, NO database transaction open ...
    BEGIN;  commit_reservation(...) | release_reservation(...);  COMMIT

An uncommitted reservation holds a row lock on the tenant's period row, which
makes every other reservation for that tenant wait.  Never hold it open across
a network call.

How atomic reservation works (PostgreSQL)
-----------------------------------------
Capacity is claimed by ONE conditional UPDATE::

    UPDATE tenant_usage_periods
       SET used_units = used_units + :n
     WHERE id = :period
       AND used_units + :n <= (SELECT monthly_conversation_limit
                                 FROM tenants WHERE id = :tenant)

The database decides.  Under PostgreSQL's default READ COMMITTED isolation,
concurrent updates to the same row serialise on its row lock, and a waiting
UPDATE re-evaluates its WHERE clause against the newest committed row version.
So two requests can never both take the last unit; the loser matches zero rows
and is denied.  There is no read-then-compare-in-Python step.  (Under
REPEATABLE READ or SERIALIZABLE the loser would instead get a serialisation
error; the application runs at the default.)

The limit comes from ``Tenant.monthly_conversation_limit`` (no new column).
``used + n <= limit`` can never hold for ``limit <= 0``, so zero and negative
limits mean "no capacity" -- never "unlimited".

Idempotency
-----------
``UNIQUE (tenant_id, kind, idempotency_key)`` is enforced by the database.  The
reservation row is inserted *inside a savepoint together with the capacity
claim*, so if a concurrent retry loses the unique-constraint race the savepoint
rolls back both the insert and the claim, and the existing reservation is
returned instead.  A retry never consumes a second unit.

Row-level security
------------------
Both tables are RLS-protected on ``app.current_tenant_id``.  Per-tenant calls
work under the application's tenant binding.  ``release_stale_reservations``
called with ``tenant_id=None`` sweeps every tenant and therefore needs a role
that bypasses RLS (as platform maintenance does); with an RLS-subject role call
it once per tenant with that tenant bound.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from flask import current_app
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from ai.limits import (
    DEFAULT_USAGE_RESERVATION_STALE_SECONDS,
    USAGE_RESERVATION_STALE_SECONDS_CEILING,
    safe_limit,
)
from smartdesk.extensions import db
from smartdesk.models import Tenant, TenantUsagePeriod, UsageReservation, _uuid, utcnow

logger = logging.getLogger(__name__)

#: The reservation ``kind`` for one OpenAI-requiring receptionist reply.
AI_REPLY_KIND = "ai_reply"

#: Default maximum rows released by one ``release_stale_reservations`` call.
DEFAULT_STALE_BATCH = 1000


class UsageError(Exception):
    """Base class for usage-service errors."""


class TenantNotFound(UsageError, LookupError):
    """The tenant does not exist (or is not visible under RLS)."""


class ReservationNotFound(UsageError, LookupError):
    """No such reservation for this tenant."""


class ReservationStateError(UsageError):
    """An illegal transition, e.g. committing a released reservation."""


class _CapacityExhausted(Exception):
    """Internal: rolls back the reservation savepoint when the limit is hit."""


@dataclass(frozen=True)
class ReservationResult:
    """Outcome of ``reserve_ai_reply``.

    ``allowed`` is True when the caller holds (or already committed) a unit for
    this idempotency key and may proceed to call OpenAI.  ``created`` is True
    only when THIS call consumed a unit.  ``reason`` is one of:

    * ``reserved``          -- new unit reserved
    * ``duplicate``         -- retry of an existing live/committed reservation;
                               no extra unit consumed
    * ``already_released``  -- the key's reservation was released earlier; it no
                               longer holds capacity (``allowed`` is False; use
                               a new key for a new attempt)
    * ``quota_exceeded``    -- no capacity left this period (also: limit <= 0)
    """

    allowed: bool
    reason: str
    reservation: UsageReservation | None = None
    created: bool = False

    @property
    def duplicate(self) -> bool:
        return self.reason in ("duplicate", "already_released")


@dataclass(frozen=True)
class UsageSnapshot:
    """Read-only view of a tenant's usage for one UTC month."""

    tenant_id: str
    period_start: datetime
    period_end: datetime
    configured_limit: int
    used_units: int
    available_units: int


@dataclass(frozen=True)
class StaleReleaseResult:
    released: int
    units: int


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


def _as_utc(moment: datetime) -> datetime:
    """Normalise to aware UTC. Naive values are *defined* to be UTC -- the
    server's local timezone is never consulted."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def usage_period_for(moment: datetime | None = None) -> tuple[datetime, datetime]:
    """Return ``(period_start, period_end)`` for the UTC calendar month holding
    ``moment`` (default: now).  ``period_start`` is inclusive, ``period_end``
    exclusive, e.g. 2026-10-01T00:00Z .. 2026-11-01T00:00Z."""
    moment = _as_utc(moment if moment is not None else utcnow())
    start = datetime(moment.year, moment.month, 1, tzinfo=timezone.utc)
    if moment.month == 12:
        end = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(moment.year, moment.month + 1, 1, tzinfo=timezone.utc)
    return start, end


def _ensure_period(tenant_id: str, start: datetime, end: datetime, now: datetime) -> str:
    """Return the id of the tenant's period row, creating it if needed.

    Race-safe: ``INSERT ... ON CONFLICT DO NOTHING`` on the unique window, then
    SELECT.  Concurrent creators block on the unique index until the first
    commits, so exactly one row ever exists.
    """
    dialect = db.session.get_bind().dialect.name
    values = dict(
        id=_uuid(), tenant_id=tenant_id, period_start=start, period_end=end,
        used_units=0, created_at=now, updated_at=now,
    )
    if dialect in ("postgresql", "sqlite"):
        insert = (postgresql if dialect == "postgresql" else sqlite).insert
        db.session.execute(
            insert(TenantUsagePeriod)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "period_start", "period_end"])
        )
    else:  # pragma: no cover - portable fallback
        try:
            with db.session.begin_nested():
                db.session.execute(sa.insert(TenantUsagePeriod).values(**values))
        except IntegrityError:
            pass
    period_id = db.session.execute(
        sa.select(TenantUsagePeriod.id).where(
            TenantUsagePeriod.tenant_id == tenant_id,
            TenantUsagePeriod.period_start == start,
            TenantUsagePeriod.period_end == end,
        )
    ).scalar_one()
    return str(period_id)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _clean_text(name: str, value: Any, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{name} must be at most {max_len} characters")
    return value


def _clean_units(units: Any) -> int:
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError("units must be a whole number >= 1")
    return units


def _clean_tokens(name: str, value: Any) -> int | None:
    """Token counts are recorded exactly as reported or left None -- never
    estimated or derived."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative whole number or None")
    return value


def stale_after_default() -> timedelta:
    """The configured stale-reservation timeout (``USAGE_RESERVATION_STALE_SECONDS``,
    default 600).  Invalid/zero/negative values fall back to the default."""
    seconds = safe_limit(
        current_app.config.get("USAGE_RESERVATION_STALE_SECONDS"),
        DEFAULT_USAGE_RESERVATION_STALE_SECONDS,
        USAGE_RESERVATION_STALE_SECONDS_CEILING,
        "USAGE_RESERVATION_STALE_SECONDS",
    )
    return timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _tenant_limit(tenant_id: str) -> int:
    limit = db.session.execute(
        sa.select(Tenant.monthly_conversation_limit).where(Tenant.id == tenant_id)
    ).first()
    if limit is None:
        raise TenantNotFound(f"Tenant {tenant_id} not found")
    return limit[0]


def find_reservation(tenant_id: str, kind: str, idempotency_key: str) -> UsageReservation | None:
    """Return the tenant's reservation for ``(kind, idempotency_key)`` or None."""
    return db.session.execute(
        sa.select(UsageReservation)
        .where(
            UsageReservation.tenant_id == str(tenant_id),
            UsageReservation.kind == kind,
            UsageReservation.idempotency_key == idempotency_key,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def get_reservation(tenant_id: str, reservation_id: str) -> UsageReservation | None:
    """Return the tenant's reservation by id (fresh from the database) or None."""
    return db.session.execute(
        sa.select(UsageReservation)
        .where(
            UsageReservation.id == str(reservation_id),
            UsageReservation.tenant_id == str(tenant_id),
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------


def _claim_capacity(tenant_id: str, period_id: str, units: int, now: datetime) -> bool:
    """The atomic step: a single conditional UPDATE.  True iff capacity was taken."""
    limit = (
        sa.select(Tenant.monthly_conversation_limit)
        .where(Tenant.id == tenant_id)
        .scalar_subquery()
    )
    result = db.session.execute(
        sa.update(TenantUsagePeriod)
        .where(
            TenantUsagePeriod.id == period_id,
            TenantUsagePeriod.tenant_id == tenant_id,
            TenantUsagePeriod.used_units + units <= limit,
        )
        .values(used_units=TenantUsagePeriod.used_units + units, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _result_for_existing(reservation: UsageReservation) -> ReservationResult:
    if reservation.status == "released":
        return ReservationResult(False, "already_released", reservation, created=False)
    return ReservationResult(True, "duplicate", reservation, created=False)


def reserve_ai_reply(
    tenant_id: str,
    idempotency_key: str,
    *,
    kind: str = AI_REPLY_KIND,
    units: int = 1,
    now: datetime | None = None,
    metadata: dict | None = None,
) -> ReservationResult:
    """Atomically reserve ``units`` of this month's AI-reply capacity.

    Guarantees ``used_units + units <= tenant.monthly_conversation_limit`` even
    under concurrent callers (see module docstring).  Retrying the same
    ``(tenant, kind, idempotency_key)`` returns the existing reservation and
    consumes nothing.  A denied attempt leaves no row behind, so it can be
    retried later (e.g. after the limit is raised).

    Does not commit -- the caller must commit promptly (see module docstring).
    Raises ``TenantNotFound`` for an unknown tenant and ``ValueError`` for bad
    arguments.
    """
    tenant_id = str(tenant_id)
    idempotency_key = _clean_text("idempotency_key", idempotency_key, 128)
    kind = _clean_text("kind", kind, 40)
    units = _clean_units(units)
    now = _as_utc(now if now is not None else utcnow())

    _tenant_limit(tenant_id)  # raises TenantNotFound; cheap, avoids FK errors

    existing = find_reservation(tenant_id, kind, idempotency_key)
    if existing is not None:
        return _result_for_existing(existing)

    start, end = usage_period_for(now)
    period_id = _ensure_period(tenant_id, start, end, now)

    reservation = UsageReservation(
        tenant_id=tenant_id,
        usage_period_id=period_id,
        kind=kind,
        idempotency_key=idempotency_key,
        units=units,
        status="reserved",
        reserved_at=now,
        meta=dict(metadata or {}),
    )
    try:
        # Insert + claim in ONE savepoint: if either the unique constraint (a
        # concurrent retry won) or the capacity check fails, both are undone.
        with db.session.begin_nested():
            db.session.add(reservation)
            db.session.flush()
            if not _claim_capacity(tenant_id, period_id, units, now):
                raise _CapacityExhausted()
    except _CapacityExhausted:
        return ReservationResult(False, "quota_exceeded", None, created=False)
    except IntegrityError:
        # Lost the idempotency race (or another constraint fired).  Recover the
        # winner's reservation; if there is none, this was not a duplicate.
        existing = find_reservation(tenant_id, kind, idempotency_key)
        if existing is None:
            raise
        return _result_for_existing(existing)

    return ReservationResult(True, "reserved", reservation, created=True)


# ---------------------------------------------------------------------------
# Commit / release
# ---------------------------------------------------------------------------


def commit_reservation(
    tenant_id: str,
    reservation_id: str,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    now: datetime | None = None,
) -> UsageReservation:
    """``reserved -> committed``, recording OpenAI token counts.

    The unit was already counted at reserve time, so committing never changes
    ``used_units``.  Idempotent: committing an already-committed reservation
    returns it unchanged (the first call's token counts are kept).  Raises
    ``ReservationStateError`` if it was released and ``ReservationNotFound`` if
    it does not exist.  Token counts must be reported values or None.
    """
    tenant_id = str(tenant_id)
    prompt_tokens = _clean_tokens("prompt_tokens", prompt_tokens)
    completion_tokens = _clean_tokens("completion_tokens", completion_tokens)
    total_tokens = _clean_tokens("total_tokens", total_tokens)
    now = _as_utc(now if now is not None else utcnow())

    result = db.session.execute(
        sa.update(UsageReservation)
        .where(
            UsageReservation.id == str(reservation_id),
            UsageReservation.tenant_id == tenant_id,
            UsageReservation.status == "reserved",
        )
        .values(
            status="committed",
            committed_at=now,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    reservation = get_reservation(tenant_id, reservation_id)
    if reservation is None:
        raise ReservationNotFound(f"Reservation {reservation_id} not found")
    if result.rowcount == 1 or reservation.status == "committed":
        return reservation
    raise ReservationStateError(
        f"Reservation {reservation_id} is {reservation.status}; only a reserved "
        "reservation can be committed"
    )


def _return_capacity(period_id: str, tenant_id: str, units: int, now: datetime) -> None:
    """Give ``units`` back to a period.  Clamped at zero: the counter can never
    go negative (the CHECK constraint is the final backstop)."""
    used = TenantUsagePeriod.used_units
    db.session.execute(
        sa.update(TenantUsagePeriod)
        .where(TenantUsagePeriod.id == period_id, TenantUsagePeriod.tenant_id == tenant_id)
        .values(
            used_units=sa.case((used >= units, used - units), else_=0),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )


def release_reservation(
    tenant_id: str, reservation_id: str, *, now: datetime | None = None
) -> UsageReservation:
    """``reserved -> released``, returning the unit(s) to capacity.

    The status flip is a conditional UPDATE (``WHERE status = 'reserved'``), so
    concurrent or repeated releases return capacity exactly once; later calls
    are no-ops that return the released reservation.  Raises
    ``ReservationStateError`` if it was already committed.
    """
    tenant_id = str(tenant_id)
    now = _as_utc(now if now is not None else utcnow())

    flipped = db.session.execute(
        sa.update(UsageReservation)
        .where(
            UsageReservation.id == str(reservation_id),
            UsageReservation.tenant_id == tenant_id,
            UsageReservation.status == "reserved",
        )
        .values(status="released", released_at=now, updated_at=now)
        .returning(UsageReservation.usage_period_id, UsageReservation.units)
        .execution_options(synchronize_session=False)
    ).first()
    if flipped is not None:
        _return_capacity(str(flipped.usage_period_id), tenant_id, flipped.units, now)

    reservation = get_reservation(tenant_id, reservation_id)
    if reservation is None:
        raise ReservationNotFound(f"Reservation {reservation_id} not found")
    if flipped is not None or reservation.status == "released":
        return reservation
    raise ReservationStateError(
        f"Reservation {reservation_id} is {reservation.status}; only a reserved "
        "reservation can be released"
    )


def release_stale_reservations(
    *,
    stale_after: timedelta | None = None,
    now: datetime | None = None,
    tenant_id: str | None = None,
    batch_size: int = DEFAULT_STALE_BATCH,
) -> StaleReleaseResult:
    """Release ``reserved`` reservations older than ``stale_after`` (default:
    ``USAGE_RESERVATION_STALE_SECONDS``, 600 s) and return their capacity.

    Recovery for requests that crashed between reserve and commit/release.
    Safe to call repeatedly and concurrently: rows are claimed with
    ``FOR UPDATE SKIP LOCKED`` and flipped by a conditional UPDATE, so each is
    released exactly once and sweeps never wait on (or deadlock with) normal
    reserve/commit/release traffic.  Processes at most ``batch_size`` rows per
    call; call again while ``released == batch_size``.  There is deliberately no
    scheduler here -- something else must call this.

    The timeout must stay well above the OpenAI timeout: a reservation released
    while its call is still running can no longer be committed.
    """
    batch_size = _clean_units(batch_size)
    now = _as_utc(now if now is not None else utcnow())
    stale_after = stale_after if stale_after is not None else stale_after_default()
    if stale_after <= timedelta(0):
        raise ValueError("stale_after must be positive")
    cutoff = now - stale_after

    candidates = sa.select(UsageReservation.id).where(
        UsageReservation.status == "reserved", UsageReservation.reserved_at < cutoff
    )
    if tenant_id is not None:
        candidates = candidates.where(UsageReservation.tenant_id == str(tenant_id))
    candidates = (
        candidates.order_by(UsageReservation.reserved_at, UsageReservation.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )

    rows = db.session.execute(
        sa.update(UsageReservation)
        .where(UsageReservation.id.in_(candidates), UsageReservation.status == "reserved")
        .values(status="released", released_at=now, updated_at=now)
        .returning(
            UsageReservation.tenant_id, UsageReservation.usage_period_id, UsageReservation.units
        )
        .execution_options(synchronize_session=False)
    ).all()

    per_period: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row.usage_period_id), str(row.tenant_id))
        per_period[key] = per_period.get(key, 0) + row.units
    # Fixed lock order across periods so two sweeps can never deadlock.
    for (period_id, row_tenant), units in sorted(per_period.items()):
        _return_capacity(period_id, row_tenant, units, now)

    released = len(rows)
    if released:
        logger.info("Released %d stale usage reservation(s)", released)
    return StaleReleaseResult(released=released, units=sum(per_period.values()))


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_current_usage(tenant_id: str, *, now: datetime | None = None) -> UsageSnapshot:
    """Return the tenant's usage for the UTC month containing ``now``.

    Read-only: it never creates a period row (no row means 0 used).
    ``configured_limit`` is the stored ``Tenant.monthly_conversation_limit``
    verbatim; ``available_units`` is ``max(limit - used, 0)`` and is 0 whenever
    the limit is zero or negative (those never mean unlimited).
    """
    tenant_id = str(tenant_id)
    limit = _tenant_limit(tenant_id)
    start, end = usage_period_for(now)
    used = db.session.execute(
        sa.select(TenantUsagePeriod.used_units)
        .where(
            TenantUsagePeriod.tenant_id == tenant_id,
            TenantUsagePeriod.period_start == start,
            TenantUsagePeriod.period_end == end,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    used = used or 0
    available = max(limit - used, 0) if limit >= 1 else 0
    return UsageSnapshot(
        tenant_id=tenant_id,
        period_start=start,
        period_end=end,
        configured_limit=limit,
        used_units=used,
        available_units=available,
    )
