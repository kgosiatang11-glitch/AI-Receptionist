"""Import the legacy single-tenant flat files into the Test Business tenant.

Run with ``flask --app wsgi legacy-import --state-dir /path/to/production/state``.

The source files are read-only inputs; nothing here deletes or modifies them,
so the legacy deployment keeps working untouched if you run this against a
copy of production before decommissioning it.

Everything created here is explicitly flagged ``is_test_data=True`` and lands
under the Test Business tenant only, never under any real customer's
tenant — production and test data must never mix. This is also why it is a
separate, explicit command rather than something ``seed`` runs
automatically: importing real customer conversation history is worth a
deliberate decision each time, not a side effect of routine setup.

The Test Business tenant is development/test tooling, not part of a normal
production seed -- create it locally with ``python scripts/dev_seed.py``
(requires ``DATABASE_URL`` to point at a development database) before
running this command.

Idempotent: re-running against the same files does not duplicate customers,
conversations or messages (matched on phone / session key), though it will
append any messages not already present.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click
from flask.cli import with_appcontext

from smartdesk.extensions import db
from smartdesk.models import Channel, Conversation, Customer, Message, Tenant

TEST_TENANT_SLUG = "test-business"


def _parse_sessions(path: Path) -> dict[str, datetime]:
    """``sender|iso-timestamp`` per line -> {sender: first_seen}."""
    if not path.exists():
        return {}
    first_seen: dict[str, datetime] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        sender, _, timestamp = line.partition("|")
        sender = sender.strip()
        try:
            when = datetime.fromisoformat(timestamp.strip())
        except ValueError:
            when = datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if sender not in first_seen or when < first_seen[sender]:
            first_seen[sender] = when
    return first_seen


def _parse_users(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _parse_history(path: Path) -> dict[str, list[dict]]:
    """``{sender: [{role, content}, ...]}`` as written by the legacy engine."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise click.ClickException(f"Could not parse conversation history: {exc}")


def _session_channel_kind(sender: str) -> str:
    return "voice" if sender.startswith("voice:") else "whatsapp"


def _get_test_tenant() -> Tenant:
    tenant = Tenant.query.filter_by(slug=TEST_TENANT_SLUG).one_or_none()
    if tenant is None:
        raise click.ClickException(
            f"No '{TEST_TENANT_SLUG}' tenant found. This is development/test "
            "tooling: run 'python scripts/dev_seed.py' first (with DATABASE_URL "
            "pointed at a development database) to create it."
        )
    return tenant


def _get_or_create_placeholder_channel(tenant: Tenant, kind: str) -> Channel:
    """A synthetic channel for imported history that has no real number.

    Real Test Business channels (from *_WHATSAPP_NUMBER / *_VOICE_NUMBER env
    vars) still take priority if one already exists for this kind.
    """
    existing = Channel.query.filter_by(tenant_id=tenant.id, kind=kind).first()
    if existing is not None:
        return existing
    address = f"legacy-import:{kind}"
    channel = Channel.query.filter_by(kind=kind, address=address).one_or_none()
    if channel is None:
        channel = Channel(
            tenant_id=tenant.id,
            kind=kind,
            address=address,
            display_name=f"Imported legacy {kind} history",
            is_active=False,
        )
        db.session.add(channel)
        db.session.flush()
    return channel


def _import_customer(tenant: Tenant, sender: str, first_seen: datetime | None) -> Customer:
    phone = None if sender.startswith("voice:") else sender.replace("whatsapp:", "")
    lookup_key = phone or sender

    customer = None
    if phone:
        customer = Customer.query.filter_by(tenant_id=tenant.id, phone=phone).one_or_none()
    if customer is None:
        customer = Customer(
            tenant_id=tenant.id,
            phone=phone,
            is_test_data=True,
            first_contact_at=first_seen or datetime.now(timezone.utc),
            last_contact_at=first_seen or datetime.now(timezone.utc),
        )
        db.session.add(customer)
        db.session.flush()
    return customer


