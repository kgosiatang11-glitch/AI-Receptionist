"""Seed the four initial tenants.

Run with ``flask --app wsgi seed``.  Idempotent: existing tenants are left
alone, so it is safe to re-run after adding a channel or knowledge section.

What this deliberately does NOT do:

* invent salon business facts — the salon's knowledge sections are created
  empty and unpublished, with the tenant named by ``SALON_TENANT_NAME``;
* invent phone numbers — channels are only created from environment variables
  you supply, because a wrong number here would silently route one business's
  customers to another;
* claim booking-system API access — 10by20's existing external booking system
  is recorded as an external integration reference, not an API client.
"""

from __future__ import annotations

import os

import click
from flask.cli import with_appcontext

from smartdesk.extensions import db
from smartdesk.models import (
    AUTOMATION_KINDS,
    Automation,
    Channel,
    ReceptionistProfile,
    Tenant,
)
from smartdesk.services.knowledge import seed_sections
from smartdesk.tenancy import normalize_address

# SmartDesk's own sales copy. This is the ONLY tenant that gets it.
SMARTDESK_KNOWLEDGE = {
    "business_information": {
        "body": (
            "SmartDesk AI builds AI receptionists for businesses. O'Brien answers "
            "customer enquiries around the clock over WhatsApp and phone calls, "
            "provides business information, captures leads and assists with bookings."
        )
    },
    "services": {
        "data": {
            "items": [
                "AI WhatsApp Receptionists",
                "24/7 Automated Customer Support",
                "Appointment & Booking Automation",
                "Customer FAQs",
                "Lead capture",
                "Business information",
                "Multi-language support",
                "Human handoff",
                "Custom business knowledge",
            ]
        }
    },
    "pricing": {
        "body": (
            "Please contact sales for pricing plans tailored to your business "
            "size and needs."
        )
    },
    "opening_hours": {"body": "We are available 24 hours a day, 7 days a week."},
}

TENANT_SPECS = [
    {
        "slug": "smartdesk",
        "name": "SmartDesk AI",
        "business_type": "technology",
        "status": "active",
        "is_internal": True,
        "is_test_data": False,
        "knowledge": SMARTDESK_KNOWLEDGE,
        "profile": {
            "greeting": (
                "Hello and welcome to SmartDesk AI! I'm O'Brien, your AI "
                "Receptionist. How can I assist you today?"
            ),
            "voice_greeting": (
                "Hello! Thank you for calling Smart Desk AI. How can I help you today?"
            ),
            # The platform's own tenant is the only one that sells.
            "sales_mode_enabled": True,
        },
        "channel_env": {
            "whatsapp": "SMARTDESK_WHATSAPP_NUMBER",
            "voice": "SMARTDESK_VOICE_NUMBER",
        },
    },
    {
        "slug": "10by20",
        "name": "10by20 Padel Club",
        "business_type": "sports",
        "status": "active",
        "is_internal": False,
        "is_test_data": False,
        # Left empty on purpose: real opening hours, pricing and services must
        # be entered by the club in the Control Center, not guessed here.
        "knowledge": {},
        "profile": {
            "greeting": (
                "Hello and welcome to 10by20 Padel Club! I'm O'Brien. "
                "How can I help you today?"
            ),
            "voice_greeting": (
                "Hello! Thank you for calling 10by20 Padel Club. "
                "How can I help you today?"
            ),
            "sales_mode_enabled": False,
        },
        "channel_env": {
            "whatsapp": "TENBY20_WHATSAPP_NUMBER",
            "voice": "TENBY20_VOICE_NUMBER",
        },
        # Recorded as a reference only. SmartDesk has no API access to it.
        "external_booking_system": "Playbypoint",
    },
    {
        "slug": "salon",
        # Overridden by SALON_TENANT_NAME once the real name is supplied.
        "name": os.getenv("SALON_TENANT_NAME", "Salon (name pending)"),
        "business_type": "beauty",
        "status": "active",
        "is_internal": False,
        "is_test_data": False,
        "knowledge": {},
        "profile": {
            "greeting": None,
            "voice_greeting": None,
            "sales_mode_enabled": False,
        },
        "channel_env": {
            "whatsapp": "SALON_WHATSAPP_NUMBER",
            "voice": "SALON_VOICE_NUMBER",
        },
    },
    {
        "slug": "test-business",
        "name": "Test Business",
        "business_type": "test",
        "status": "development",
        "is_internal": True,
        # Everything created under this tenant is flagged as test data so it
        # can never be mistaken for, or aggregated with, production figures.
        "is_test_data": True,
        "knowledge": {
            "business_information": {
                "body": "Development tenant used for local testing only."
            },
            "opening_hours": {"body": "Test hours: 08:00-17:00, Monday to Friday."},
        },
        "profile": {
            "greeting": "Hello, this is the Test Business assistant.",
            "voice_greeting": "Hello, this is the Test Business assistant.",
            "sales_mode_enabled": False,
        },
        "channel_env": {
            "whatsapp": "TEST_WHATSAPP_NUMBER",
            "voice": "TEST_VOICE_NUMBER",
        },
    },
]


