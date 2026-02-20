from flask import Flask, request
from dotenv import load_dotenv
load_dotenv()

from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
from datetime import datetime
import os.path

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

app = Flask(__name__)
BOT_ACTIVE = True

OWNER = "whatsapp:+26771298601"

from twilio.rest import Client as TwilioClient

twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

@app.route("/")
def health():
    return "AI Receptionist is running"

@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp():
    resp = MessagingResponse()
    print("🔥 WHATSAPP HIT RECEIVED")
    print("🚀 VERSION: USAGE COUNTER V2")
    global BOT_ACTIVE

    incoming = request.values.get("Body", "")
    sender = request.values.get("From", "")
    text = incoming.lower()
 
    # BASIC Plan Conversation Limit (500 per month)

    from datetime import datetime, timedelta

    if not os.path.exists("usage.txt"):
        with open("usage.txt", "w") as f:
            f.write("0")

    if not os.path.exists("sessions.txt"):
        open("sessions.txt", "w").close()

    # Read current usage
    with open("usage.txt", "r") as f:
        raw = f.read().strip()
         if raw == "":
             raw = "0"
        count = int(raw)

    # Read sessions
    with open("sessions.txt", "r") as f:
        sessions = f.readlines()

    now = datetime.now()
    new_conversation = True

    updated_sessions = []

    for line in sessions:
        saved_sender, saved_time = line.strip().split("|")
        saved_time = datetime.fromisoformat(saved_time)

        if saved_sender == sender:
            if now - saved_time < timedelta(hours=24):
                new_conversation = False
            else:
                new_conversation = True
            updated_sessions.append(f"{sender}|{now.isoformat()}\n")
        else:
            updated_sessions.append(line)

    if sender not in [line.split("|")[0] for line in sessions]:
        updated_sessions.append(f"{sender}|{now.isoformat()}\n")

    # Save updated sessions
    with open("sessions.txt", "w") as f:
        f.writelines(updated_sessions)

    # Only increase count if it's a new 24hr conversation
    if new_conversation:

        if count >= 500:
            try:
                twilio_client.messages.create(
                    body="⚠️ BASIC plan limit reached (500 conversations). Please upgrade client.",
                    from_="whatsapp:+14155238886",
                    to=OWNER
             )
            except Exception as e:
                print("Owner notification failed:", e)

            print("⚠️ LIMIT REACHED - OWNER NOTIFIED")

        resp = MessagingResponse()
        resp.message("You have reached your monthly conversation limit. Please upgrade your plan.")
        return str(resp)

    # Increase count only if under limit
    count += 1
    print("✅ NEW COUNT:", count)

    with open("usage.txt", "w") as f:
        f.write(str(count))
    # Owner controls
    if sender == OWNER and text == "/off":
        BOT_ACTIVE = False
        resp = MessagingResponse()
        resp.message("Bot turned OFF")
        return str(resp)

    if sender == OWNER and text == "/on":
        BOT_ACTIVE = True
        resp = MessagingResponse()
        resp.message("Bot turned ON")
        return str(resp)

    if not BOT_ACTIVE:
        resp = MessagingResponse()
        return str(resp)

    # Create users file if missing
    if not os.path.exists("users.txt"):
        open("users.txt", "w").close()

    with open("users.txt", "r") as f:
        users = f.read().splitlines()

    # First time welcome
    if sender not in users:
        with open("users.txt", "a") as f:
            f.write(sender + "\n")

        resp = MessagingResponse()
        resp.message(
            "Hi 👋 Thank you for contacting 10by20@FNB World of Golf! How can I help you 😊 "
        )
        return str(resp)

    # Greeting
    if text in ["hi", "hello", "hey"]:
        resp = MessagingResponse()
        resp.message("Hi 👋 How can I help you today?")
        return str(resp)

    # Booking shortcut
    if "book" in text:
        resp = MessagingResponse()
        resp.message(
            "To make a booking, please visit: https://bluetree.playbypoint.com\n\n"
            "Let us know if you need anything else 🙂"
        )
        return str(resp)

    # Prices
    if any(word in text for word in ["price", "rates", "cost", "how much", "fee", "court price"]):
        resp = MessagingResponse()
        resp.message(
            "🎾 Court Rates:\n\n"
            "Weekdays:\n"
            "07:00–09:00 P260/hr\n"
            "09:00–16:00 P120/hr\n"
            "16:00–18:00 P260/hr\n"
            "18:00–21:00 P340/hr\n\n"
            "Weekends:\n"
            "07:00–18:00 P260/hr\n"
            "18:00–21:00 P340/hr\n\n"
            "🎾 Racket Rental:\nP50 per person"
            "To secure your preferred time, book here:\nhttps://bluetree.playbypoint.com"
        )
        return str(resp)

    # Location
    if "location" in text or "where" in text:
        resp = MessagingResponse()
        resp.message("📍 We are located at FNB World of Golf@ Bluetree, Maruapula.")
        return str(resp)

    # Walk-ins
    if "walk" in text:
        resp = MessagingResponse()
        resp.message("Yes, walk-ins are welcome (subject to court availability).")
        return str(resp)

    # Payment
    if "payment" in text or "pay" in text:
        resp = MessagingResponse()
        resp.message("We accept EFT and card swipe.")
        return str(resp)

    # Opening hours
    if "hours" in text or "open" in text or "closing" in text:
        resp = MessagingResponse()
        resp.message(
           "We are open daily from 0700 to 2100.\n\n"
           "You can book your session here:\nhttps://bluetree.playbypoint.com"
    )
    return str(resp)

    # Human escalation
    if any(word in text for word in
    ["manager", "human", "call", "person"]):
        try:
            twilio_client.messages.create(
                body=f"Escalation Request:\nFrom: {sender}\nMessage: {incoming}",
                from_="whatsapp:+14155238886",
                to=OWNER
            )
        except Exception as e:
            print("Escalation failed:", e)

        resp = MessagingResponse()
        resp.message("Thank you. A team member will contact you shortly.")
        return str(resp)

    # Log user message
    with open("logs.txt", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()} | USER: {incoming}\n")
    
    # OpenAI fallback
    response = client.responses.create(
    model="gpt-4o-mini",
    input=[
        {
            "role": "system",
            "content": """
    You are the official WhatsApp receptionist for 10by20 Padel Club located at FNB World of Golf @ Bluetree, Maruapula.

    Your job:
    - Help customers book courts
    - Provide pricing information
    - Share opening hours
    - Explain padel rules, scoring, equipment, and benefits
    - Answer questions about the club and facilities

    STRICT RULES:
    - Only answer questions related to padel or 10by20 Padel Club.
    - If a question is unrelated (politics, weather, world news, crypto, coding, general knowledge, etc.), politely redirect the conversation back to the club.
    - Do NOT answer unrelated questions.
    - Keep responses under 2 sentences.
    - Be friendly, confident, and professional.
    - Encourage bookings when appropriate.
    - Never mention that you are an AI.
    """
             },
             {
                 "role": "user",
                 "content": incoming
            }
         ]
    )

    reply = response.output_text
    
    resp = MessagingResponse()
    resp.message(reply)

    # Log bot reply
    with open("logs.txt", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()} | BOT: {reply}\n\n")


    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)




