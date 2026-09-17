# Smart Desk AI Receptionist

Flask-based O'Brien receptionist with Twilio WhatsApp and Voice channels, one verified business-knowledge source, session memory, and OpenAI language generation.

## What It Does
- Receives inbound WhatsApp messages through a Twilio webhook
- Replies instantly for common intents like pricing, hours, bookings, location, and payment
- Escalates to a human when the user asks for a person or manager
- Gives OpenAI the verified business knowledge and O'Brien rules for natural-language answers without making OpenAI the source of truth
- Tracks first-time visitors and counts new 24-hour conversations against a monthly limit
- Answers incoming Twilio Voice calls, transcribes each spoken turn, and reads a shared-AI reply back to the caller
- Keeps WhatsApp and Voice as channel adapters over the same O'Brien engine
- Uses the same sales policy across WhatsApp and Voice: explain clearly, recommend relevant value, move genuine interest to setup, and hand off immediately when a human is requested

## Architecture

```text
config/smartdesk_config.json (verified facts)
        ↓
knowledge/business_knowledge.py
        ↓
ai/receptionist.py (O'Brien rules + OpenAI + per-session memory)
        ↓
WhatsApp /voice channel adapters
```

- Edit `config/smartdesk_config.json` to change verified services, pricing, hours, greeting, and features.
- Existing `BUSINESS_LOCATION` and `BOOKING_URL` environment values are also included in the verified OpenAI context when configured.
- WhatsApp memory is keyed by its sender number. Voice memory is keyed by `voice:<Twilio CallSid>`, so simultaneous calls cannot share history.
- If information is absent from the knowledge source, O'Brien says it is unavailable instead of inventing an answer. Booking is never claimed as complete unless a connected system confirms it.
- Pricing comes only from the verified knowledge source. O'Brien can offer the next setup step, but never claims a purchase, payment, or installation is complete without a connected system confirmation.

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
VOICE_SPEECH_TIMEOUT_SECONDS=auto
TWILIO_VOICE_LANGUAGE=en-US
VOICE_MAX_SILENCE_REPROMPTS=2
```

## Local Testing
- Start the Flask app with `python app.py`
- Expose it using ngrok or a similar tunnel
- Point the Twilio WhatsApp webhook to `/whatsapp`
- Send messages from WhatsApp and verify the replies in `logs.txt`
- Point the Twilio Voice number's **A call comes in** webhook to `https://your-domain/voice` using `POST`
- Call the number and verify that it reads: "Hello! Thank you for calling Smart Desk AI. How can I help you today?"
- Ask the same verified-fact question through both channels, such as "What time do you close?". The facts should match even when the wording differs.
- For a Voice test, wait briefly after speaking so Twilio can finish the transcription.

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
- Voice captures caller speech with Twilio `<Gather>`, sends its `SpeechResult` to O'Brien, speaks the reply with `<Say>`, then listens for the next turn. After the configured silence retries, it ends the call politely. Booking remains deferred.