def _seed_tenant(spec: dict) -> Tenant:
    tenant = Tenant.query.filter_by(slug=spec["slug"]).one_or_none()
    created = tenant is None
    if created:
        tenant = Tenant(slug=spec["slug"])
        db.session.add(tenant)

    tenant.name = spec["name"]
    tenant.business_type = spec["business_type"]
    tenant.status = spec["status"]
    tenant.is_internal = spec["is_internal"]
    tenant.is_test_data = spec["is_test_data"]
    db.session.flush()

    profile = ReceptionistProfile.query.filter_by(tenant_id=tenant.id).one_or_none()
    if profile is None:
        profile = ReceptionistProfile(tenant_id=tenant.id, **spec["profile"])
        db.session.add(profile)

    seed_sections(tenant, spec.get("knowledge"))

    for kind, env_name in spec["channel_env"].items():
        address = normalize_address(os.getenv(env_name))
        if not address:
            continue
        existing = Channel.query.filter_by(kind=kind, address=address).one_or_none()
        if existing is not None:
            if existing.tenant_id != tenant.id:
                click.echo(
                    f"  ! {kind} number {address} already belongs to another "
                    f"tenant; skipping",
                    err=True,
                )
            continue
        db.session.add(
            Channel(
                tenant_id=tenant.id,
                kind=kind,
                address=address,
                display_name=f"{tenant.name} {kind}",
            )
        )
        click.echo(f"  + {kind} channel {address}")

    for kind in AUTOMATION_KINDS:
        if Automation.query.filter_by(tenant_id=tenant.id, kind=kind).one_or_none():
            continue
        db.session.add(
            Automation(
                tenant_id=tenant.id,
                kind=kind,
                name=kind.replace("_", " ").title(),
                status="pending",
                is_implemented=False,
            )
        )

    click.echo(f"{'Created' if created else 'Updated'} tenant: {tenant.name}")
    return tenant


@click.command("seed")
@with_appcontext
def seed_command() -> None:
    """Create or update the initial tenants."""
    for spec in TENANT_SPECS:
        _seed_tenant(spec)
    db.session.commit()
    click.echo(
        "\nDone. Salon knowledge is intentionally empty until real details are "
        "supplied.\nSet SALON_TENANT_NAME and the *_WHATSAPP_NUMBER / "
        "*_VOICE_NUMBER variables, then re-run."
    )


@click.command("grant")
@click.argument("email")
@click.option("--tenant", "tenant_slug", help="Tenant slug, e.g. 10by20")
@click.option("--role", default="owner", help="viewer | agent | manager | owner")
@click.option("--platform-admin", is_flag=True, help="Grant SmartDesk admin rights")
@with_appcontext
def grant_command(email, tenant_slug, role, platform_admin) -> None:
    """Grant a user access. The user must have signed in via Supabase once."""
    from smartdesk.models import Membership, User

    user = User.query.filter_by(email=email.lower()).one_or_none()
    if user is None:
        raise click.ClickException(
            f"No user with email {email}. Ask them to sign in to the dashboard once."
        )

    if platform_admin:
        user.is_platform_admin = True
        click.echo(f"{email} is now a SmartDesk platform administrator.")

    if tenant_slug:
        tenant = Tenant.query.filter_by(slug=tenant_slug).one_or_none()
        if tenant is None:
            raise click.ClickException(f"No tenant with slug {tenant_slug}")
        membership = Membership.query.filter_by(
            tenant_id=tenant.id, user_id=user.id
        ).one_or_none()
        if membership is None:
            membership = Membership(tenant_id=tenant.id, user_id=user.id)
            db.session.add(membership)
        membership.role = role
        click.echo(f"{email} is now {role} of {tenant.name}.")

    db.session.commit()
