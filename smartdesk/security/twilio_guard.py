"""Twilio webhook signature validation.

Without this, anyone who learns a webhook URL can drive OpenAI spend, inject
turns into a customer's stored conversation, and forge owner commands.  It is
enforced by default and can only be disabled explicitly (the test suite does).
"""

from __future__ import annotations

import functools
import logging

from flask import Response, current_app, request
from twilio.request_validator import RequestValidator

logger = logging.getLogger(__name__)


def _public_url() -> str:
    """The URL Twilio signed, which may differ from what Flask sees.

    Behind a proxy or tunnel, Flask reports the internal scheme/host. Twilio
    signs the public URL, so TWILIO_WEBHOOK_BASE_URL overrides it when set.
    """
    base = (current_app.config.get("TWILIO_WEBHOOK_BASE_URL") or "").rstrip("/")
    if not base:
        return request.url
    return f"{base}{request.full_path.rstrip('?')}"


def validate_twilio_request() -> bool:
    if not current_app.config.get("TWILIO_VALIDATE_SIGNATURE", True):
        return True

    auth_token = current_app.config.get("TWILIO_AUTH_TOKEN")
    if not auth_token:
        logger.error("TWILIO_AUTH_TOKEN is not set; rejecting webhook")
        return False

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    validator = RequestValidator(auth_token)
    return validator.validate(_public_url(), request.form.to_dict(), signature)


def twilio_webhook(view):
    """Reject any request that Twilio did not sign."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not validate_twilio_request():
            logger.warning(
                "Rejected unsigned webhook request to %s from %s",
                request.path,
                request.headers.get("X-Forwarded-For", request.remote_addr),
            )
            return Response("Forbidden", status=403, mimetype="text/plain")
        return view(*args, **kwargs)

    return wrapper
