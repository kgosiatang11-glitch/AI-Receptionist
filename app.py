from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
import re
from intent_router import route_message, detect_intent as detect_intent_router
from flask import Flask, request
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


def twiml_message(body: str) -> str:
    response = MessagingResponse()
    response.message(body)
    return str(response)


def normalize_text(message: str) -> str:
    return " ".join(message.lower().split())


def matches_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


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


def generate_ai_reply(incoming: str) -> str:
    if client is None:
        return (
            "Thanks for your message. SmartDesk AI helps businesses automate customer support "
            "and answer enquiries on WhatsApp 24/7."
        )

    # Use intent detection from intent_router to provide context for the model
    intent, info = detect_intent_router(incoming)
    business = info.get("business")

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
        "End with a relevant follow-up question to continue the conversation. Avoid long lists unless asked."
    )

    user_prompt = "\n\n".join(user_prompt_lines)

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    # Safely extract the assistant text
    try:
        return response.choices[0].message.content.strip()
    except Exception:
        return (
            "Thanks for your message. SmartDesk AI helps businesses automate customer support "
            "and answer enquiries on WhatsApp 24/7."
        )


@app.route("/")
def health() -> str:
    return "SmartDesk AI is running"


@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp() -> str:
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
        return str(MessagingResponse())

    is_new_conversation, usage_count = register_conversation(sender, now)
    if is_new_conversation and should_limit_conversation(usage_count):
        notify_owner_of_limit(usage_count)
        return twiml_message(
            "We are temporarily unavailable on WhatsApp right now. "
            "Please contact the business directly for help."
        )

    if not user_exists(sender):
        add_user(sender)

        # Route first message through intent router
        routed = route_message(incoming)
        if routed.get("response"):
            log_message("USER", incoming)
            log_message("BOT", routed.get("response"))
            return twiml_message(routed.get("response"))

        if should_escalate(text):
            notify_owner_of_escalation(sender, incoming)
            reply = "Thank you. A team member will contact you shortly."
            log_message("USER", incoming)
            log_message("BOT", reply)
            return twiml_message(reply)

        log_message("BOT", BUSINESS_GREETING)
        return twiml_message(BUSINESS_GREETING)

    if not incoming:
        return twiml_message("Please send a message and I will be happy to help.")

    routed = route_message(incoming)
    if routed.get("response"):
        log_message("USER", incoming)
        log_message("BOT", routed.get("response"))
        return twiml_message(routed.get("response"))

    if should_escalate(text):
        notify_owner_of_escalation(sender, incoming)
        reply = "Thank you. A team member will contact you shortly."
        log_message("USER", incoming)
        log_message("BOT", reply)
        return twiml_message(reply)

    log_message("USER", incoming)
    # Use AI for fallback / contextual replies
    reply = generate_ai_reply(incoming)
    log_message("BOT", reply)
    return twiml_message(reply)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
