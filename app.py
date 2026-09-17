from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from ai.receptionist import ReceptionistEngine
from flask import Flask, Response, request
from openai import OpenAI
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from voice import create_voice_blueprint, voice_error_twiml

load_dotenv()

TIMEZONE = os.getenv("BUSINESS_TIMEZONE", "Africa/Gaborone")
DEFAULT_TWILIO_NUMBER = "whatsapp:+14155238886"

STATE_DIR = Path(os.getenv("STATE_DIR", "."))
USAGE_FILE = STATE_DIR / "usage.txt"
SESSIONS_FILE = STATE_DIR / "sessions.txt"
USERS_FILE = STATE_DIR / "users.txt"
LOG_FILE = STATE_DIR / "logs.txt"
BOT_STATE_FILE = STATE_DIR / "bot_state.txt"
CONVERSATION_HISTORY_FILE = STATE_DIR / "conversation_history.json"
MAX_CONVERSATION_HISTORY = 20

OWNER = os.getenv("OWNER_WHATSAPP", "whatsapp:+26771298601")
MONTHLY_CONVERSATION_LIMIT = int(os.getenv("MONTHLY_CONVERSATION_LIMIT", "500"))
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "10"))

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "SmartDesk AI")
BUSINESS_LOCATION = os.getenv("BUSINESS_LOCATION", "Your business")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", DEFAULT_TWILIO_NUMBER)
VOICE_LISTEN_TIMEOUT_SECONDS = int(os.getenv("VOICE_LISTEN_TIMEOUT_SECONDS", "30"))
VOICE_SPEECH_TIMEOUT_SECONDS = os.getenv("VOICE_SPEECH_TIMEOUT_SECONDS", "auto")
TWILIO_VOICE_LANGUAGE = os.getenv("TWILIO_VOICE_LANGUAGE", "en-US")
VOICE_MAX_SILENCE_REPROMPTS = int(os.getenv("VOICE_MAX_SILENCE_REPROMPTS", "2"))

app = Flask(__name__)
state_lock = Lock()

openai_api_key = os.getenv("OPENAI_API_KEY")
client = (
    OpenAI(
        api_key=openai_api_key,
        timeout=OPENAI_TIMEOUT_SECONDS,
        max_retries=0,
    )
    if openai_api_key
    else None
)

twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_client = (
    TwilioClient(twilio_account_sid, twilio_auth_token)
    if twilio_account_sid and twilio_auth_token
    else None
)

def ensure_state_files() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    defaults = {
        USAGE_FILE: "0",
        SESSIONS_FILE: "",
        USERS_FILE: "",
        LOG_FILE: "",
        BOT_STATE_FILE: "on",
    }

    for path, default_content in defaults.items():
        if not path.exists():
            path.write_text(default_content, encoding="utf-8")


def read_usage_count() -> int:
    raw_value = USAGE_FILE.read_text(encoding="utf-8").strip()
    return int(raw_value or "0")


def write_usage_count(count: int) -> None:
    USAGE_FILE.write_text(str(count), encoding="utf-8")


def load_sessions() -> dict[str, datetime]:
    sessions: dict[str, datetime] = {}
    for line in SESSIONS_FILE.read_text(encoding="utf-8").splitlines():
        if "|" not in line:
            continue
        sender, saved_time = line.split("|", 1)
        try:
            sessions[sender] = datetime.fromisoformat(saved_time)
        except ValueError:
            continue
    return sessions


def save_sessions(sessions: dict[str, datetime]) -> None:
    lines = [
        f"{sender}|{saved_time.isoformat()}"
        for sender, saved_time in sorted(sessions.items())
    ]
    content = "\n".join(lines)
    if content:
        content += "\n"
    SESSIONS_FILE.write_text(content, encoding="utf-8")


def is_bot_active() -> bool:
    state = BOT_STATE_FILE.read_text(encoding="utf-8").strip().lower()
    return state != "off"


def set_bot_active(active: bool) -> None:
    BOT_STATE_FILE.write_text("on" if active else "off", encoding="utf-8")


def register_conversation(sender: str, now: datetime) -> tuple[bool, int]:
    with state_lock:
        ensure_state_files()
        sessions = load_sessions()
        last_seen = sessions.get(sender)
        is_new_conversation = last_seen is None or now - last_seen >= timedelta(hours=24)
        sessions[sender] = now
        save_sessions(sessions)

        count = read_usage_count()
        if is_new_conversation:
            count += 1
            write_usage_count(count)
        return is_new_conversation, count


def user_exists(sender: str) -> bool:
    users = USERS_FILE.read_text(encoding="utf-8").splitlines()
    return sender in users


def add_user(sender: str) -> None:
    with state_lock:
        ensure_state_files()
        users = USERS_FILE.read_text(encoding="utf-8").splitlines()
        if sender not in users:
            USERS_FILE.write_text(
                "".join(f"{user}\n" for user in [*users, sender]), encoding="utf-8"
            )


def log_message(source: str, message: str) -> None:
    with state_lock:
        ensure_state_files()
        timestamp = datetime.now().isoformat(timespec="seconds")
        with LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{timestamp} | {source}: {message}\n")


def load_conversation_history(sender: str) -> list[dict[str, str]]:
    if not sender:
        return []
    ensure_state_files()
    if not CONVERSATION_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(CONVERSATION_HISTORY_FILE.read_text(encoding="utf-8"))
    except (ValueError, TypeError):
        return []
    history = data.get(sender, []) if isinstance(data, dict) else []
    return [item for item in history if isinstance(item, dict)]


