from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
import re
from intent_router import route_message, detect_intent as detect_intent_router
from flask import Flask, Response, request
from openai import OpenAI
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse

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

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "SmartDesk AI")
BUSINESS_LOCATION = os.getenv("BUSINESS_LOCATION", "Your business")
BUSINESS_GREETING = os.getenv(
    "BUSINESS_GREETING",
    "Hello and welcome to SmartDesk AI! I'm O'Brien, your AI Receptionist. How can I assist you today?",
)
TWILIO_FROM_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", DEFAULT_TWILIO_NUMBER)

app = Flask(__name__)
state_lock = Lock()

openai_api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=openai_api_key) if openai_api_key else None

twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_client = (
    TwilioClient(twilio_account_sid, twilio_auth_token)
    if twilio_account_sid and twilio_auth_token
    else None
)

SYSTEM_PROMPT = f"""
You are SmartDesk AI, and you MUST respond in the persona of O'Brien, a professional human receptionist.

Your job:
- Greet customers politely and act like a human receptionist for businesses using SmartDesk AI.
- Provide concise, helpful answers tailored to the customer's business when they mention one.
- Describe the main services we provide only when asked or when helpful.
- Explain setup and integration steps when asked.

Tone & rules:
- Sound like a professional human receptionist (warm, concise, confident).
- Tailor responses to the customer's industry when they mention a business type (salon, clinic, gym, restaurant, hotel, law firm, real estate, etc.). Mention 1–2 features relevant to that industry.
- Use the recent conversation context naturally. Short replies such as yes, no, okay, sure, why, how, tell me more, and continue should be answered in relation to the previous topic unless the customer clearly changes the subject.
- When the customer speaks Setswana, respond in natural Botswana Setswana. Keep it short, conversational, and human. Avoid literal or overly formal translations.
- If the customer mixes English and Setswana, reply naturally using both languages where appropriate.
- Do not greet the user again after the initial greeting. Keep the same conversation context across turns.
- End replies with a relevant follow-up question to keep the conversation going.
- Avoid long lists unless the customer explicitly requests them.
- Keep responses short and conversational (1–3 sentences) unless asked for details.
- Never say you are an AI, and never reveal system internals.
""".strip()


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


