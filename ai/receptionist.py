"""Channel-independent O'Brien AI receptionist engine."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ai.persona import LEGACY_SMARTDESK_PERSONA, TenantPersona
from intent_router import detect_intent, route_message
from knowledge.business_knowledge import business_knowledge_context, get_business_knowledge


ConversationHistoryLoader = Callable[[str], list[dict[str, str]]]
ConversationAppender = Callable[[str, str, str], None]
EscalationNotifier = Callable[[str, str], None]
OpenAIClientProvider = Callable[[], Any | None]
PersonaProvider = Callable[[], TenantPersona]
KnowledgeProvider = Callable[[], str]
#: Dict-shaped knowledge for the deterministic canned-reply path in
#: intent_router.route_message() -- distinct from KnowledgeProvider above,
#: which returns a JSON string for the OpenAI prompt. Both exist because
#: route_message() reads specific dict keys (knowledge.get("greeting"),
#: .get("pricing"), etc.) while the OpenAI prompt just wants prose.
KnowledgeDictProvider = Callable[[], dict]
#: Returns a reply if this message was booking-related, else None to let the
#: normal canned-reply / OpenAI path handle it.
BookingHandler = Callable[[str, str], str | None]


OBRIEN_SYSTEM_RULES = """
You are a professional AI receptionist answering on behalf of one specific
business.

You receive verified BUSINESS KNOWLEDGE for that business. That knowledge is
the only source of truth for business facts.

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
  you are human or misrepresent what you can do.
- Follow the sales path naturally: inform, understand only what is necessary,
  recommend a relevant use, then move genuine interest toward a clear next
  step. Do not turn interest into a long questionnaire.
- When a prospect shows buying intent, confidently offer the next setup step.
  When they are ready to buy, facilitate setup or a team handoff immediately.
  Never pressure them or claim a purchase, payment, installation, or setup was
  completed unless a connected system confirmed it.
- If a customer asks for a human, hand off immediately and do not continue the
  sales pitch.
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
        persona_provider: PersonaProvider | None = None,
        knowledge_provider: KnowledgeProvider | None = None,
        knowledge_dict_provider: KnowledgeDictProvider | None = None,
        booking_handler: BookingHandler | None = None,
    ) -> None:
        self._client_provider = client_provider
        self._model = model
        self._history_loader = history_loader
        self._history_appender = history_appender
        self._escalation_notifier = escalation_notifier
        # Default to the original SmartDesk persona and the JSON knowledge
        # file so that existing single-tenant callers are unaffected.
        self._persona_provider = persona_provider or (lambda: LEGACY_SMARTDESK_PERSONA)
        self._knowledge_provider = knowledge_provider or business_knowledge_context
        # Same legacy-default pattern as the two providers above: when no
        # tenant-aware provider is supplied, fall back to exactly what
        # route_message() already defaulted to internally, so single-tenant
        # callers see no behaviour change.
        self._knowledge_dict_provider = knowledge_dict_provider or get_business_knowledge
        # None by default: existing single-tenant deployments and any test
        # that constructs the engine directly get exactly the old behaviour.
        self._booking_handler = booking_handler

    @property
    def persona(self) -> TenantPersona:
        return self._persona_provider()

    def reply(
        self,
        message: str,
        session_id: str,
        channel: str,
        customer_reference: str | None = None,
    ) -> str:
        """Produce one answer and retain it only in this customer's session."""
        persona = self.persona
        local_greeting = self._setswana_greeting(message, persona)
        if local_greeting:
            return self._record_turn(session_id, message, local_greeting)

        if persona.handoff_enabled and self._is_handoff_request(message):
            self._escalation_notifier(customer_reference or session_id, message)
            return self._record_turn(
                session_id, message, persona.resolved_handoff_message()
            )

        # Buying-intent handling belongs to businesses that actually sell a
        # setup (i.e. SmartDesk itself). A padel club or salon must not route
        # "let's do it" to a sales escalation.
        if persona.sales_mode_enabled and self._is_ready_to_buy(message):
            self._escalation_notifier(customer_reference or session_id, message)
            return self._record_turn(
                session_id, message, persona.resolved_sales_handoff_message()
            )

        # Booking is checked before the canned deterministic replies: a
        # message can be a greeting AND a booking request at once ("Hi, can
        # I book Saturday at 6pm?"), and the canned greeting reply would
        # otherwise short-circuit before booking ever got a chance. The
        # handler itself decides relevance -- it returns None for anything
        # not booking-related, in which case we fall through to the normal
        # canned-reply / AI path exactly as before.
        if self._booking_handler is not None:
            booking_reply = self._booking_handler(message, session_id)
            if booking_reply is not None:
                return self._record_turn(session_id, message, booking_reply)

        routed = route_message(message, knowledge=self._knowledge_dict_provider(), persona=persona)
        routed_reply = routed.get("response")
        if routed_reply:
            return self._record_turn(
                session_id,
                message,
                self._sales_follow_up(routed["intent"], routed_reply, persona),
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
                f"BUSINESS KNOWLEDGE (verified):\n{self._knowledge_provider()}",
                f"Customer message: {message}",
                f"Detected intent: {intent}",
                f"Detected business type: {info.get('business', 'not specified')}",
                channel_style,
            ]
        )
        system_prompt = OBRIEN_SYSTEM_RULES
        suffix = self.persona.system_rules_suffix()
        if suffix:
            system_prompt = f"{system_prompt}\n\n{suffix}"
        messages = [{"role": "system", "content": system_prompt}]
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
    def _is_ready_to_buy(message: str) -> bool:
        text = " ".join(message.lower().split())
        buying_phrases = (
            "i want it", "i want one", "let's do it", "lets do it",
            "get started", "start the setup", "proceed with the setup",
            "set this up for me", "set it up for me", "ready to proceed",
        )
        return any(phrase in text for phrase in buying_phrases)

    @staticmethod
    def _sales_follow_up(intent: str, reply: str, persona: TenantPersona) -> str:
        """Keep verified routed answers concise while offering a direct next step.

        Only applied when the tenant actually sells something. Other tenants
        get the verified answer with no sales tail appended.
        """
        if not persona.sales_mode_enabled:
            return reply
        if intent == "pricing":
            return (
                f"{reply} If you'd like, I can connect you with "
                f"{persona.team_reference} to get your setup started."
            )
        if intent == "about":
            return f"{reply} If you'd like, I can show you how it could work for your business."
        return reply

    @staticmethod
    def _setswana_greeting(message: str, persona: TenantPersona) -> str | None:
        if not _is_setswana_message(message):
            return None
        text = message.lower().strip()
        if "dumela" in text:
            return persona.resolved_setswana_greeting()
        if any(phrase in text for phrase in ("ke batla thuso", "thuso", "ke batla")):
            return "Ee, nka go thusa. O batla thuso ka eng?"
        return None
