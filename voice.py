"""Twilio Voice channel integration.

This module owns Voice-specific TwiML and webhook handling.  It receives the
shared AI reply callable from the application so that future voice turns use
the same business knowledge, conversation history, and OpenAI configuration
as WhatsApp instead of creating a second AI implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from flask import Blueprint, Response, request
from twilio.twiml.voice_response import VoiceResponse


VOICE_GREETING = "Hello! Thank you for calling Smart Desk AI. How can I help you today?"
DEFAULT_LISTEN_TIMEOUT_SECONDS = 30
DEFAULT_SPEECH_TIMEOUT_SECONDS = "auto"
DEFAULT_VOICE_LANGUAGE = "en-US"


def voice_twiml(response: VoiceResponse | None = None) -> Response:
    """Return a Twilio Voice-compatible XML response."""
    return Response(
        str(response or VoiceResponse()),
        status=200,
        mimetype="application/xml",
    )


def voice_error_twiml() -> Response:
    """Give callers a clear, valid response if a Voice webhook fails."""
    response = VoiceResponse()
    response.say("Sorry, we could not process your call right now. Please try again shortly.")
    return voice_twiml(response)


class VoiceConversationService:
    """Channel adapter for future voice turns.

    A speech provider can pass a verified transcript to ``reply_to_transcript``
    later.  The injected callable is the existing shared AI service, not a
    Voice-only OpenAI client.
    """

    def __init__(self, ai_reply_service: Callable[[str, str | None], str]) -> None:
        self._ai_reply_service = ai_reply_service

    def reply_to_transcript(self, transcript: str, caller: str) -> str:
        return self._ai_reply_service(transcript, sender=caller)


def create_voice_blueprint(
    ai_reply_service: Callable[[str, str | None], str],
    event_logger: Callable[[str, str], None],
    listen_timeout_seconds: int = DEFAULT_LISTEN_TIMEOUT_SECONDS,
    speech_timeout_seconds: str = DEFAULT_SPEECH_TIMEOUT_SECONDS,
    voice_language: str = DEFAULT_VOICE_LANGUAGE,
) -> Blueprint:
    """Create the Voice webhook module using the application's shared services."""
    voice = Blueprint("voice", __name__)
    conversation_service = VoiceConversationService(ai_reply_service)
    voice.conversation_service = conversation_service  # type: ignore[attr-defined]
    timeout = max(1, min(listen_timeout_seconds, 60))

    def gather_speech(response: VoiceResponse, prompt: str) -> None:
        """Ask for one spoken turn and send Twilio's transcript to the callback."""
        gather = response.gather(
            input="speech",
            timeout=timeout,
            speech_timeout=speech_timeout_seconds,
            language=voice_language,
            action="/voice/continue",
            method="POST",
            action_on_empty_result=True,
        )
        gather.say(prompt, language=voice_language)

    @voice.route("/voice", methods=["GET", "POST"])
    def answer_call() -> Response:
        call_sid = request.values.get("CallSid", "unknown")
        caller = request.values.get("From", "unknown")
        logging.getLogger(__name__).info(
            "Inbound Voice webhook received: call_sid=%s caller_present=%s",
            call_sid,
            caller != "unknown",
        )
        event_logger("VOICE", f"Inbound call received: call_sid={call_sid}")

        response = VoiceResponse()
        gather_speech(response, VOICE_GREETING)
        return voice_twiml(response)

    @voice.route("/voice/continue", methods=["POST"])
    def continue_call() -> Response:
        call_sid = request.values.get("CallSid", "unknown")
        caller = request.values.get("From", "") or f"voice:{call_sid}"
        transcript = request.values.get("SpeechResult", "").strip()
        logging.getLogger(__name__).info(
            "Voice interaction received: call_sid=%s transcript_present=%s",
            call_sid,
            bool(transcript),
        )
        event_logger("VOICE", f"Voice interaction received: call_sid={call_sid}")

        response = VoiceResponse()
        if not transcript:
            gather_speech(
                response,
                "I'm sorry, I didn't catch that. Please say that again.",
            )
            return voice_twiml(response)

        event_logger("VOICE_CALLER", transcript)
        reply = conversation_service.reply_to_transcript(transcript, caller)
        event_logger("VOICE_ASSISTANT", reply)
        response.say(reply, language=voice_language)
        gather_speech(response, "Is there anything else I can help you with?")
        return voice_twiml(response)

    return voice