def _import_conversation(
    tenant: Tenant, channel: Channel, sender: str, customer: Customer | None
) -> Conversation:
    conversation = Conversation.query.filter_by(
        tenant_id=tenant.id, session_key=sender
    ).one_or_none()
    if conversation is None:
        conversation = Conversation(
            tenant_id=tenant.id,
            channel_id=channel.id,
            channel_kind=_session_channel_kind(sender),
            session_key=sender,
            customer_id=customer.id if customer else None,
            is_test_data=True,
            status="closed",  # Imported history is historical, not live.
        )
        db.session.add(conversation)
        db.session.flush()
    return conversation


_ROLE_MAP = {"user": "customer", "assistant": "assistant"}


def _import_messages(tenant: Tenant, conversation: Conversation, turns: list[dict]) -> int:
    existing_count = Message.query.filter_by(conversation_id=conversation.id).count()
    imported = 0
    for index, turn in enumerate(turns):
        if index < existing_count:
            continue  # Already imported on a previous run.
        role = _ROLE_MAP.get(turn.get("role"), "customer")
        body = (turn.get("content") or "").strip()
        if not body:
            continue
        db.session.add(
            Message(tenant_id=tenant.id, conversation_id=conversation.id, role=role, body=body)
        )
        imported += 1
    if turns:
        last_body = (turns[-1].get("content") or "").strip()
        if last_body:
            conversation.last_message_preview = last_body[:280]
    return imported


@click.command("legacy-import")
@click.option(
    "--state-dir",
    default=".",
    help="Directory containing the legacy usage.txt / sessions.txt / users.txt / "
    "conversation_history.json files (the old STATE_DIR).",
)
@click.option("--dry-run", is_flag=True, help="Report what would be imported without writing.")
@with_appcontext
def legacy_import_command(state_dir: str, dry_run: bool) -> None:
    """Import legacy flat-file state into the Test Business tenant."""
    base = Path(state_dir)
    if not base.exists():
        raise click.ClickException(f"State directory not found: {base}")

    tenant = _get_test_tenant()

    sessions = _parse_sessions(base / "sessions.txt")
    known_users = _parse_users(base / "users.txt")
    history = _parse_history(base / "conversation_history.json")

    senders = set(sessions) | set(known_users) | set(history)
    if not senders:
        click.echo("Nothing to import: no session, user or history data found.")
        return

    click.echo(f"Found {len(senders)} unique sender(s) across the legacy files.")
    if dry_run:
        for sender in sorted(senders):
            turn_count = len(history.get(sender, []))
            click.echo(f"  would import: {sender}  ({turn_count} message turns)")
        click.echo("Dry run — no changes written.")
        return

    channels = {
        "whatsapp": _get_or_create_placeholder_channel(tenant, "whatsapp"),
        "voice": _get_or_create_placeholder_channel(tenant, "voice"),
    }

    customers_created = conversations_created = messages_created = 0
    for sender in sorted(senders):
        kind = _session_channel_kind(sender)
        customer = None
        if kind == "whatsapp":
            before = Customer.query.filter_by(
                tenant_id=tenant.id, phone=sender.replace("whatsapp:", "")
            ).count()
            customer = _import_customer(tenant, sender, sessions.get(sender))
            customers_created += 0 if before else 1

        before_conv = Conversation.query.filter_by(
            tenant_id=tenant.id, session_key=sender
        ).count()
        conversation = _import_conversation(tenant, channels[kind], sender, customer)
        conversations_created += 0 if before_conv else 1

        messages_created += _import_messages(tenant, conversation, history.get(sender, []))

    db.session.commit()
    click.echo(
        f"Imported into '{tenant.name}': "
        f"{customers_created} new customer(s), "
        f"{conversations_created} new conversation(s), "
        f"{messages_created} new message(s)."
    )
    click.echo(
        "All imported records are flagged is_test_data=True and are excluded "
        "from production analytics and reporting."
    )
