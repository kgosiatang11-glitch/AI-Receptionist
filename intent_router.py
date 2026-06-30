from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Tuple, Dict

CONFIG_PATH = Path(__file__).parent / "config" / "smartdesk_config.json"

try:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        CONFIG = json.load(f)
except Exception:
    CONFIG = {}

BUSINESS_TYPES = [
    "salon",
    "gym",
    "restaurant",
    "tattoo",
    "clinic",
    "hotel",
    "law firm",
    "real estate",
    "school",
    "barber",
    "bakery",
    "shop",
]


def detect_intent(text: str) -> Tuple[str, Dict]:
    info: Dict = {}
    t = (text or "").lower()

    # Greetings
    if re.match(r"^(hi|hello|hey|good morning|good afternoon|good evening|ji)\b", t):
        return "greeting", info

    # About / how it works
    if re.search(r"\b(how\b.*\bwork|how does .* work|what is smartdesk|what is the system|tell me about|what do you do)\b", t):
        return "about", info

    # Features (also detect 'services' queries)
    if re.search(r"\b(features|what can you do|capabilit|what do you offer|what are your features|service|services|offer)\b", t):
        return "features", info

    # Pricing
    if re.search(r"\b(price|pricing|cost|how much|plans|subscription)\b", t):
        return "pricing", info

    # Business hours
    if re.search(r"\b(hours|opening hours|open|when do you open|what time)\b", t):
        return "hours", info

    # Compatibility / business-specific questions
    if re.search(r"\b(can|could|does|do|will)\b.*\b(work|be used|be suitable|fit|help)\b.*\bfor\b", t) or re.search(r"\bworks for\b|\bsuitable for\b|\buse for\b", t):
        for b in BUSINESS_TYPES:
            if re.search(rf"\b{re.escape(b)}s?\b", t):
                info["business"] = b
                break
        return "compatibility", info

    return "ai", info


def route_message(text: str) -> Dict[str, Optional[str]]:
    """Route the message to a hardcoded reply if applicable.

    Returns a dict: { 'intent': str, 'response': Optional[str], 'use_ai': bool }
    If 'response' is None and 'use_ai' is True, caller should invoke OpenAI.
    """
    intent, info = detect_intent(text)

    # Greetings
    if intent == "greeting":
        return {"intent": intent, "response": CONFIG.get("greeting"), "use_ai": False}

    if intent == "about":
        about = (
            "SmartDesk AI works in 5 simple steps:\n"
            "1. A business tells us about its services.\n"
            "2. We configure the AI with the business information.\n"
            "3. We connect it to the business's WhatsApp number.\n"
            "4. The AI automatically answers customer questions, provides business information, and assists with bookings 24/7.\n"
            "5. The business saves time and never misses customer enquiries."
        )
        return {"intent": intent, "response": about, "use_ai": False}

    if intent == "features":
        features = CONFIG.get("features", [])
        features_text = "\n".join(f"- {f}" for f in features)
        return {"intent": intent, "response": features_text, "use_ai": False}

    if intent == "pricing":
        return {"intent": intent, "response": CONFIG.get("pricing"), "use_ai": False}

    if intent == "hours":
        return {"intent": intent, "response": CONFIG.get("business_hours"), "use_ai": False}

    if intent == "compatibility":
        business = info.get("business")
        if business:
            resp = (
                f"Absolutely! SmartDesk AI can be customized for {business}s. It can answer customer questions, handle bookings, provide business information, and integrate with your workflows. Would you like to hear how we'd set it up for your {business}?"
            )
            return {"intent": intent, "response": resp, "use_ai": False}
        # generic compatibility
        resp = (
            "Yes — SmartDesk AI is adaptable and can be configured for most businesses (salons, gyms, restaurants, clinics, hotels, and more). Tell me about your business and I can explain how we'd set it up."
        )
        return {"intent": intent, "response": resp, "use_ai": False}

    # default to AI
    return {"intent": intent, "response": None, "use_ai": True}
