from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx
import json
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

app = FastAPI(title="Teleron AI Receptionist Webhook")

# ── CONFIG — fill these in ─────────────────────────────────────────────────────
SUPABASE_URL     = "https://fjtngjxvarpboretvrzl.supabase.co"
SUPABASE_KEY     = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZqdG5nanh2YXJwYm9yZXR2cnpsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkzMTExOTIsImV4cCI6MjA5NDg4NzE5Mn0.UuWxjqPX1YRmhPS6qzSUpX9iaJ0_URC8nk8Yvbps374"
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL       = "llama-3.3-70b-versatile"
GMAIL_SENDER     = os.getenv("GMAIL_SENDER", "")      # your gmail address
GMAIL_PASSWORD   = os.getenv("GMAIL_PASSWORD", "")    # gmail app password
NOTIFY_EMAIL     = os.getenv("NOTIFY_EMAIL", "")      # where to send summaries
# ──────────────────────────────────────────────────────────────────────────────


async def groq_analyse(transcript: str, caller_phone: str) -> dict:
    """Send transcript to Groq and get structured summary back."""
    system = (
        "You are an expert HVAC and home services BPO dispatcher AI. "
        "Analyse the call transcript and return ONLY valid JSON, no markdown, no extra text."
    )
    prompt = f"""Analyse this customer service call transcript for an HVAC/home services company.
Caller phone: {caller_phone}

Transcript:
\"\"\"{transcript}\"\"\"

Return ONLY a valid JSON object:
{{
  "customer_name": "extracted full name or Unknown",
  "phone": "extracted phone number or {caller_phone}",
  "address": "extracted full service address or Unknown",
  "problem": "one clear sentence describing the main issue",
  "urgency": "one of: Low / Medium / High / Emergency",
  "service_type": "one of: HVAC / Plumbing / Electrical / Appliance Repair / General Home Service / Unknown",
  "tech_skill": "specific skill the technician needs",
  "sentiment": "one of: Calm / Frustrated / Urgent / Angry / Satisfied / Confused",
  "language": "language the customer spoke in e.g. English, Urdu, Spanish, Arabic",
  "follow_up": ["action item 1", "action item 2", "action item 3"],
  "notes": "any other important details from the call"
}}"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json"
    }
    payload = {
        "model":       GROQ_MODEL,
        "max_tokens":  800,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt}
        ]
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

    # Extract JSON robustly
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                raw = part
                break
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]

    return json.loads(raw)


async def save_job_to_supabase(summary: dict, transcript: str) -> int | None:
    """Insert job into Supabase jobs table and return new job id."""
    job_data = {
        "customer_name":  summary.get("customer_name", "Unknown"),
        "phone":          summary.get("phone", "Unknown"),
        "transcript":     transcript,
        "status":         "Pending Assignment",
        "scheduled_date": str(datetime.now().date()),
        "assigned_tech":  "Unassigned",
        "timestamp":      datetime.now().isoformat(),
        "keywords":       summary.get("address", ""),
        "ai_summary":     json.dumps(summary)
    }
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation"
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/jobs",
            headers=headers,
            json=job_data
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0]["id"] if data else None


def send_email_summary(summary: dict, job_id: int, transcript: str):
    """Send HTML email summary to the dispatch team."""
    if not GMAIL_SENDER or not GMAIL_PASSWORD or not NOTIFY_EMAIL:
        return  # Email not configured — skip silently

    urgency_colors = {
        "Emergency": "#f87171",
        "High":      "#fb923c",
        "Medium":    "#fbbf24",
        "Low":       "#4ade80"
    }
    urgency      = summary.get("urgency", "Unknown")
    urgency_color = urgency_colors.get(urgency, "#94a3b8")

    follow_ups = "".join(
        f"<li style='margin-bottom:6px;color:#334155;'>{item}</li>"
        for item in summary.get("follow_up", [])
    )

    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:600px;margin:0 auto;background:#f8fafc;border-radius:12px;overflow:hidden;">
      <div style="background:#075E54;padding:24px;text-align:center;">
        <h1 style="color:#ffffff;margin:0;font-size:20px;">⚡ Teleron — New AI Call Job</h1>
        <p style="color:#dcf8c6;margin:6px 0 0;font-size:13px;">Job #{job_id} — {datetime.now().strftime('%B %d, %Y at %H:%M')}</p>
      </div>

      <div style="padding:24px;">

        <div style="background:#ffffff;border-radius:10px;padding:18px;margin-bottom:16px;border:1px solid #e2e8f0;">
          <h2 style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin:0 0 12px;">👤 Customer Info</h2>
          <p style="margin:4px 0;color:#1e293b;"><b>Name:</b> {summary.get('customer_name','Unknown')}</p>
          <p style="margin:4px 0;color:#1e293b;"><b>Phone:</b> {summary.get('phone','Unknown')}</p>
          <p style="margin:4px 0;color:#1e293b;"><b>Address:</b> {summary.get('address','Unknown')}</p>
          <p style="margin:4px 0;color:#1e293b;"><b>Language:</b> {summary.get('language','English')}</p>
        </div>

        <div style="background:#ffffff;border-radius:10px;padding:18px;margin-bottom:16px;border-left:4px solid #3b82f6;border:1px solid #e2e8f0;">
          <h2 style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin:0 0 8px;">🔧 Problem Detected</h2>
          <p style="margin:0;color:#1e293b;font-size:15px;line-height:1.5;">{summary.get('problem','Unknown')}</p>
        </div>

        <div style="display:grid;gap:12px;margin-bottom:16px;">
          <div style="background:#ffffff;border-radius:10px;padding:14px;border:1px solid #e2e8f0;">
            <span style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;">Urgency</span><br>
            <span style="display:inline-block;margin-top:6px;background:{urgency_color};color:#000;padding:3px 12px;border-radius:999px;font-size:12px;font-weight:700;">{urgency}</span>
          </div>
          <div style="background:#ffffff;border-radius:10px;padding:14px;border:1px solid #e2e8f0;">
            <span style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;">Service Type</span><br>
            <span style="color:#1e293b;font-weight:600;font-size:14px;">{summary.get('service_type','Unknown')}</span>
          </div>
          <div style="background:#ffffff;border-radius:10px;padding:14px;border:1px solid #e2e8f0;">
            <span style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;">Tech Skill Needed</span><br>
            <span style="color:#1e293b;font-weight:600;font-size:14px;">{summary.get('tech_skill','Unknown')}</span>
          </div>
          <div style="background:#ffffff;border-radius:10px;padding:14px;border:1px solid #e2e8f0;">
            <span style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;">Customer Sentiment</span><br>
            <span style="color:#1e293b;font-weight:600;font-size:14px;">{summary.get('sentiment','Unknown')}</span>
          </div>
        </div>

        <div style="background:#ffffff;border-radius:10px;padding:18px;margin-bottom:16px;border:1px solid #e2e8f0;">
          <h2 style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin:0 0 10px;">✅ Follow-up Actions</h2>
          <ul style="margin:0;padding-left:18px;">{follow_ups}</ul>
        </div>

        <div style="background:#f1f5f9;border-radius:10px;padding:16px;margin-bottom:16px;">
          <h2 style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin:0 0 8px;">📝 Call Transcript</h2>
          <p style="margin:0;color:#475569;font-size:12px;line-height:1.6;white-space:pre-wrap;">{transcript[:1500]}{'...' if len(transcript)>1500 else ''}</p>
        </div>

        <div style="text-align:center;padding:16px;background:#075E54;border-radius:10px;">
          <p style="color:#ffffff;margin:0;font-size:13px;">🚀 This job has been automatically added to your Teleron Dispatch Board</p>
        </div>

      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚡ [{urgency}] New AI Call — {summary.get('customer_name','Unknown')} — Job #{job_id}"
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_SENDER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_SENDER, NOTIFY_EMAIL, msg.as_string())


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "Teleron AI Receptionist Webhook is running ✅"}


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.post("/vapi-webhook")
async def vapi_webhook(request: Request):
    """
    Receives POST from Vapi when a call ends.
    Vapi sends: { message: { type: 'end-of-call-report', transcript, call, ... } }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    message     = body.get("message", body)   # Vapi wraps in 'message'
    msg_type    = message.get("type", "")

    # Only process end-of-call reports
    if msg_type != "end-of-call-report":
        return JSONResponse({"status": "ignored", "type": msg_type})

    transcript   = message.get("transcript", "")
    call         = message.get("call", {})
    caller_phone = call.get("customer", {}).get("number", "Unknown")

    if not transcript.strip():
        return JSONResponse({"status": "skipped", "reason": "empty transcript"})

    # 1. Analyse with Groq
    try:
        summary = await groq_analyse(transcript, caller_phone)
    except Exception as e:
        summary = {
            "customer_name": "Unknown",
            "phone":         caller_phone,
            "address":       "Unknown",
            "problem":       f"Analysis failed: {e}",
            "urgency":       "Medium",
            "service_type":  "Unknown",
            "tech_skill":    "General",
            "sentiment":     "Unknown",
            "language":      "Unknown",
            "follow_up":     ["Manual review required"],
            "notes":         ""
        }

    # 2. Save to Supabase
    try:
        job_id = await save_job_to_supabase(summary, transcript)
    except Exception as e:
        job_id = None
        summary["notes"] = f"DB save failed: {e}"

    # 3. Send email summary
    try:
        send_email_summary(summary, job_id or 0, transcript)
    except Exception:
        pass  # Don't fail the webhook if email fails

    return JSONResponse({
        "status":    "success",
        "job_id":    job_id,
        "customer":  summary.get("customer_name"),
        "urgency":   summary.get("urgency"),
        "language":  summary.get("language")
    })


