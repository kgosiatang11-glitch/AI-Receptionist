#!/usr/bin/env python3
"""DEVELOPMENT / TEST TOOL ONLY -- do not run against a production database.

Creates two tenants that a real customer installation should never have:

* ``smartdesk``     -- SmartDesk AI's own vendor/demo tenant, with the
                       platform's own sales copy (sales_mode_enabled=True).
* ``test-business``  -- a disposable, clearly-flagged (is_test_data=True)
                       tenant for local manual testing.

Neither of these is created by ``flask seed`` (see ``smartdesk/seeds.py``),
and this script is intentionally NOT registered as a Flask CLI command on
the app, so it can never run as a side effect of a normal deployment. It is
only ever invoked directly:

    DATABASE_URL=postgresql+psycopg2://... python scripts/dev_seed.py

The connection string is read from the ``DATABASE_URL`` environment
variable only -- nothing here hardcodes a host, user, or password, and
nothing in this script prints the connection string.

Numbers are only registered as channels if you supply them via environment
variables (same convention as production config, to avoid accidentally
routing a real customer's number to this demo tenant):

    SMARTDESK_WHATSAPP_NUMBER, SMARTDESK_VOICE_NUMBER
    TEST_WHATSAPP_NUMBER, TEST_VOICE_NUMBER
"""

from __future__ import annotations

import os
import sys

if not os.getenv("DATABASE_URL"):
    sys.exit(
        "DATABASE_URL is not set. Refusing to run: this script must never "
        "guess or default to a database. Set DATABASE_URL to a local "
        "development database and try again."
    )

from smartdesk.app import create_app  # noqa: E402
from smartdesk.config import Config  # noqa: E402
from smartdesk.extensions import db  # noqa: E402
from smartdesk.seeds import seed_tenant  # noqa: E402

# SmartDesk's own sales copy. This is the ONLY tenant that gets it, and it
# only ever exists in a development/demo database via this script.
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

DEV_TENANT_SPECS = [
    {
        "slug": "smartdesk",
        "name": "SmartDesk AI",
        "business_type": "technology",
        "status": "active",
        "is_internal": True,
        "is_test_data": True,
        "knowledge": SMARTDESK_KNOWLEDGE,
        "profile": {
            "greeting": (
                "Hello and welcome to SmartDesk AI! I'm O'Brien, your AI "
                "Receptionist. How can I assist you today?"
            ),
            "voice_greeting": (
                "Hello! Thank you for calling Smart Desk AI. How can I help you today?"
            ),
            # The platform's own demo tenant is the only one that sells.
            "sales_mode_enabled": True,
        },
        "channel_env": {
            "whatsapp": "SMARTDESK_WHATSAPP_NUMBER",
            "voice": "SMARTDESK_VOICE_NUMBER",
        },
    },
    {
        "slug": "test-business",
        "name": "Test Business",
        "business_type": "test",
        "status": "development",
        "is_internal": True,
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


def main() -> None:
    app = create_app(Config)
    with app.app_context():
        for spec in DEV_TENANT_SPECS:
            seed_tenant(spec)
        db.session.commit()
    print(
        "\nDone. Both tenants are flagged is_test_data=True and are excluded "
        "from production reporting. This script is dev/test tooling only -- "
        "never run it against a real customer's DATABASE_URL."
    )


if __name__ == "__main__":
    main()
