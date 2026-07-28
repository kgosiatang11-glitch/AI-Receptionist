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
    t = (text or "").lower().strip()

    # Greetings
    if re.match(r"^(hi|hello|hey|good morning|good afternoon|good evening|ji)\b", t):
        return "greeting", info

    # Booking
    if re.search(r"\b(book|booking|appointment|schedule|reserve|reservation)\b", t):
        return "booking", info

    # Business hours
    if re.search(r"\b(hours|opening hours|open|when do you open|what time|working hours)\b", t):
        return "hours", info

    # Pricing
    if re.search(r"\b(price|pricing|cost|how much|plans|subscription|quote|quotation)\b", t):
        return "pricing", info

    # Human handoff (only explicit requests)
    if re.search(r"\b(i want to speak to a human|can someone call me|contact your team|i need customer support|let me speak to sales|speak to sales|talk to a human|speak to customer support|can you connect me to your team|can i speak to sales)\b", t):
        return "human_handoff", info

    # Contact information
    if re.search(r"\b(contact|contact us|reach out|phone number|email|address|location|where are you)\b", t):
        return "contact", info

    # About / how it works
    if re.search(r"\b(what is smartdesk|what is the system|tell me about smartdesk|tell me about it|what do you do|how does smartdesk work|how does smartdesk ai work|how does it work|how it works|how does it work for businesses)\b", t):
        return "about", info

    # Features (only explicit feature requests)
    if re.search(r"\b(show me the features|list your features|what features do you have|what can smartdesk ai do|what features can smartdesk ai do|what are your features|what services do you offer|what services can you provide|what do you offer)\b", t):
        return "features", info

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

    if intent == "human_handoff":
        return {"intent": intent, "response": "Thank you. A team member will contact you shortly.", "use_ai": False}

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
