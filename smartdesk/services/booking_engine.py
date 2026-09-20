"""The conversational booking layer for the WhatsApp/voice AI receptionist.

Two booking modes, configured per tenant on :class:`ReceptionistProfile`:

- ``calendar`` — SmartDesk owns the booking. Slots are collected across
  turns into a :class:`BookingConversationState` row, checked against the
  tenant's connected Google Calendar via :mod:`smartdesk.services.calendar`,
  confirmed with the customer, and only then written as a real
  :class:`Booking` + calendar event.
- ``external`` — the tenant already has its own booking system (10by20's
  Playbypoint, say). O'Brien never creates a booking or a calendar event;
  it only ever hands back the tenant's own configured URL.

Entry point is :func:`handle_booking_message`, called from
:mod:`smartdesk.services.receptionist` as the engine's optional
``booking_handler``. It returns ``None`` when the message isn't
booking-related at all, which the caller treats as "let the normal
canned-reply / OpenAI path handle this" — booking logic never hijacks an
unrelated question.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from smartdesk.extensions import db
from smartdesk.models import (
    Booking,
    BookingConversationState,
    Conversation,
    ReceptionistProfile,
    Tenant,
)
from smartdesk.services import calendar as calendar_service
from smartdesk.services import nlu_datetime as ndt
from smartdesk.services.knowledge import get_profile, knowledge_dict

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("collecting", "confirming")

_CANCEL_RE = re.compile(r"\bcancel\b")
_MODIFY_RE = re.compile(r"\b(reschedule|move|change|different time|instead|push)\b")
_STATUS_RE = re.compile(
    r"\b(do i have|what'?s my|check my|my appointment|my booking|any booking)\b"
)
# A raw keyword check, deliberately NOT the single classified label from
# detect_intent() -- a message can be a greeting AND a booking request at
# once ("Hi, can I book Saturday at 6pm?"), and detect_intent only returns
# one label. Using its own keyword match here means a greeting prefix can
# never cause a booking request to be missed.
_BOOKING_KEYWORDS_RE = re.compile(
    r"\b(book|booking|appointment|schedule|reserve|reservation)\b"
)

#: A conservative internal search window for offering alternatives. This is
#: never shown to the customer as a claim about opening hours -- it only
#: bounds which candidate times get checked against the real calendar.
_ALTERNATIVE_WINDOW_START_HOUR = 9
_ALTERNATIVE_WINDOW_END_HOUR = 20


def handle_booking_message(
    tenant: Tenant, conversation: Conversation, channel: str, message: str
) -> str | None:
    profile = get_profile(tenant.id)
    if profile is None or not profile.booking_assistance_enabled:
        return None

    if profile.booking_mode == "external":
        return _handle_external_mode(tenant, profile, conversation, message)
    return _handle_calendar_mode(tenant, profile, conversation, message)


# ---------------------------------------------------------------------------
# External booking mode
# ---------------------------------------------------------------------------


def _handle_external_mode(
    tenant: Tenant, profile: ReceptionistProfile, conversation: Conversation, message: str
) -> str | None:
    if not _BOOKING_KEYWORDS_RE.search(message.lower()):
        return None  # Not booking-related; let the normal reply path answer.

    if profile.external_booking_url:
        return (
            f"Sure! You can book directly here: {profile.external_booking_url}"
        )
    return (
        "We take bookings through our own system, but I don't have that link "
        "configured yet — please contact us directly to book."
    )


# ---------------------------------------------------------------------------
# Calendar booking mode — dispatch
# ---------------------------------------------------------------------------


def _handle_calendar_mode(
    tenant: Tenant, profile: ReceptionistProfile, conversation: Conversation, message: str
) -> str | None:
    active_state = get_active_state(conversation.id)

    if _CANCEL_RE.search(message.lower()):
        return _handle_cancel(tenant, conversation, active_state)

    if _MODIFY_RE.search(message.lower()) and active_state is None:
        return _handle_modify_request(tenant, profile, conversation, message)

    if _STATUS_RE.search(message.lower()) and active_state is None:
        return _handle_status_check(tenant, conversation)

    if active_state is not None:
        return _continue_state(tenant, profile, conversation, active_state, message)

    if not _BOOKING_KEYWORDS_RE.search(message.lower()):
        return None  # Nothing booking-related here; don't hijack the turn.

    state = _create_state(tenant, conversation)
    return _continue_state(tenant, profile, conversation, state, message)


def get_active_state(conversation_id: str) -> BookingConversationState | None:
    return (
        BookingConversationState.query.filter(
            BookingConversationState.conversation_id == conversation_id,
            BookingConversationState.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(BookingConversationState.created_at.desc())
        .first()
    )


def _latest_confirmed(conversation: Conversation) -> BookingConversationState | None:
    """Find the customer's most recent confirmed booking to modify/cancel.

    Looks across the customer's other conversations too (not just this one)
    since "cancel my booking" doesn't require the customer to be in the same
    WhatsApp thread that made it -- but never outside this tenant.
    """
    query = BookingConversationState.query.filter(
        BookingConversationState.tenant_id == conversation.tenant_id,
        BookingConversationState.status == "confirmed",
        BookingConversationState.booking_id.isnot(None),
    )
    if conversation.customer_id:
        query = query.filter(BookingConversationState.customer_id == conversation.customer_id)
    else:
        query = query.filter(BookingConversationState.conversation_id == conversation.id)
    return query.order_by(BookingConversationState.updated_at.desc()).first()


def _create_state(tenant: Tenant, conversation: Conversation) -> BookingConversationState:
    state = BookingConversationState(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        customer_id=conversation.customer_id,
        timezone=tenant.timezone,
        status="collecting",
    )
    db.session.add(state)
    db.session.flush()
    return state


# ---------------------------------------------------------------------------
# Cancel / modify / status
# ---------------------------------------------------------------------------


def _handle_cancel(
    tenant: Tenant, conversation: Conversation, active_state: BookingConversationState | None
) -> str:
    if active_state is not None:
        active_state.status = "cancelled"
        return (
            "No problem, I've cancelled that request. Let me know if you'd "
            "like to book a different time."
        )

    confirmed = _latest_confirmed(conversation)
    if confirmed is None:
        return "I couldn't find an existing booking for you. Would you like to make one?"

    booking = db.session.get(Booking, confirmed.booking_id)
    if booking is not None:
        booking.status = "cancelled"
        calendar_service.cancel_event_for_booking(tenant, booking)
    confirmed.status = "cancelled"
    return "Your booking has been cancelled. Let me know if you'd like to book another time."


def _handle_status_check(tenant: Tenant, conversation: Conversation) -> str:
    confirmed = _latest_confirmed(conversation)
    if confirmed is None:
        return "I don't have any upcoming booking on file for you. Would you like to make one?"
    booking = db.session.get(Booking, confirmed.booking_id)
    if booking is None or not booking.starts_at:
        return "I don't have any upcoming booking on file for you. Would you like to make one?"
    when = _humanize_datetime(ensure_aware_utc(booking.starts_at).astimezone(ZoneInfo(tenant.timezone)))
    service_note = f" for {booking.service}" if booking.service else ""
    return f"You're booked{service_note} on {when}."


def _handle_modify_request(
    tenant: Tenant, profile: ReceptionistProfile, conversation: Conversation, message: str
) -> str:
    confirmed = _latest_confirmed(conversation)
    if confirmed is None:
        return "I couldn't find an existing booking to change. Would you like to make a new one?"
    booking = db.session.get(Booking, confirmed.booking_id)
    if booking is None:
        return "I couldn't find an existing booking to change. Would you like to make a new one?"

    # A fresh state, pre-linked to the booking being modified -- that link
    # is what turns the eventual confirmation into an UPDATE rather than a
    # brand new booking. Carries over what we already know (date/service)
    # so "move it to 7pm" only needs a new time, not the whole booking again.
    tz = ZoneInfo(tenant.timezone)
    local_start = ensure_aware_utc(booking.starts_at).astimezone(tz) if booking.starts_at else None
    state = BookingConversationState(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        customer_id=conversation.customer_id,
        timezone=tenant.timezone,
        status="collecting",
        booking_id=booking.id,
        requested_date=local_start.date() if local_start else None,
        requested_time=local_start.time() if local_start else None,
        duration_minutes=int(
            (ensure_aware_utc(booking.ends_at) - ensure_aware_utc(booking.starts_at)).total_seconds() // 60
        ) if booking.starts_at and booking.ends_at else None,
        service=booking.service,
    )
    db.session.add(state)
    db.session.flush()
    return _continue_state(tenant, profile, conversation, state, message)


# ---------------------------------------------------------------------------
# Slot collection / confirmation / creation
# ---------------------------------------------------------------------------


def _continue_state(
    tenant: Tenant,
    profile: ReceptionistProfile,
    conversation: Conversation,
    state: BookingConversationState,
    message: str,
) -> str:
    if state.status == "confirming":
        return _handle_confirmation_reply(tenant, profile, conversation, state, message)
    return _collect_slots(tenant, profile, conversation, state, message)


def _collect_slots(
    tenant: Tenant,
    profile: ReceptionistProfile,
    conversation: Conversation,
    state: BookingConversationState,
    message: str,
) -> str:
    tz_name = tenant.timezone
    known_services = _known_services(tenant)

    new_date = ndt.extract_date(message, tz_name)
    new_time = ndt.extract_time(message)
    new_duration = ndt.extract_duration_minutes(message)
    new_party = ndt.extract_party_size(message)
    new_service = ndt.extract_service(message, known_services)
    new_name = ndt.extract_customer_name(message)

    if new_date:
        state.requested_date = new_date
    if new_time:
        state.requested_time = new_time
    if new_duration:
        state.duration_minutes = new_duration
    if new_party:
        state.party_size = new_party
    if new_service:
        state.service = new_service
    if new_name:
        state.customer_name = new_name

    if state.requested_date is None:
        state.last_prompted_field = "date"
        return "Sure! What date would you like to come in?"

    if state.requested_time is None:
        period = ndt.extract_vague_period(message)
        date_label = _humanize_date(state.requested_date)
        state.last_prompted_field = "time"
        if period:
            return f"Great, {date_label}. What time in the {period} would suit you?"
        return f"Sure! What time on {date_label} would you like?"

    return _check_availability_and_prompt(tenant, profile, state)


def _check_availability_and_prompt(
    tenant: Tenant, profile: ReceptionistProfile, state: BookingConversationState
) -> str:
    tz = ZoneInfo(tenant.timezone)
    duration = state.duration_minutes or profile.default_booking_duration_minutes
    start = datetime.combine(state.requested_date, state.requested_time, tzinfo=tz)
    end = start + timedelta(minutes=duration)

    try:
        available = calendar_service.check_availability(tenant, start, end)
    except calendar_service.CalendarError as exc:
        state.status = "failed"
        state.last_failure_reason = str(exc)
        return (
            "I'm having trouble checking availability right now. Please try "
            "again shortly, or contact us directly to book."
        )

    if not available:
        alternatives = _find_alternatives(tenant, start, duration)
        if alternatives:
            options = " or ".join(_humanize_time(alt) for alt in alternatives)
            return (
                f"{_humanize_datetime(start)} isn't available. "
                f"Would {options} work instead?"
            )
        return (
            f"{_humanize_datetime(start)} isn't available, and I couldn't find "
            "another free slot that day. Would you like to try a different day?"
        )

    state.status = "confirming"
    duration_note = f", {duration}-minute booking" if duration != 60 else ""
    service_note = f" for {state.service}" if state.service else ""
    party_note = f" for {state.party_size}" if state.party_size else ""
    return (
        f"Just to confirm: {_humanize_datetime(start)}{duration_note}"
        f"{service_note}{party_note}. Would you like me to confirm that?"
    )


def _handle_confirmation_reply(
    tenant: Tenant,
    profile: ReceptionistProfile,
    conversation: Conversation,
    state: BookingConversationState,
    message: str,
) -> str:
    if ndt.is_affirmative(message):
        return _finalize_booking(tenant, conversation, state)

    # A correction ("actually make it 7pm") looks negative-ish but carries new
    # information; try extracting a new date/time before giving up to "no".
    new_date = ndt.extract_date(message, tenant.timezone)
    new_time = ndt.extract_time(message)
    if new_date or new_time:
        state.status = "collecting"
        return _collect_slots(tenant, profile, conversation, state, message)

    if ndt.is_negative_or_change(message):
        state.status = "collecting"
        return "No problem — what date and time would work instead?"

    return "Sorry, should I go ahead and confirm that booking? (yes/no)"


def _finalize_booking(
    tenant: Tenant, conversation: Conversation, state: BookingConversationState
) -> str:
    # Idempotency backstop: if another turn already finalized this state
    # (e.g. a retried webhook processed the confirmation twice), don't
    # create a second booking or a second calendar event.
    db.session.refresh(state)
    if state.status == "confirmed":
        booking = db.session.get(Booking, state.booking_id) if state.booking_id else None
        if booking and booking.starts_at:
            when = _humanize_datetime(
                ensure_aware_utc(booking.starts_at).astimezone(ZoneInfo(tenant.timezone))
            )
            return f"You're already booked for {when}. See you then!"

    tz = ZoneInfo(tenant.timezone)
    duration = state.duration_minutes or 60
    start = datetime.combine(state.requested_date, state.requested_time, tzinfo=tz)
    end = start + timedelta(minutes=duration)

    is_reschedule = state.booking_id is not None
    if is_reschedule:
        booking = db.session.get(Booking, state.booking_id)
        calendar_service.cancel_event_for_booking(tenant, booking)
        # Always store UTC explicitly. This isn't just tidiness: some
        # backends (SQLite, notably, used in this test suite) preserve the
        # wall-clock digits of whatever's assigned but silently drop the UTC
        # offset on readback, so an ambiguous "naive means what timezone?"
        # question would arise later. Normalizing to UTC on write makes the
        # later ``ensure_aware_utc`` reinterpretation on read correct by
        # construction, on SQLite and Postgres alike.
        booking.starts_at = start.astimezone(timezone.utc)
        booking.ends_at = end.astimezone(timezone.utc)
        booking.service = state.service or booking.service
        booking.status = "pending"
    else:
        booking = Booking(
            tenant_id=tenant.id,
            customer_id=state.customer_id,
            conversation_id=conversation.id,
            service=state.service,
            starts_at=start.astimezone(timezone.utc),
            ends_at=end.astimezone(timezone.utc),
            status="pending",
            source="ai",
            is_test_data=tenant.is_test_data,
        )
        db.session.add(booking)
    db.session.flush()

    try:
        calendar_service.create_event_for_booking(tenant, booking)
    except calendar_service.CalendarError as exc:
        # The booking row exists but stays 'pending' -- never told to the
        # customer as confirmed. State stays 'confirming' so a retry ("yes")
        # can safely try again without creating a duplicate.
        state.last_failure_reason = str(exc)
        return (
            "I wasn't able to confirm that with our calendar just now. "
            "Your request is saved — please try confirming again in a "
            "moment, or we'll follow up with you directly."
        )

    state.status = "confirmed"
    state.booking_id = booking.id
    when = _humanize_datetime(start)
    verb = "moved to" if is_reschedule else "booked for"
    return f"You're {verb} {when}. See you then!"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _known_services(tenant: Tenant) -> list[str]:
    data = knowledge_dict(tenant.id)
    services = data.get("features") or data.get("services_details", {}).get("items") or []
    return [s for s in services if isinstance(s, str)]


def _find_alternatives(
    tenant: Tenant, requested_start: datetime, duration: int, limit: int = 2
) -> list[datetime]:
    """Check real candidate slots and return up to ``limit`` that are free.

    Every candidate is verified against the calendar -- never guessed.
    """
    found: list[datetime] = []
    day_start = requested_start.replace(
        hour=_ALTERNATIVE_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    day_end = requested_start.replace(
        hour=_ALTERNATIVE_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    offsets = [30, -30, 60, -60, 90, -90, 120, -120]
    for offset in offsets:
        if len(found) >= limit:
            break
        candidate = requested_start + timedelta(minutes=offset)
        if candidate < day_start or candidate + timedelta(minutes=duration) > day_end:
            continue
        try:
            if calendar_service.check_availability(
                tenant, candidate, candidate + timedelta(minutes=duration)
            ):
                found.append(candidate)
        except calendar_service.CalendarError:
            break  # Calendar is having trouble; stop guessing, don't loop.
    return found


def ensure_aware_utc(value: datetime) -> datetime:
    """Coerce a possibly-naive datetime to UTC-aware.

    A value read back from the database can come back naive if the driver
    doesn't round-trip timezone info perfectly (this happens on SQLite;
    Postgres's ``timestamptz`` does not have this problem). Python's default
    behaviour for a naive datetime is to assume SYSTEM LOCAL time on the next
    ``.astimezone()`` call, which is fragile and environment-dependent.
    Treating a naive readback as UTC instead is the correct assumption for
    anything this app itself wrote, since every write path here constructs
    timezone-aware values.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _humanize_date(value) -> str:
    return value.strftime("%A, %d %B")


def _humanize_time(value: datetime) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def _humanize_datetime(value: datetime) -> str:
    return f"{_humanize_date(value.date())} at {_humanize_time(value)}"
