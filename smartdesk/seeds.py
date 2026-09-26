"""Production seeding and admin-promotion CLI commands.

Run with ``flask --app wsgi seed``. A fresh, sellable installation of this
platform starts with **zero pre-existing tenants**. This command does not
create any business-specific data -- there is nothing here for a new
customer to inherit, edit around, or accidentally expose.

The intended path to your first tenant and platform administrator is:

1. Sign up through the dashboard (Supabase Auth).
2. Confirm your email.
3. Create your business via the normal self-service ``NoBusinessPage`` /
   ``POST /signup/business`` flow, OR have a platform administrator create
   it for you via the Admin Tenants screen / ``POST /admin/tenants``.
4. Promote your account to platform administrator with:
   ``flask --app wsgi grant you@yourcompany.com --platform-admin``

Vendor-specific or development-only tenant data (e.g. a "SmartDesk AI" demo
tenant, or a disposable test-business tenant) deliberately does NOT live
here. See ``scripts/dev_seed.py`` for that -- it is a standalone,
clearly-labelled development/test tool that is never wired into this app's
CLI and is never run as part of a normal deployment.
"""

from __future__ import annotations

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


def seed_tenant(spec: dict) -> Tenant:
    """Create or update a single tenant from a spec dict.

    Generic, business-agnostic helper -- shared by this module (which, in
    production, calls it zero times) and by ``scripts/dev_seed.py`` (which
    uses it to set up development-only tenants). Kept here so that
    behaviour cannot drift between the two call sites.
    """
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

    for kind, env_name in spec.get("channel_env", {}).items():
        address = normalize_address(_env_value(env_name))
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


def _env_value(env_name: str) -> str | None:
    import os

    return os.getenv(env_name)


# Kept for backwards compatibility with any external tooling that imported
# the old private name.
_seed_tenant = seed_tenant


@click.command("seed")
@with_appcontext
def seed_command() -> None:
    """Production seed entrypoint. Intentionally creates nothing.

    A fresh SmartDesk AI installation has no pre-existing tenants, demo
    businesses, or vendor sales data. This command exists so that
    deployment scripts calling ``flask seed`` out of habit don't fail; it
    prints guidance instead of inserting anything into the database.
    """
    click.echo(
        "Nothing to seed -- this is a clean installation.\n\n"
        "To get started:\n"
        "  1. Sign up through the dashboard.\n"
        "  2. Confirm your email.\n"
        "  3. Create your business (self-service, from the dashboard) or "
        "have an existing platform admin create it for you.\n"
        "  4. Promote your account to platform administrator:\n"
        "       flask --app wsgi grant you@yourcompany.com --platform-admin\n\n"
        "For development/test-only sample tenants, see scripts/dev_seed.py "
        "-- it is intentionally separate from this command and is never run "
        "automatically."
    )


@click.command("grant")
@click.argument("email")
@click.option("--tenant", "tenant_slug", help="Tenant slug, e.g. acme-corp")
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
