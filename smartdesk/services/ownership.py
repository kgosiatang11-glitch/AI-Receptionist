"""Tenant-ownership checks for client-supplied references (C5).

Foreign keys in this schema point at a bare ``customers.id`` / ``users.id`` etc.,
so the database alone does not stop a row in tenant A from referencing a row in
tenant B.  Every tenant-scoped write that accepts an ID from the client must
therefore prove the referenced object belongs to the *authenticated* tenant
before anything is written:

    authenticated actor -> resolved tenant (g.tenant) -> referenced object
        -> belongs to that tenant? -> only then write

The tenant used here is always the one resolved by ``require_tenant`` -- never
an ID taken from the request body.  A platform administrator acting inside one
tenant is checked against that same tenant, so admin rights do not turn a
tenant-scoped write into a cross-tenant one.

Unknown, malformed and cross-tenant IDs are deliberately indistinguishable:
all raise :class:`ReferenceNotFound`, which the API maps to one stable 404.
That avoids confirming that another tenant's object exists and never echoes
anything about it.
"""

from __future__ import annotations

import uuid

from flask import jsonify

from smartdesk.extensions import db
from smartdesk.models import Membership, User

REFERENCE_NOT_FOUND = "reference_not_found"


class ReferenceNotFound(Exception):
    """A referenced object does not exist *in the current tenant*."""

    def __init__(self, field: str):
        super().__init__(field)
        self.field = field


def reference_not_found_response(exc: ReferenceNotFound):
    """The one response shape for every rejected reference."""
    return (
        jsonify(
            {
                "error": f"{exc.field} not found",
                "code": REFERENCE_NOT_FOUND,
                "field": exc.field,
            }
        ),
        404,
    )


def normalize_reference_id(value, field: str) -> str:
    """Return a canonical UUID string, or raise ``ReferenceNotFound``.

    Rejects non-strings and malformed values *before* they reach a query: on
    PostgreSQL a malformed value in a ``uuid`` column is a database error (a
    500), and it must not be distinguishable from "not in your tenant".
    """
    if not isinstance(value, str):
        raise ReferenceNotFound(field)
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        raise ReferenceNotFound(field) from None


def require_owned(model, ref_id, tenant_id: str, field: str):
    """Load ``model`` row ``ref_id`` only if it belongs to ``tenant_id``.

    ``model`` must be tenant-scoped (have a ``tenant_id`` column).  The tenant
    filter is part of the query itself, so a row of another tenant is never
    loaded into the session at all.
    """
    if not hasattr(model, "tenant_id"):
        raise TypeError(f"{model.__name__} is not tenant-scoped; use a model-specific check")
    normalized = normalize_reference_id(ref_id, field)
    row = db.session.execute(
        db.select(model).where(model.id == normalized, model.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if row is None:
        raise ReferenceNotFound(field)
    return row


def optional_owned(model, ref_id, tenant_id: str, field: str):
    """Like :func:`require_owned`, but a missing/empty reference means "none".

    ``None`` and ``""`` clear the reference (existing API behaviour); anything
    else must pass the ownership check.
    """
    if ref_id is None or ref_id == "":
        return None
    return require_owned(model, ref_id, tenant_id, field)


def require_tenant_user(user_id, tenant_id: str, field: str = "assigned_user_id") -> User:
    """Load a user only if they are a member of ``tenant_id``.

    Users are platform-global rows, so tenant ownership is expressed by
    ``Membership``.  A user with no membership in this tenant -- including a
    platform administrator who is not a member -- cannot be referenced.
    """
    normalized = normalize_reference_id(user_id, field)
    user = db.session.execute(
        db.select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(User.id == normalized, Membership.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if user is None:
        raise ReferenceNotFound(field)
    return user


def optional_tenant_user(user_id, tenant_id: str, field: str = "assigned_user_id"):
    if user_id is None or user_id == "":
        return None
    return require_tenant_user(user_id, tenant_id, field)


def require_assignable_user(user_id, tenant_id: str, field: str = "assigned_user_id") -> User:
    """Load a user who may be stored in ``assigned_user_id`` of ``tenant_id`` (C6).

    Stricter than :func:`require_tenant_user`: the user must be an *active*
    member with an assignable role (agent / owner).  Non-members -- including
    platform administrators --, members of other tenants, viewers, managers
    (not in :data:`~smartdesk.models.ASSIGNABLE_ROLES`) and deactivated users
    all raise the same :class:`ReferenceNotFound`, so the response never says
    *why* an id was refused.
    """
    from smartdesk.services.assignments import assignable_user_query

    normalized = normalize_reference_id(user_id, field)
    user = db.session.execute(
        assignable_user_query(tenant_id).where(User.id == normalized)
    ).scalar_one_or_none()
    if user is None:
        raise ReferenceNotFound(field)
    return user


def optional_assignable_user(user_id, tenant_id: str, field: str = "assigned_user_id"):
    """``None`` / ``""`` clear the assignment; anything else must be assignable."""
    if user_id is None or user_id == "":
        return None
    return require_assignable_user(user_id, tenant_id, field)


__all__ = [
    "REFERENCE_NOT_FOUND",
    "ReferenceNotFound",
    "normalize_reference_id",
    "optional_assignable_user",
    "optional_owned",
    "optional_tenant_user",
    "reference_not_found_response",
    "require_assignable_user",
    "require_owned",
    "require_tenant_user",
]
