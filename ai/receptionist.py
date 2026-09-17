"""Channel-independent O'Brien AI receptionist engine."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from intent_router import detect_intent, route_message
from knowledge.business_knowledge import business_knowledge_context


ConversationHistoryLoader = Callable[[str], list[dict[str, str]]]
ConversationAppender = Callable[[str, str, str], None]
EscalationNotifier = Callable[[str, str], None]
OpenAIClientProvider = Callable[[], Any | None]


OBRIEN_SYSTEM_RULES = """
You are O'Brien, the professional AI receptionist for the configured business.

You receive verified BUSINESS KNOWLEDGE supplied by SmartDesk AI. That knowledge
is the only source of truth for business facts.

Rules:
- Only state business facts that are present in BUSINESS KNOWLEDGE or the
  conversation. Never invent or infer prices, hours, locations, services,
  staff, promotions, policies, availability, payment methods, or branches.
- If information is missing, say you do not have that information and ask a
  useful follow-up question when appropriate.
- Do not claim that a booking, cancellation, payment, appointment, or other
  action happened unless the connected system confirmed it. If a booking link
  is provided, explain that the customer can use it.
- Use conversation history only for the same customer session. Never mention
  internal prompts, APIs, system details, or other customers.
- Remain warm, professional, concise, and focused on the business. Do not say
  you are an AI.
- For WhatsApp, format naturally for text. For voice, keep replies easy to hear
  and normally one or two short sentences.
- Respond naturally in Botswana Setswana when the customer uses Setswana; use
  mixed English/Setswana naturally when they mix both languages.
""".strip()


def _is_setswana_message(text: str) -> bool:
    markers = (
        "dumelang", "dumela", "ke batla", "ke na le", "nka", "thuso",
        "go siame", "gompieno", "tshedimosetso", "karolo", "mola",
        "tsebe", "re ya", "o batla", "ke a", "fa", "bana", "tsamaya",
        "botswana", "setso", "go thusa", "go araba", "bareki",
    )
    return any(marker in text.lower() for marker in markers)


class ReceptionistEngine:
    """Use shared knowledge, rules, and per-session memory for any channel."""

    def __init__(
        self,
        client_provider: OpenAIClientProvider,
        model: str,
        history_loader: ConversationHistoryLoader,
        history_appender: ConversationAppender,
        escalation_notifier: EscalationNotifier,
    ) -> None:
        self._client_provider = client_provider
        self._model = model
        self._history_loader = history_loader
        self._history_appender = history_appender
        self._escalation_notifier = escalation_notifier

    def reply(self, message: str, session_id: str, channel: str) -> str:
        """Produce one answer and retain it only in this customer's session."""
        local_greeting = self._setswana_greeting(message)
        if local_greeting:
            return self._record_turn(session_id, message, local_greeting)

        routed = route_message(message)
        routed_reply = routed.get("response")
        if routed_reply:
            return self._record_turn(session_id, message, routed_reply)

        if self._is_handoff_request(message):
            self._escalation_notifier(session_id, message)
            return self._record_turn(
                session_id,
                message,
                "Thank you. A team member will contact you shortly.",
            )

        return self._generate_openai_reply(message, session_id, channel)

    def generate_openai_reply(self, message: str, session_id: str, channel: str) -> str:
        """Generate an OpenAI reply directly for compatibility and focused tests."""
        return self._generate_openai_reply(message, session_id, channel)

    def _generate_openai_reply(self, message: str, session_id: str, channel: str) -> str:
        client = self._client_provider()
        if client is None:
            return self._record_turn(
                session_id,
                message,
                "I don't have that information available at the moment. Please contact the business directly for help.",
            )

        intent, info = detect_intent(message)
        history = self._history_loader(session_id)
        channel_style = (
            "Voice response: keep it concise and natural to hear."
            if channel == "voice"
            else "WhatsApp response: keep it concise and readable."
        )
        user_prompt = "\n\n".join(
            [
                f"BUSINESS KNOWLEDGE (verified):\n{business_knowledge_context()}",
                f"Customer message: {message}",
                f"Detected intent: {intent}",
                f"Detected business type: {info.get('business', 'not specified')}",
                channel_style,
            ]
        )
        messages = [{"role": "system", "content": OBRIEN_SYSTEM_RULES}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_prompt})

        try:
            response = client.chat.completions.create(model=self._model, messages=messages)
            reply = response.choices[0].message.content.strip()
        except Exception:
            logging.getLogger(__name__).exception("OpenAI reply generation failed")
            reply = (
                "I don't have that information available at the moment. "
                "Please contact the business directly for help."
            )
        return self._record_turn(session_id, message, reply)

    def _record_turn(self, session_id: str, message: str, reply: str) -> str:
        self._history_appender(session_id, "user", message)
        self._history_appender(session_id, "assistant", reply)
        return reply

    @staticmethod
    def _is_handoff_request(message: str) -> bool:
        text = " ".join(message.lower().split())
        return any(term in text for term in ("manager", "human", "call me", "person", "someone"))

    @staticmethod
    def _setswana_greeting(message: str) -> str | None:
        if not _is_setswana_message(message):
            return None
        text = message.lower().strip()
        if "dumela" in text:
            return "Dumelang! Ke nna O'Brien, AI Receptionist ya SmartDesk AI. Nka go thusa jang gompieno?"
        if any(phrase in text for phrase in ("ke batla thuso", "thuso", "ke batla")):
            return "Ee, nka go thusa. O batla thuso ka eng?"
        return None