def save_conversation_history(sender: str, history: list[dict[str, str]]) -> None:
    if not sender:
        return
    ensure_state_files()
    try:
        data = json.loads(CONVERSATION_HISTORY_FILE.read_text(encoding="utf-8")) if CONVERSATION_HISTORY_FILE.exists() else {}
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[sender] = history[-MAX_CONVERSATION_HISTORY:]
    CONVERSATION_HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_conversation_message(sender: str, role: str, content: str) -> None:
    if not sender or not content:
        return
    with state_lock:
        history = load_conversation_history(sender)
        message = {"role": role, "content": str(content).strip()}
        if history and history[-1].get("role") == role and history[-1].get("content") == message["content"]:
            return
        history.append(message)
        save_conversation_history(sender, history)


def get_recent_conversation_history(sender: str) -> list[dict[str, str]]:
    return load_conversation_history(sender)[-MAX_CONVERSATION_HISTORY:]


def twiml_message(body: str = "") -> Response:
    """Return a Twilio-compatible XML response.

    Twilio reads the reply body from TwiML.  Returning an explicit XML content
    type prevents an otherwise valid response being treated as an HTML page by
    a proxy or webhook client.
    """
    response = MessagingResponse()
    if body:
        response.message(body)
    return Response(str(response), status=200, mimetype="application/xml")


def normalize_text(message: str) -> str:
    return " ".join(message.lower().split())


def notify_owner_of_limit(count: int) -> None:
    if not twilio_client:
        return
    try:
        twilio_client.messages.create(
            body=(
                f"Monthly conversation limit reached for {BUSINESS_NAME}. "
                f"Current counted conversations: {count}."
            ),
            from_=TWILIO_FROM_NUMBER,
            to=OWNER,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        print("Owner notification failed:", exc)


def notify_owner_of_escalation(sender: str, incoming: str) -> None:
    if not twilio_client:
        return
    try:
        twilio_client.messages.create(
            body=f"Escalation Request:\nFrom: {sender}\nMessage: {incoming}",
            from_=TWILIO_FROM_NUMBER,
            to=OWNER,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        print("Escalation failed:", exc)


def should_limit_conversation(count: int) -> bool:
    return count > MONTHLY_CONVERSATION_LIMIT


receptionist_engine = ReceptionistEngine(
    client_provider=lambda: client,
    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    history_loader=get_recent_conversation_history,
    history_appender=append_conversation_message,
    escalation_notifier=notify_owner_of_escalation,
)


def generate_ai_reply(incoming: str, sender: str | None = None) -> str:
    """Compatibility wrapper for the existing direct OpenAI reply helper."""
    return receptionist_engine.generate_openai_reply(
        incoming,
        sender or "",
        channel="whatsapp",
    )


def build_conversation_reply(incoming: str, sender: str | None = None) -> str:
    """Shared WhatsApp conversation entry point."""
    return receptionist_engine.reply(incoming, sender or "", channel="whatsapp")


def build_voice_conversation_reply(incoming: str, sender: str | None = None) -> str:
    """Shared Voice conversation entry point with voice-specific presentation."""
    return receptionist_engine.reply(incoming, sender or "", channel="voice")


# Voice receives this existing AI function by dependency injection.  Future
# transcription can therefore reuse the same prompt, knowledge, history, and
# OpenAI client as WhatsApp without a separate AI implementation.
app.register_blueprint(
    create_voice_blueprint(
        build_voice_conversation_reply,
        event_logger=log_message,
        listen_timeout_seconds=VOICE_LISTEN_TIMEOUT_SECONDS,
        speech_timeout_seconds=VOICE_SPEECH_TIMEOUT_SECONDS,
        voice_language=TWILIO_VOICE_LANGUAGE,
        max_silence_reprompts=VOICE_MAX_SILENCE_REPROMPTS,
    )
)


@app.route("/")
def health() -> str:
    return "SmartDesk AI is running"


@app.errorhandler(Exception)
def handle_webhook_error(error: Exception) -> Response:
    """Always give Twilio a valid reply instead of leaving a message unanswered."""
    app.logger.exception("Unhandled request error", exc_info=error)
    if request.path.startswith("/voice"):
        return voice_error_twiml()
    return twiml_message(
        "Sorry, we could not process that message right now. Please try again shortly."
    )


@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp() -> Response:
    incoming = request.values.get("Body", "").strip()
    sender = request.values.get("From", "").strip()
    message_sid = request.values.get("MessageSid", "unknown")
    text = normalize_text(incoming)
    now = datetime.now()

    ensure_state_files()
    app.logger.warning(
        "Inbound WhatsApp webhook received: message_sid=%s sender_present=%s body_present=%s",
        message_sid,
        bool(sender),
        bool(incoming),
    )

    if not sender:
        return twiml_message("We could not identify your WhatsApp number. Please try again.")

    if sender == OWNER and text == "/off":
        set_bot_active(False)
        return twiml_message("Bot turned OFF")

    if sender == OWNER and text == "/on":
        set_bot_active(True)
        return twiml_message("Bot turned ON")

    if not is_bot_active():
        return twiml_message()

    is_new_conversation, usage_count = register_conversation(sender, now)
    if is_new_conversation and should_limit_conversation(usage_count):
        notify_owner_of_limit(usage_count)
        return twiml_message(
            "We are temporarily unavailable on WhatsApp right now. "
            "Please contact the business directly for help."
        )

    if not incoming:
        return twiml_message("Please send a message and I will be happy to help.")

    if not user_exists(sender):
        add_user(sender)

    log_message("USER", incoming)
    reply = build_conversation_reply(incoming, sender=sender)
    log_message("BOT", reply)
    return twiml_message(reply)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
