# Smart Desk AI Receptionist

Flask-based receptionist with Twilio WhatsApp and Voice webhook handling, rule-based answers, human escalation, and a shared OpenAI fallback.

## What It Does
- Receives inbound WhatsApp messages through a Twilio webhook
- Replies instantly for common intents like pricing, hours, bookings, location, and payment
- Escalates to a human when the user asks for a person or manager
- Uses OpenAI as a fallback for club and padel questions
- Tracks first-time visitors and counts new 24-hour conversations against a monthly limit
- Answers incoming Twilio Voice calls with a professional greeting and keeps the call open briefly for the next interaction
- Keeps Voice isolated in `voice.py`, ready to send future verified speech transcripts through the same AI service used by WhatsApp

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Required Environment Variables

```env
OPENAI_API_KEY=...
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
TWILIO_VOICE_NUMBER=+267XXXXXXXX
OWNER_WHATSAPP=whatsapp:+26771298601
```

## Optional Environment Variables

```env
OPENAI_MODEL=gpt-4o-mini
OPENAI_TIMEOUT_SECONDS=10
MONTHLY_CONVERSATION_LIMIT=500
BUSINESS_NAME=10by20 Padel Club
BUSINESS_LOCATION=FNB World of Golf @ Bluetree, Maruapula
BOOKING_URL=https://bluetree.playbypoint.com
BUSINESS_TIMEZONE=Africa/Gaborone
STATE_DIR=.
VOICE_LISTEN_TIMEOUT_SECONDS=30
```

## Local Testing
- Start the Flask app with `python app.py`
- Expose it using ngrok or a similar tunnel
- Point the Twilio WhatsApp webhook to `/whatsapp`
- Send messages from WhatsApp and verify the replies in `logs.txt`
- Point the Twilio Voice number's **A call comes in** webhook to `https://your-domain/voice` using `POST`
- Call the number and verify that it reads: "Hello! Thank you for calling Smart Desk AI. How can I help you today?"

## Runtime Files
- `usage.txt`: monthly counted conversations
- `sessions.txt`: last-seen timestamp per WhatsApp number
- `users.txt`: known users who have already received the welcome message
- `logs.txt`: simple conversation log
- `bot_state.txt`: persisted on/off toggle for owner commands

## Notes
- The app now persists bot on/off state across restarts
- Runtime text files are acceptable for a tiny deployment, but a database is the next upgrade if traffic grows
- `service_account.json` is currently unused by the Flask flow
- Voice currently provides the greeting and a short keypad wait only. Speech recognition, AI audio responses, and booking are intentionally deferred; future voice turns will call the existing shared AI service rather than a duplicate implementation.