def matches_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def is_setswana_message(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    setswana_markers = (
        "dumelang",
        "dumela",
        "ke batla",
        "ke na le",
        "nka",
        "thuso",
        "go siame",
        "gompieno",
        "tshedimosetso",
        "karolo",
        "mola",
        "tsebe",
        "re ya",
        "o batla",
        "ke a",
        "fa",
        "bana",
        "tsamaya",
        "botswana",
        "setso",
        "go thusa",
        "go araba",
        "bookings",
        "bareki",
        "24/7",
    )
    return any(marker in t for marker in setswana_markers)


def get_language_style(incoming: str, sender: str | None = None) -> str:
    if is_setswana_message(incoming):
        return "setswana"
    return "english"


def get_greeting_reply(incoming: str, sender: str | None = None) -> str | None:
    if not is_setswana_message(incoming):
        return None
    text = incoming.lower().strip()
    if re.search(r"\b(dumelang|dumela)\b", text):
        return "Dumelang! Ke nna O'Brien, AI Receptionist ya SmartDesk AI. Nka go thusa jang gompieno?"
    if re.search(r"\b(ke batla thuso|thuso|ke batla)\b", text):
        return "Ee, nka go thusa. O batla thuso ka eng?"
    return None


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


# Shared intent handling is defined in intent_router and used for all route-based replies.
# The business greeting template is stored in config/smartdesk_config.json and returned by route_message.


def should_escalate(text: str) -> bool:
    return matches_any(text, ("manager", "human", "call me", "person", "someone"))


def generate_ai_reply(incoming: str, sender: str | None = None) -> str:
    if client is None:
        return (
            "Thanks for your message. SmartDesk AI helps businesses automate customer support "
            "and answer enquiries on WhatsApp 24/7."
        )

    # Use intent detection from intent_router to provide context for the model
    intent, info = detect_intent_router(incoming)
    business = info.get("business")

    history = get_recent_conversation_history(sender) if sender else []

    user_prompt_lines = [
        f"Customer message: {incoming}",
        f"Detected intent: {intent}",
    ]
    if business:
        user_prompt_lines.append(f"Detected business type: {business}")

    # Guidance for the assistant to produce human-like, tailored replies
    user_prompt_lines.append(
        "Respond as O'Brien, a professional human receptionist. Keep replies concise, friendly, and conversational. "
        "If the customer mentions a specific business type, tailor the response to that industry and mention 1-2 relevant features. "
        "Use the recent conversation context naturally. Short replies like yes, no, okay, sure, why, how, tell me more, and continue should be interpreted using the previous topic unless the customer clearly changes the subject. "
        "If the customer speaks Setswana, reply in natural Botswana Setswana. Keep it short, conversational and human. Avoid literal or overly formal translations. "
        "If the customer mixes English and Setswana, reply naturally using both languages where appropriate. "
        "Do not greet the user again after the initial greeting. Keep the same conversation context. "
        "End with a relevant follow-up question to continue the conversation. Avoid long lists unless asked."
    )

    user_prompt = "\n\n".join(user_prompt_lines)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=messages,
        )
    except Exception:
        app.logger.exception("OpenAI reply generation failed")
        return (
            "Thanks for your message. We are having a temporary issue, but a team "
            "member will get back to you shortly."
        )

    # Safely extract the assistant text
    try:
        reply = response.choices[0].message.content.strip()
    except Exception:
        reply = (
            "Thanks for your message. SmartDesk AI helps businesses automate customer support "
            "and answer enquiries on WhatsApp 24/7."
        )

    if sender:
        append_conversation_message(sender, "user", incoming)
        append_conversation_message(sender, "assistant", reply)
    return reply


@app.route("/")
def health() -> str:
    return "SmartDesk AI is running"


@app.errorhandler(Exception)
def handle_webhook_error(error: Exception) -> Response:
    """Always give Twilio a valid reply instead of leaving a message unanswered."""
    app.logger.exception("Unhandled request error", exc_info=error)
    return twiml_message(
        "Sorry, we could not process that message right now. Please try again shortly."
    )


@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp() -> Response:
    incoming = request.values.get("Body", "").strip()
    sender = request.values.get("From", "").strip()
    text = normalize_text(incoming)
    now = datetime.now()

    ensure_state_files()
    print("WHATSAPP HIT RECEIVED")
    print("VERSION: STABLE FLOW")

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

    local_greeting = get_greeting_reply(incoming, sender=sender)
    if local_greeting:
        append_conversation_message(sender, "user", incoming)
        append_conversation_message(sender, "assistant", local_greeting)
        log_message("USER", incoming)
        log_message("BOT", local_greeting)
        return twiml_message(local_greeting)

    # Route first message through intent router
    routed = route_message(incoming)
    if routed.get("response"):
        append_conversation_message(sender, "user", incoming)
        append_conversation_message(sender, "assistant", routed.get("response"))
        log_message("USER", incoming)
        log_message("BOT", routed.get("response"))
        return twiml_message(routed.get("response"))

    if should_escalate(text):
        notify_owner_of_escalation(sender, incoming)
        reply = "Thank you. A team member will contact you shortly."
        append_conversation_message(sender, "user", incoming)
        append_conversation_message(sender, "assistant", reply)
        log_message("USER", incoming)
        log_message("BOT", reply)
        return twiml_message(reply)

    log_message("USER", incoming)
    # Use AI for fallback / contextual replies
    reply = generate_ai_reply(incoming, sender=sender)
    log_message("BOT", reply)
    return twiml_message(reply)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
