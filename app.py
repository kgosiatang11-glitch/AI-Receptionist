from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
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

BOOKING_URL = os.getenv("BOOKING_URL", "https://bluetree.playbypoint.com")
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "10by20 Padel Club")
BUSINESS_LOCATION = os.getenv(
    "BUSINESS_LOCATION", "FNB World of Golf @ Bluetree, Maruapula"
)
BUSINESS_GREETING = os.getenv(
    "BUSINESS_GREETING",
    f"Hi! Thank you for contacting {BUSINESS_NAME}. How can I help you today?",
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
You are the official WhatsApp receptionist for {BUSINESS_NAME} located at {BUSINESS_LOCATION}.

Your job:
- Help customers book courts
- Provide pricing information
- Share opening hours
- Explain padel rules, scoring, equipment, and benefits
- Answer questions about the club and facilities

STRICT RULES:
- Only answer questions related to padel or {BUSINESS_NAME}.
- If a question is unrelated (politics, weather, world news, crypto, coding, general knowledge, etc.), politely redirect the conversation back to the club.
- Do NOT answer unrelated questions.
- Keep responses under 2 sentences.
- Be friendly, confident, and professional.
- Encourage bookings when appropriate.
- Never mention that you are an AI.
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


def get_rule_based_reply(text: str) -> str | None:
    if text in {"hi", "hello", "hey"}:
        return "Hi! How can I help you today?"

    if "book" in text or "booking" in text:
        return (
            f"To make a booking, please visit: {BOOKING_URL}\n\n"
            "Let us know if you need anything else."
        )

    if matches_any(text, ("price", "rates", "cost", "how much", "fee", "court price")):
        return (
            "Court Rates:\n\n"
            "Weekdays:\n"
            "07:00-09:00 P260/hr\n"
            "09:00-16:00 P120/hr\n"
            "16:00-18:00 P260/hr\n"
            "18:00-21:00 P340/hr\n\n"
            "Weekends:\n"
            "07:00-18:00 P260/hr\n"
            "18:00-21:00 P340/hr\n\n"
            "Racket Rental:\n"
            "P50 per person\n\n"
            f"To secure your preferred time, book here:\n{BOOKING_URL}"
        )

    if matches_any(text, ("location", "where are you", "where is", "address")):
        return f"We are located at {BUSINESS_LOCATION}."

    if matches_any(text, ("walk-in", "walk in", "walkins", "walk ins")):
        return "Yes, walk-ins are welcome, subject to court availability."

    if matches_any(text, ("payment", "pay", "card", "eft")):
        return "We accept EFT and card payments."

    if matches_any(text, ("hours", "opening hours", "open today", "closing time", "what time")):
        return (
            "We are open daily from 07:00 to 21:00.\n\n"
            f"You can book your session here:\n{BOOKING_URL}"
        )

    return None


def should_escalate(text: str) -> bool:
    return matches_any(text, ("manager", "human", "call me", "person", "someone"))


def generate_ai_reply(incoming: str) -> str:
    if client is None:
        return (
            "Thanks for your message. Our team can help with club questions and bookings, "
            f"and you can book directly here: {BOOKING_URL}"
        )

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": incoming},
        ],
    )
    return response.choices[0].message.content.strip()


@app.route("/")
def health() -> str:
    return "AI Receptionist is running"


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
            "Please contact the club directly for help with your booking."
        )

    if not user_exists(sender):
        add_user(sender)
        log_message("BOT", BUSINESS_GREETING)
        return twiml_message(BUSINESS_GREETING)

    if not incoming:
        return twiml_message("Please send a message and I will be happy to help.")

    rule_based_reply = get_rule_based_reply(text)
    if rule_based_reply:
        log_message("USER", incoming)
        log_message("BOT", rule_based_reply)
        return twiml_message(rule_based_reply)

    if should_escalate(text):
        notify_owner_of_escalation(sender, incoming)
        reply = "Thank you. A team member will contact you shortly."
        log_message("USER", incoming)
        log_message("BOT", reply)
        return twiml_message(reply)

    log_message("USER", incoming)
    reply = generate_ai_reply(incoming)
    log_message("BOT", reply)
    return twiml_message(reply)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
