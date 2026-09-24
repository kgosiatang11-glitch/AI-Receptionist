"""Application factory for the SmartDesk AI platform.

Two run modes, chosen by whether ``DATABASE_URL`` is set:

* **Platform mode** (DATABASE_URL present) — multi-tenant. Webhooks resolve a
  tenant from the destination number; the Control Center API is mounted.
* **Legacy mode** (no DATABASE_URL) — the original single-tenant file-backed
  behaviour, unchanged. This exists so that deploying this branch cannot take
  the current production receptionist down before Supabase is provisioned,
  and so the original test suite keeps passing.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

from smartdesk.config import Config, TestConfig
from smartdesk.extensions import db, migrate
from smartdesk.security.jwt_auth import AuthError

load_dotenv()

logger = logging.getLogger(__name__)


def create_app(config_object=None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object or Config)

    database_enabled = bool(app.config.get("SQLALCHEMY_DATABASE_URI"))
    app.config["PLATFORM_MODE"] = database_enabled

    if not database_enabled:
        # Guard rail: the factory only builds the multi-tenant app. Legacy
        # mode is served by the original, untouched app.py via wsgi.py.
        raise RuntimeError(
            "DATABASE_URL is required to build the platform app. "
            "Without it, wsgi.py serves the original single-tenant app."
        )

    db.init_app(app)
    migrate.init_app(app, db)
    _register_platform(app)
    _register_common(app)

    from smartdesk.legacy_import import legacy_import_command
    from smartdesk.seeds import grant_command, seed_command

    app.cli.add_command(seed_command)
    app.cli.add_command(grant_command)
    app.cli.add_command(legacy_import_command)
    return app


def _register_platform(app: Flask) -> None:
    from smartdesk.api.admin_api import admin_api
    from smartdesk.api.calendar_api import calendar_api, calendar_oauth
    from smartdesk.api.config_api import config_api
    from smartdesk.api.dashboard import dashboard_api
    from smartdesk.channels.webhooks import webhooks

    app.register_blueprint(webhooks)
    app.register_blueprint(dashboard_api, url_prefix="/api/v1")
    app.register_blueprint(config_api, url_prefix="/api/v1")
    app.register_blueprint(calendar_api, url_prefix="/api/v1")
    app.register_blueprint(admin_api, url_prefix="/api/v1")
    app.register_blueprint(calendar_oauth)  # no prefix: Google's redirect target

    @app.teardown_request
    def _rollback_on_error(exc):
        if exc is not None:
            db.session.rollback()

    _register_cors(app)
    logger.info("SmartDesk AI started in PLATFORM (multi-tenant) mode")


def _register_cors(app: Flask) -> None:
    """Allow only the configured dashboard origins to call the API."""
    allowed = set(app.config.get("DASHBOARD_ORIGINS", ()))

    @app.after_request
    def _cors(response: Response) -> Response:
        origin = request.headers.get("Origin")
        if origin and origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-Tenant-Id"
            )
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PATCH, PUT, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Max-Age"] = "600"
        return response

    @app.route("/api/v1/<path:_unused>", methods=["OPTIONS"])
    def _preflight(_unused):
        return Response(status=204)


def _register_common(app: Flask) -> None:
    @app.get("/")
    def health():
        return jsonify(
            {
                "service": "SmartDesk AI",
                "status": "ok",
                "mode": "platform",
            }
        )

    @app.errorhandler(AuthError)
    def _auth_error(error: AuthError):
        # Never leak whether a tenant exists; the message is already generic.
        return jsonify({"error": error.message}), error.status

    @app.errorhandler(404)
    def _not_found(_error):
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(Exception)
    def _unhandled(error: Exception):
        app.logger.exception("Unhandled request error", exc_info=error)
        if app.config.get("SQLALCHEMY_DATABASE_URI"):
            db.session.rollback()

        # Twilio webhooks must still receive valid TwiML, otherwise the
        # customer's message goes unanswered.
        if request.path.startswith("/voice"):
            from twilio.twiml.voice_response import VoiceResponse

            response = VoiceResponse()
            response.say(
                "Sorry, we could not process your call right now. "
                "Please try again shortly."
            )
            return Response(str(response), status=200, mimetype="application/xml")
        if request.path.startswith("/whatsapp"):
            from twilio.twiml.messaging_response import MessagingResponse

            response = MessagingResponse()
            response.message(
                "Sorry, we could not process that message right now. "
                "Please try again shortly."
            )
            return Response(str(response), status=200, mimetype="application/xml")

        # API callers get a real status code rather than a masked 200.
        return jsonify({"error": "Internal server error"}), 500


def create_test_app() -> Flask:
    return create_app(TestConfig)
