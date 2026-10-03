"""Cost-containment limits for every OpenAI request the receptionist makes.

Each limit bounds ONE dimension of what a single request can cost.  They are
deliberately finite: an unset, malformed, zero, negative or absurdly large
value never produces an unlimited request -- it falls back to the documented
default (and logs a warning so the misconfiguration is visible).

This module has no Flask dependency so the shared engine, the platform config
and the legacy single-tenant app all use the same values and validation.

Limits
------
``RECEPTIONIST_MAX_OUTPUT_TOKENS`` (default 400, ceiling 2000)
    Sent to OpenAI as ``max_completion_tokens``. Caps the model's reply, which
    is the most expensive part of a completion.  400 tokens is roughly 1,600
    characters -- the Twilio WhatsApp body limit -- and the system prompt asks
    for concise replies (voice: one or two short sentences), so a legitimate
    reply never needs more.  Note: if ``OPENAI_MODEL`` is ever switched to a
    reasoning model, reasoning tokens count against this cap; raise it then.

``MAX_CONVERSATION_HISTORY`` (default 20, ceiling 50)
    Maximum number of prior messages sent to the model.  The newest messages
    are kept; the system prompt and the current customer message are always
    sent in addition.  (Existing setting; now validated.)

``RECEPTIONIST_MAX_KNOWLEDGE_CHARS`` (default 12000, ceiling 60000)
    Maximum characters of tenant knowledge placed in the prompt (~3,000
    tokens).  Typical seeded knowledge is a few KB; this only bites when a
    tenant stores an unusually large knowledge base.  Only the text sent to
    the model is cut -- stored knowledge records are never modified.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_MAX_OUTPUT_TOKENS = 400
OUTPUT_TOKENS_CEILING = 2000

DEFAULT_MAX_HISTORY_MESSAGES = 20
HISTORY_MESSAGES_CEILING = 50

DEFAULT_MAX_KNOWLEDGE_CHARS = 12000
KNOWLEDGE_CHARS_CEILING = 60000

# Stale usage-reservation recovery (smartdesk/services/usage.py). A reservation
# is held only while one OpenAI call is in flight (OPENAI_TIMEOUT_SECONDS
# defaults to 10), so 10 minutes is far beyond any legitimate call yet returns
# leaked capacity quickly.  Must stay well above the OpenAI timeout.
DEFAULT_USAGE_RESERVATION_STALE_SECONDS = 600
USAGE_RESERVATION_STALE_SECONDS_CEILING = 86400

KNOWLEDGE_TRUNCATION_NOTICE = "\n[... business knowledge truncated: size limit reached ...]"


def safe_limit(value, default: int, ceiling: int, name: str = "limit") -> int:
    """Return ``value`` as a positive int within ``1..ceiling``, else ``default``.

    ``None`` means "not configured" and silently yields the default.  Anything
    else that is not a whole number in range also yields the default, with a
    warning.  Booleans are rejected (``True`` must not become ``1``).
    """
    if value is None:
        return default
    parsed = None
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            parsed = None
    if parsed is None or parsed < 1 or parsed > ceiling:
        logger.warning(
            "Invalid %s=%r (must be a whole number from 1 to %d); using default %d",
            name, value, ceiling, default,
        )
        return default
    return parsed


def bound_knowledge(text: str, max_chars: int) -> str:
    """Deterministically cap ``text`` at ``max_chars`` characters.

    Over-long text is cut and a short notice appended so the model knows the
    knowledge is incomplete (the system rules tell it to say so rather than
    guess).  The result, notice included, never exceeds ``max_chars``.
    """
    text = text if isinstance(text, str) else str(text)
    if len(text) <= max_chars:
        return text
    notice = KNOWLEDGE_TRUNCATION_NOTICE
    if max_chars <= len(notice):
        return text[:max_chars]
    return text[: max_chars - len(notice)] + notice
