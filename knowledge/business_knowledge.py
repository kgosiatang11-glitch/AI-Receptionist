"""Central gateway for the configured business's verified information.

The JSON file is retained as the editable source of truth already used by the
project.  This module is the only interface used by O'Brien and the intent
router, which keeps future per-business knowledge stores straightforward.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "smartdesk_config.json"


def get_business_knowledge() -> dict[str, Any]:
    """Load only verified project configuration and existing environment facts."""
    try:
        knowledge = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        knowledge = {}

    if not isinstance(knowledge, dict):
        knowledge = {}

    # These values already configure the current WhatsApp implementation.  Do
    # not add placeholder defaults: omitted values must remain unknown.
    location = os.getenv("BUSINESS_LOCATION")
    booking_url = os.getenv("BOOKING_URL")
    if location:
        knowledge["location"] = location
    if booking_url:
        knowledge["booking_url"] = booking_url
    return knowledge


def business_knowledge_context() -> str:
    """Format verified facts for O'Brien's OpenAI context without invention."""
    return json.dumps(get_business_knowledge(), ensure_ascii=False, indent=2)
