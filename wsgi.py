"""WSGI entrypoint.

Chooses between the multi-tenant platform application and the original
single-tenant application based on whether a database is configured.  This is
what makes the multi-tenant branch safe to deploy before Supabase exists: with
no DATABASE_URL, production keeps running exactly the code it runs today.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

if os.getenv("DATABASE_URL"):
    from smartdesk.app import create_app

    app = create_app()
else:
    logging.getLogger(__name__).warning(
        "DATABASE_URL is not set: serving the legacy single-tenant receptionist. "
        "The Control Center API and multi-tenant routing are unavailable."
    )
    from app import app  # noqa: F401  (original application, unchanged)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
