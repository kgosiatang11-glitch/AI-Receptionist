"""Shared tenant-creation logic.

Two callers create a ``Tenant`` from scratch: the Super Admin manual-entry
endpoint (``smartdesk/api/admin_api.py``, kept for platform-staff use) and
the self-service customer signup endpoint (``smartdesk/api/signup_api.py``).
Both need the exact same result -- a tenant plus the same "empty, honest"
starting data (a disabled receptionist profile, blank knowledge sections,
the automation placeholders) -- so that logic lives here once instead of
being duplicated and risking the two flows drifting apart.
"""

from __future__ import annotations

from smartdesk.extensions import db
from smartdesk.models import AUTOMATION_KINDS, Automation, ReceptionistProfile, Tenant
from smartdesk.services.knowledge import seed_sections

VALID_PLANS = ("basic", "professional", "enterprise")
VALID_STATUSES = ("active", "development", "suspended")


def slugify_tenant_name(name: str) -> str:
    """Turn a business name into a unique, URL-safe tenant slug."""
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    while "--" in base:
        base = base.replace("--", "-")
    base = base.strip("-") or "tenant"
    slug = base
    suffix = 1
    # Guarantee uniqueness against the real unique constraint rather than
    # hoping the caller's chosen name never collides.
    while Tenant.query.filter_by(slug=slug).one_or_none() is not None:
        suffix += 1
        slug = f"{base}-{suffix}"
    return slug


def provision_new_tenant(
    *,
    name: str,
    business_type: str = "other",
    status: str = "active",
    plan: str = "basic",
    owner_name: str | None = None,
    owner_email: str | None = None,
    owner_phone: str | None = None,
    escalation_email: str | None = None,
    knowledge_seed: dict | None = None,
) -> Tenant:
    """Create a ``Tenant`` plus its default receptionist/knowledge/automation
    rows. Does not commit -- the caller decides the transaction boundary
    (e.g. so a Membership row can be added in the same commit).

    ``knowledge_seed`` is passed straight through to ``seed_sections`` --
    e.g. ``{"location": {"body": "123 Main St"}}`` to publish the business's
    stated location as its own knowledge section immediately, using the
    existing knowledge-base schema rather than adding a new column for it.
    """
    tenant = Tenant(
        slug=slugify_tenant_name(name),
        name=name,
        business_type=business_type or "other",
        status=status,
        plan=plan,
        owner_name=owner_name,
        owner_email=owner_email,
        owner_phone=owner_phone,
        escalation_email=escalation_email,
    )
    db.session.add(tenant)
    db.session.flush()

    # Same shape every tenant gets -- an empty, honest starting point.
    # Never invents business facts for a tenant that hasn't supplied any.
    db.session.add(ReceptionistProfile(tenant_id=tenant.id, sales_mode_enabled=False))
    seed_sections(tenant, knowledge_seed or {})
    for kind in AUTOMATION_KINDS:
        db.session.add(
            Automation(
                tenant_id=tenant.id, kind=kind, name=kind.replace("_", " ").title()
            )
        )
    return tenant