@app.post("/test-webhook")
async def test_webhook():
    """Test endpoint — simulates a completed call with a sample transcript."""
    fake_transcript = """
    AI: Thank you for calling Teleron Home Services! I'm your AI assistant. How can I help you today?
    Customer: Hi yes my AC is not working at all. It's very hot inside and I have two kids at home.
    AI: I'm so sorry to hear that. Can I get your name please?
    Customer: My name is Sarah Johnson.
    AI: Thank you Sarah. And what is your service address?
    Customer: 4521 Oak Street, Houston, Texas 77001.
    AI: Got it. And how long has the AC been down?
    Customer: Since yesterday evening. It just stopped blowing cold air.
    AI: Understood. This sounds urgent. Is there anything else I should know?
    Customer: No just please send someone as soon as possible. It's really hot.
    AI: Absolutely Sarah. I've logged your emergency request. A dispatcher will call you within 15 minutes to confirm your appointment. Is there anything else?
    Customer: No that's it thank you.
    AI: Thank you for calling Teleron. We'll take care of you right away. Goodbye!
    """
    summary = await groq_analyse(fake_transcript.strip(), "+1-555-000-1234")
    job_id  = await save_job_to_supabase(summary, fake_transcript.strip())
    try:
        send_email_summary(summary, job_id or 0, fake_transcript.strip())
    except Exception:
        pass
    return JSONResponse({"status": "test complete", "job_id": job_id, "summary": summary})