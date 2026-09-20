"""Tenant knowledge and persona assembly.

This module is the database-backed replacement for the single global
``config/smartdesk_config.json``.  It is deliberately *not* a RAG pipeline: a
tenant's structured knowledge is small enough to pass to the model whole, so
retrieval would add a failure mode (retrieving the wrong section) without
improving accuracy.  The interface is shaped so a retrieval step can be
inserted later behind ``knowledge_context`` without touching the engine.
"""

from __future__ import annotations

import json

from ai.persona import TenantPersona
from smartdesk.extensions import db
from smartdesk.models import (
    KNOWLEDGE_SECTIONS,
    KnowledgeDocument,
    ReceptionistProfile,
    Tenant,
)

#: Human labels for the Control Center knowledge editor.
SECTION_LABELS = {
    "business_information": "Business Information",
    "services": "Services",
    "pricing": "Pricing",
    "opening_hours": "Opening Hours",
    "location": "Location",
    "faqs": "FAQs",
    "policies": "Policies",
    "booking_information": "Booking Information",
    "ai_instructions": "AI Instructions",
}


def get_profile(tenant_id: str) -> ReceptionistProfile | None:
    return ReceptionistProfile.query.filter_by(tenant_id=tenant_id).one_or_none()


def ensure_profile(tenant: Tenant) -> ReceptionistProfile:
    profile = get_profile(tenant.id)
    if profile is None:
        profile = ReceptionistProfile(tenant_id=tenant.id)
        db.session.add(profile)
        db.session.flush()
    return profile


def build_persona(tenant: Tenant) -> TenantPersona:
    """Assemble the engine persona for one tenant."""
    profile = get_profile(tenant.id)
    if profile is None:
        # A tenant with no profile yet gets a neutral receptionist, never the
        # platform's sales persona.
        return TenantPersona(
            business_name=tenant.name,
            sales_mode_enabled=False,
        )
    return TenantPersona(
        business_name=tenant.name,
        assistant_name=profile.display_name or "O'Brien",
        personality=profile.personality or "",
        business_instructions=profile.business_instructions or "",
        handoff_enabled=profile.handoff_enabled,
        lead_capture_enabled=profile.lead_capture_enabled,
        booking_assistance_enabled=profile.booking_assistance_enabled,
        sales_mode_enabled=profile.sales_mode_enabled,
        handoff_message=profile.handoff_message,
        sales_handoff_message=profile.sales_handoff_message,
    )


def knowledge_dict(tenant_id: str) -> dict:
    """Return the tenant's published knowledge as a plain dict.

    Shaped to match the keys the existing intent router already reads
    (``greeting``, ``pricing``, ``business_hours``, ``features``) so the
    deterministic reply paths keep working unchanged.
    """
    documents = KnowledgeDocument.query.filter_by(
        tenant_id=tenant_id, is_published=True
    ).all()

    knowledge: dict = {}
    for doc in documents:
        payload = doc.data or {}
        if doc.body:
            knowledge[doc.section] = doc.body
        if payload:
            knowledge.setdefault(f"{doc.section}_details", payload)

    profile = get_profile(tenant_id)
    if profile and profile.greeting:
        knowledge["greeting"] = profile.greeting

    # Aliases for the keys the legacy router expects.
    if "pricing" in knowledge:
        knowledge.setdefault("pricing", knowledge["pricing"])
    if "opening_hours" in knowledge:
        knowledge["business_hours"] = knowledge["opening_hours"]
    if "business_information" in knowledge:
        knowledge["about"] = knowledge["business_information"]
    services = knowledge.get("services_details", {}).get("items")
    if services:
        knowledge["features"] = services

    return knowledge


def knowledge_context(tenant_id: str) -> str:
    """Serialise tenant knowledge for the model prompt."""
    data = knowledge_dict(tenant_id)
    if not data:
        return "(No business knowledge has been configured yet.)"
    return json.dumps(data, ensure_ascii=False, indent=2)


def seed_sections(tenant: Tenant, content: dict | None = None) -> None:
    """Create empty knowledge sections for a tenant, or backfill blank ones.

    Sections a business has never touched are created blank rather than
    pre-filled: inventing business facts is exactly what the receptionist is
    forbidden to do. But a section that is still genuinely blank -- created
    before this tenant's own spec included content for it (e.g. during an
    earlier partial seed run) -- IS backfilled from that spec on a later
    call. This never overwrites a section a human has actually written
    content into; it only fills in what would otherwise stay silently blank
    forever, since the original "create if missing" logic never revisits a
    row that already exists.
    """
    content = content or {}
    existing = {
        d.section: d
        for d in KnowledgeDocument.query.filter_by(tenant_id=tenant.id).all()
    }
    for section in KNOWLEDGE_SECTIONS:
        payload = content.get(section) or {}
        document = existing.get(section)

        if document is not None:
            is_blank = not document.body and not document.data
            has_spec_content = bool(payload.get("body") or payload.get("data"))
            if is_blank and has_spec_content:
                document.body = payload.get("body")
                document.data = payload.get("data") or {}
                document.is_published = True
            continue

        db.session.add(
            KnowledgeDocument(
                tenant_id=tenant.id,
                section=section,
                title=SECTION_LABELS[section],
                body=payload.get("body"),
                data=payload.get("data") or {},
                # Blank sections stay unpublished so the model is never handed
                # an empty heading it might try to fill in.
                is_published=bool(payload.get("body") or payload.get("data")),
            )
        )
