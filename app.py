import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import os
import base64
import json
import requests
import plotly.graph_objects as go
from supabase import create_client, Client
import io
import uuid
import random

# --- 1. PAGE CONFIG ---
st.set_page_config(page_title="Teleron Central Dispatch", page_icon="logo.png", layout="wide")

# --- 2. SUPABASE ---
SUPABASE_URL = "https://fjtngjxvarpboretvrzl.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZqdG5nanh2YXJwYm9yZXR2cnpsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkzMTExOTIsImV4cCI6MjA5NDg4NzE5Mn0.UuWxjqPX1YRmhPS6qzSUpX9iaJ0_URC8nk8Yvbps374"

@st.cache_resource
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)
supabase = get_supabase()

# --- 3. GROQ ---
try:
    GROQ_API_KEY = st.secrets["GROQ_API_KEY"]
    AI_ENABLED = True
except Exception:
    GROQ_API_KEY = None
    AI_ENABLED = False

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"

def groq_chat(messages: list, system: str = None, max_tokens: int = 600) -> str:
    if not AI_ENABLED:
        return "AI not configured."
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload_messages = []
    if system:
        payload_messages.append({"role": "system", "content": str(system)})
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and str(content).strip():
            payload_messages.append({"role": role, "content": str(content)})
    if not any(m["role"] == "user" for m in payload_messages):
        return "No user message provided."
    payload = {"model": GROQ_MODEL, "max_tokens": max_tokens, "temperature": 0.7, "messages": payload_messages}
    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
        if not resp.ok:
            try: err = resp.json().get("error", {}).get("message", resp.text)
            except: err = resp.text
            return f"Groq error: {err}"
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"Connection error: {e}"

def generate_call_summary(transcript: str, customer_name: str, phone: str) -> dict:
    if not AI_ENABLED:
        return {"problem":"AI not configured","urgency":"Unknown","service_type":"Unknown","tech_skill":"General","sentiment":"Neutral","follow_up":["Configure Groq API key"]}
    if not transcript or not transcript.strip():
        return {"problem":"No transcript provided","urgency":"Unknown","service_type":"Unknown","tech_skill":"General","sentiment":"Neutral","follow_up":["Obtain call transcript"]}
    system = "You are an expert HVAC and home services BPO dispatcher AI. Return ONLY valid JSON, no markdown, no explanation."
    user_msg = f"""Analyze this customer call transcript and return a JSON summary.
Customer: {customer_name} | Phone: {phone}
Transcript: \"\"\"{transcript}\"\"\"

Return ONLY valid JSON:
{{"problem":"One clear sentence","urgency":"Low/Medium/High/Emergency","service_type":"HVAC/Plumbing/Electrical/Appliance Repair/General Home Service/Unknown","tech_skill":"skill needed","sentiment":"Calm/Frustrated/Urgent/Angry/Satisfied/Confused","follow_up":["item1","item2","item3"]}}"""
    raw = groq_chat([{"role": "user", "content": user_msg}], system=system, max_tokens=700)
    if raw.startswith("Groq error:") or raw.startswith("Connection error:") or raw.startswith("AI not"):
        return {"problem":raw,"urgency":"Unknown","service_type":"Unknown","tech_skill":"Unknown","sentiment":"Unknown","follow_up":["Check Groq API key"]}
    try:
        clean = raw.strip()
        if "```" in clean:
            parts = clean.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"): part = part[4:].strip()
                if part.startswith("{"): clean = part; break
        start = clean.find("{"); end = clean.rfind("}") + 1
        if start != -1 and end > start: clean = clean[start:end]
        return json.loads(clean)
    except Exception as e:
        return {"problem":f"Summary failed: {e}","urgency":"Unknown","service_type":"Unknown","tech_skill":"Unknown","sentiment":"Unknown","follow_up":["Retry"]}

def save_summary_to_db(job_id, summary):
    try: supabase.table("jobs").update({"ai_summary": json.dumps(summary)}).eq("id", job_id).execute()
    except: pass

def parse_summary(raw):
    if raw is None: return None
    if isinstance(raw, dict): return raw
    if isinstance(raw, str) and raw.strip():
        try: return json.loads(raw)
        except: return None
    return None

def whatsapp_bot_response(user_message: str, chat_history: list) -> str:
    if not AI_ENABLED: return "AI service not configured. Please add your Groq API key."
    system_prompt = """You are Teleron AI Assistant, a professional and friendly virtual dispatcher for Teleron Central Dispatch — an HVAC and home services company.
Help customers with: booking appointments, HVAC/plumbing/electrical questions, pricing guidance, collecting info, escalating emergencies.
Guidelines: Be polite and empathetic. For EMERGENCIES tell them to call 911 if life-threatening, then dispatch immediately.
Collect: customer name, address, problem description. ETAs: Emergency=1-2hrs, High=same day, Normal=next slot.
Keep responses concise for WhatsApp. End bookings saying dispatcher will call within 15 minutes.
Services: HVAC, Plumbing, Electrical, Appliance repair, General home services. Hours: Mon-Sat 7AM-8PM, Emergency 24/7."""
    messages = []
    for msg in chat_history[-10:]:
        role = msg.get("role",""); content = msg.get("content","")
        if role in ("user","assistant") and str(content).strip():
            messages.append({"role":role,"content":str(content)})
    messages.append({"role":"user","content":str(user_message)})
    return groq_chat(messages, system=system_prompt, max_tokens=500)

# --- 4. DB HELPERS ---

# STATUS PIPELINE — ordered list
STATUS_PIPELINE = [
    "Pending Assignment",
    "Assigned", 
    "En Route",
    "Arrived",
    "In Progress",
    "Completed",
    "Invoiced",
    "Paid"
]

def get_next_status(current_status):
    if current_status in STATUS_PIPELINE:
        idx = STATUS_PIPELINE.index(current_status)
        return STATUS_PIPELINE[min(idx + 1, len(STATUS_PIPELINE) - 1)]
    return "Assigned"

def get_prev_status(current_status):
    if current_status in STATUS_PIPELINE:
        idx = STATUS_PIPELINE.index(current_status)
        return STATUS_PIPELINE[max(idx - 1, 0)]
    return "Pending Assignment"

def get_jobs(status_filter=None):
    try:
        query = supabase.table("jobs").select("*").order("id", desc=True)
        if status_filter: query = query.eq("status", status_filter)
        result = query.execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except Exception as e:
        st.error(f"Error fetching jobs: {e}")
        return pd.DataFrame()

def get_job_by_id(job_id):
    try:
        result = supabase.table("jobs").select("*").eq("id", job_id).execute()
        return result.data[0] if result.data else None
    except: return None

def get_technicians(status_filter=None):
    try:
        query = supabase.table("technicians").select("*").order("name")
        if status_filter: query = query.eq("status", status_filter)
        result = query.execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except Exception as e:
        st.error(f"Error fetching technicians: {e}")
        return pd.DataFrame()

def insert_job(data):
    return supabase.table("jobs").insert(data).execute()

def update_job_status(job_id, status):
    # Only update status, don't try to update timestamp columns that may not exist
    return supabase.table("jobs").update({"status": status}).eq("id", job_id).execute()

def update_job_field(job_id, field, value):
    return supabase.table("jobs").update({field: value}).eq("id", job_id).execute()

def delete_job(job_id):
    return supabase.table("jobs").delete().eq("id", job_id).execute()

def insert_technician(data):
    return supabase.table("technicians").insert(data).execute()

def update_tech_status(tech_id, status):
    return supabase.table("technicians").update({"status": status}).eq("id", tech_id).execute()

def delete_technician(tech_id):
    return supabase.table("technicians").delete().eq("id", tech_id).execute()

# --- INVOICE HELPERS ---
def get_invoices():
    try:
        result = supabase.table("invoices").select("*").order("id", desc=True).execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

def get_invoice_by_job(job_id):
    try:
        result = supabase.table("invoices").select("*").eq("job_id", job_id).execute()
        return result.data[0] if result.data else None
    except: return None

def insert_invoice(data):
    try: return supabase.table("invoices").insert(data).execute()
    except Exception as e: return {"error": str(e)}

def update_invoice_status(inv_id, status):
    try: return supabase.table("invoices").update({"status": status}).eq("id", inv_id).execute()
    except Exception as e: return {"error": str(e)}

def delete_invoice(inv_id):
    try: return supabase.table("invoices").delete().eq("id", inv_id).execute()
    except Exception as e: return {"error": str(e)}

# --- CUSTOMER CRM HELPERS ---
def get_customers():
    try:
        result = supabase.table("customers").select("*").order("name").execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

def get_customer_by_phone(phone):
    try:
        result = supabase.table("customers").select("*").eq("phone", phone).execute()
        return result.data[0] if result.data else None
    except: return None

def upsert_customer(data):
    try:
        existing = get_customer_by_phone(data.get("phone"))
        if existing:
            return supabase.table("customers").update(data).eq("phone", data["phone"]).execute()
        return supabase.table("customers").insert(data).execute()
    except Exception as e:
        return {"error": str(e)}

def get_customer_jobs(phone):
    try:
        result = supabase.table("jobs").select("*").eq("phone", phone).order("id", desc=True).execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except: return pd.DataFrame()

def get_customer_invoices(phone):
    try:
        result = supabase.table("invoices").select("*").eq("phone", phone).order("id", desc=True).execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except: return pd.DataFrame()

# --- EQUIPMENT HELPERS ---
def get_equipment(customer_phone=None):
    try:
        query = supabase.table("equipment").select("*")
        if customer_phone: query = query.eq("customer_phone", customer_phone)
        result = query.execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except: return pd.DataFrame()

def insert_equipment(data):
    try: return supabase.table("equipment").insert(data).execute()
    except Exception as e: return {"error": str(e)}

def delete_equipment(eq_id):
    try: return supabase.table("equipment").delete().eq("id", eq_id).execute()
    except: pass

# --- PHOTO / SIGNATURE / NOTE HELPERS ---
def get_job_photos(job_id):
    try:
        result = supabase.table("job_photos").select("*").eq("job_id", job_id).order("id", desc=True).execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except: return pd.DataFrame()

def insert_job_photo(data):
    try: return supabase.table("job_photos").insert(data).execute()
    except Exception as e: return {"error": str(e)}

def get_job_signatures(job_id):
    try:
        result = supabase.table("signatures").select("*").eq("job_id", job_id).execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except: return pd.DataFrame()

def insert_signature(data):
    try: return supabase.table("signatures").insert(data).execute()
    except Exception as e: return {"error": str(e)}

def get_job_notes(job_id):
    try:
        result = supabase.table("job_notes").select("*").eq("job_id", job_id).order("id", desc=True).execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except: return pd.DataFrame()

def insert_job_note(data):
    try: return supabase.table("job_notes").insert(data).execute()
    except Exception as e: return {"error": str(e)}

# --- SMS LOG HELPERS ---
def get_sms_logs(phone=None):
    try:
        query = supabase.table("sms_logs").select("*").order("id", desc=True)
        if phone: query = query.eq("phone", phone)
        result = query.execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except: return pd.DataFrame()

def log_sms(phone, message, status="Queued"):
    try:
        return supabase.table("sms_logs").insert({
            "phone": phone, "message": message, "status": status,
            "timestamp": datetime.now().isoformat()
        }).execute()
    except: pass


# --- 5. PAGE STATE ---
if "page" not in st.session_state:
    st.session_state.page = "home"
if "invoices_local" not in st.session_state:
    st.session_state.invoices_local = []
if "wa_chat" not in st.session_state:
    st.session_state.wa_chat = []
if "selected_customer" not in st.session_state:
    st.session_state.selected_customer = None
if "portal_tech_id" not in st.session_state:
    st.session_state.portal_tech_id = None
if "portal_verified" not in st.session_state:
    st.session_state.portal_verified = False

# --- 6. SHARED CSS ---
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Space+Grotesk:wght@400;500;600;700&display=swap');

*{box-sizing:border-box;}
.stApp{background:#000000;color:#e2e8f0;font-family:'Inter',sans-serif;}
header,footer{visibility:hidden!important;}
.block-container{padding-top:1rem!important;max-width:95%!important;}

::-webkit-scrollbar{width:4px;height:4px;}
::-webkit-scrollbar-track{background:#0a0a0a;}
::-webkit-scrollbar-thumb{background:#1e293b;border-radius:99px;}

.stTextInput>div>div>input,.stTextArea>div>div>textarea,.stSelectbox>div>div>div,.stDateInput>div>div>input,.stNumberInput>div>div>input{background-color:#0a0a0a!important;color:#e2e8f0!important;border:1px solid #1a1a2e!important;border-radius:10px!important;font-family:'Inter',sans-serif!important;font-size:13px!important;padding:10px 14px!important;}
.stTextInput>div>div>input:focus,.stTextArea>div>div>textarea:focus{border-color:#6366f1!important;box-shadow:0 0 0 3px rgba(99,102,241,0.1)!important;}
.stTextInput label,.stTextArea label,.stSelectbox label,.stDateInput label,.stNumberInput label{color:#64748b!important;font-size:11px!important;font-weight:600!important;text-transform:uppercase!important;letter-spacing:1px!important;}

.stButton>button{background:linear-gradient(135deg,#6366f1,#4f46e5)!important;color:#fff!important;border:none!important;border-radius:10px!important;font-family:'Inter',sans-serif!important;font-size:12px!important;font-weight:700!important;padding:10px 20px!important;transition:all 0.2s ease!important;text-transform:uppercase!important;}
.stButton>button:hover{background:linear-gradient(135deg,#7c3aed,#6366f1)!important;transform:translateY(-1px)!important;box-shadow:0 8px 25px rgba(99,102,241,0.35)!important;}

/* ===== METRIC CARDS ===== */
.metric-card{background:#050508;border:1px solid #0f0f1a;border-radius:20px;padding:22px 24px 20px;position:relative;overflow:hidden;transition:all 0.2s ease;cursor:pointer;}
.metric-card:hover{border-color:#6366f1!important;transform:translateY(-3px);box-shadow:0 12px 40px rgba(99,102,241,0.15);}
.metric-card.blue::after{background:#6366f1;}
.metric-card.green::after{background:#10b981;}
.metric-card.amber::after{background:#f59e0b;}
.metric-card.purple::after{background:#8b5cf6;}
.metric-card.red::after{background:#f43f5e;}
.metric-card::after{content:'';position:absolute;top:0;right:0;width:80px;height:80px;border-radius:50%;opacity:0.06;transform:translate(20px,-20px);}
.metric-card-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:18px;}
.metric-icon{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:18px;}
.icon-blue{background:rgba(99,102,241,0.12);color:#818cf8;}
.icon-green{background:rgba(16,185,129,0.12);color:#34d399;}
.icon-amber{background:rgba(245,158,11,0.12);color:#fbbf24;}
.icon-purple{background:rgba(139,92,246,0.12);color:#a78bfa;}
.icon-red{background:rgba(244,63,94,0.12);color:#fb7185;}
.metric-badge{font-size:10px;font-weight:700;padding:4px 10px;border-radius:999px;letter-spacing:0.5px;text-transform:uppercase;}
.badge-blue{background:rgba(99,102,241,0.12);color:#818cf8;}
.badge-green{background:rgba(16,185,129,0.12);color:#34d399;}
.badge-amber{background:rgba(245,158,11,0.12);color:#fbbf24;}
.badge-purple{background:rgba(139,92,246,0.12);color:#a78bfa;}
.badge-red{background:rgba(244,63,94,0.12);color:#fb7185;}
.metric-number{font-family:'Space Grotesk',sans-serif;font-size:42px;font-weight:700;line-height:1;letter-spacing:-2px;color:#f8fafc;}
.metric-title{font-size:10px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:20px;}
.metric-divider{border:none;border-top:1px solid #0f0f1a;margin:0 0 14px;}
.metric-row{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#334155;margin-bottom:8px;}
.metric-row-label{display:flex;align-items:center;gap:7px;}
.metric-row-val{font-weight:700;font-size:12px;color:#94a3b8;}
.metric-dot{width:6px;height:6px;border-radius:50%;display:inline-block;}
.metric-bar-wrap{height:3px;background:#0f0f1a;border-radius:99px;margin-top:16px;overflow:hidden;}
.metric-bar-fill{height:100%;border-radius:99px;}
.metric-hint{font-size:9px;color:#334155;text-align:center;margin-top:10px;}

/* ===== PIPELINE STEPPER ===== */
.pipeline-wrap{display:flex;align-items:center;justify-content:space-between;gap:4px;padding:16px 8px;background:#03030a;border:1px solid #0f0f1a;border-radius:16px;margin:16px 0 20px;overflow-x:auto;}
.pipeline-step{flex:1;text-align:center;position:relative;min-width:80px;}
.pipeline-step-line{position:absolute;top:16px;left:-50%;right:50%;height:2px;background:#1a1a2e;z-index:0;}
.pipeline-step:first-child .pipeline-step-line{display:none;}
.pipeline-step.active .pipeline-step-line{background:linear-gradient(90deg,#6366f1,#4f46e5);}
.pipeline-dot{width:32px;height:32px;border-radius:50%;background:#0a0a0a;border:2px solid #1a1a2e;margin:0 auto 8px;position:relative;z-index:1;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#475569;transition:all 0.3s ease;}
.pipeline-step.done .pipeline-dot{background:linear-gradient(135deg,#10b981,#34d399);border-color:#34d399;color:#fff;}
.pipeline-step.active .pipeline-dot{background:linear-gradient(135deg,#6366f1,#4f46e5);border-color:#818cf8;color:#fff;box-shadow:0 0 15px rgba(99,102,241,0.3);}
.pipeline-step.current .pipeline-dot{background:linear-gradient(135deg,#f59e0b,#fbbf24);border-color:#fbbf24;color:#000;box-shadow:0 0 15px rgba(245,158,11,0.3);animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{box-shadow:0 0 15px rgba(245,158,11,0.3);}50%{box-shadow:0 0 25px rgba(245,158,11,0.5);}}
.pipeline-label{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:#475569;}
.pipeline-step.done .pipeline-label{color:#34d399;}
.pipeline-step.active .pipeline-label{color:#818cf8;}
.pipeline-step.current .pipeline-label{color:#fbbf24;}
.pipeline-time{font-size:9px;color:#1e293b;margin-top:3px;}
.pipeline-step.done .pipeline-time,.pipeline-step.active .pipeline-time,.pipeline-step.current .pipeline-time{color:#334155;}

/* ===== SECTION HEADERS ===== */
.section-header{font-size:11px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:2px;border-bottom:1px solid #0f0f1a;padding-bottom:12px;margin-bottom:20px;display:flex;align-items:center;gap:8px;}
.section-header::before{content:'';display:inline-block;width:3px;height:14px;background:linear-gradient(180deg,#6366f1,#8b5cf6);border-radius:99px;}

/* ===== JOB CARDS ===== */
.job-card{background:#050508;border:1px solid #0f0f1a;border-radius:16px;padding:18px 20px;margin-bottom:12px;transition:border-color 0.2s ease;}
.job-card:hover{border-color:#1e1b4b;}
.job-card-id{font-size:10px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;}
.job-card-name{font-size:15px;font-weight:700;color:#f1f5f9;margin-bottom:4px;}
.job-card-meta{font-size:12px;color:#334155;display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;}
.job-card-meta span{display:flex;align-items:center;gap:5px;}

/* ===== STATUS BADGES ===== */
.status-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;border:1px solid;}
.status-pending{background:rgba(245,158,11,0.08);color:#fbbf24;border-color:rgba(245,158,11,0.2);}
.status-assigned{background:rgba(99,102,241,0.08);color:#818cf8;border-color:rgba(99,102,241,0.2);}
.status-en-route{background:rgba(56,189,248,0.08);color:#38bdf8;border-color:rgba(56,189,248,0.2);}
.status-arrived{background:rgba(139,92,246,0.08);color:#a78bfa;border-color:rgba(139,92,246,0.2);}
.status-in-progress{background:rgba(236,72,153,0.08);color:#f472b6;border-color:rgba(236,72,153,0.2);}
.status-completed{background:rgba(16,185,129,0.08);color:#34d399;border-color:rgba(16,185,129,0.2);}
.status-invoiced{background:rgba(244,63,94,0.08);color:#fb7185;border-color:rgba(244,63,94,0.2);}
.status-paid{background:rgba(16,185,129,0.12);color:#34d399;border-color:rgba(16,185,129,0.3);}

/* ===== TABS ===== */
.stTabs [data-baseweb="tab-list"]{background:#050508!important;border-radius:14px!important;padding:6px!important;gap:4px!important;border:1px solid #0f0f1a!important;margin-bottom:24px!important;}
.stTabs [data-baseweb="tab"]{background:transparent!important;color:#475569!important;border-radius:10px!important;font-size:12px!important;font-weight:600!important;padding:8px 16px!important;}
.stTabs [aria-selected="true"]{background:linear-gradient(135deg,#6366f1,#4f46e5)!important;color:#fff!important;}
.stTabs [data-baseweb="tab-highlight"]{display:none!important;}
.stTabs [data-baseweb="tab-border"]{display:none!important;}

/* ===== ADDRESS BOX ===== */
.address-box{background:#03030a;border:1px solid #0f0f1a;border-left:3px solid #6366f1;border-radius:14px;padding:18px 20px;margin:14px 0 18px 0;}
.address-box-title{font-size:10px;font-weight:800;color:#6366f1;text-transform:uppercase;letter-spacing:2px;margin-bottom:14px;display:flex;align-items:center;gap:7px;}

/* ===== TECH BADGES ===== */
.badge-active{background:rgba(16,185,129,0.1);color:#34d399;border:1px solid rgba(16,185,129,0.2);border-radius:99px;padding:3px 10px;font-size:10px;font-weight:700;}
.badge-inactive{background:rgba(71,85,105,0.1);color:#64748b;border:1px solid rgba(71,85,105,0.2);border-radius:99px;padding:3px 10px;font-size:10px;font-weight:700;}

/* ===== SUMMARY CARDS ===== */
.summary-card{background:#050508;border:1px solid #0f0f1a;border-radius:20px;padding:24px 26px;margin-bottom:16px;transition:border-color 0.2s;}
.summary-card:hover{border-color:#1a1a2e;}
.summary-card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:18px;padding-bottom:16px;border-bottom:1px solid #0f0f1a;}
.summary-customer{font-size:16px;font-weight:700;color:#f1f5f9;}
.summary-phone{font-size:12px;color:#334155;margin-top:4px;}
.summary-job-id{font-size:10px;color:#334155;font-weight:700;text-transform:uppercase;letter-spacing:1px;}
.summary-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px;}
.summary-field{background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:12px 14px;}
.summary-field-label{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;color:#334155;margin-bottom:6px;}
.summary-field-value{font-size:13px;font-weight:600;color:#cbd5e1;}
.summary-problem{background:#03030a;border:1px solid #0f0f1a;border-left:3px solid #6366f1;border-radius:12px;padding:14px 16px;margin-bottom:12px;}
.summary-problem-label{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;color:#334155;margin-bottom:6px;}
.summary-problem-value{font-size:14px;color:#e2e8f0;line-height:1.6;}
.summary-followup{background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px 16px;}
.summary-followup-label{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;color:#334155;margin-bottom:10px;}
.followup-item{font-size:12px;color:#475569;padding:4px 0;display:flex;align-items:flex-start;gap:10px;}
.followup-dot{color:#6366f1;font-weight:900;flex-shrink:0;}

/* ===== WHATSAPP CHAT ===== */
.wa-outer{background:#03030a;border:1px solid #0f0f1a;border-radius:20px;overflow:hidden;max-width:460px;margin:0 auto;}
.wa-header{background:linear-gradient(135deg,#075E54,#128C7E);padding:16px 20px;display:flex;align-items:center;gap:14px;}
.wa-avatar{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#25D366,#128C7E);display:flex;align-items:center;justify-content:center;font-size:20px;}
.wa-name{font-size:15px;font-weight:700;color:#fff;}
.wa-status{font-size:11px;color:rgba(255,255,255,0.7);margin-top:2px;}
.wa-messages{padding:16px;min-height:340px;max-height:400px;overflow-y:auto;background:#020205;}
.wa-bubble-bot{background:#0f0f1a;border-radius:0 14px 14px 14px;padding:11px 15px;margin-bottom:10px;max-width:84%;font-size:13px;color:#cbd5e1;border:1px solid #1a1a2e;}
.wa-bubble-user{background:linear-gradient(135deg,#1e1b4b,#312e81);border-radius:14px 0 14px 14px;padding:11px 15px;margin-bottom:10px;max-width:84%;margin-left:auto;font-size:13px;color:#e0e7ff;}
.wa-time{font-size:10px;color:#334155;margin-top:4px;text-align:right;}

/* ===== TECH CARDS ===== */
.tech-card{background:#050508;border:1px solid #0f0f1a;border-radius:16px;padding:18px 20px;margin-bottom:12px;}
.tech-card:hover{border-color:#1e1b4b;}

/* ===== ANALYTICS ===== */
.analytics-kpi{background:#050508;border:1px solid #0f0f1a;border-radius:16px;padding:20px 24px;text-align:center;}
.analytics-kpi-num{font-family:'Space Grotesk',sans-serif;font-size:36px;font-weight:700;line-height:1;letter-spacing:-1px;}
.analytics-kpi-label{font-size:10px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-top:6px;}
.analytics-chart-box{background:#050508;border:1px solid #0f0f1a;border-radius:16px;padding:20px 22px;margin-bottom:16px;}
.analytics-chart-title{font-size:11px;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;}

/* ===== CRM STYLES ===== */
.crm-profile-card{background:#050508;border:1px solid #0f0f1a;border-radius:20px;padding:28px;position:relative;overflow:hidden;margin-bottom:20px;}
.crm-profile-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#6366f1,#8b5cf6,#6366f1);}
.crm-avatar{width:64px;height:64px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:28px;margin-bottom:14px;}
.crm-name{font-family:'Space Grotesk',sans-serif;font-size:22px;font-weight:700;color:#f8fafc;}
.crm-phone{font-size:13px;color:#475569;margin-top:4px;}
.crm-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:20px;}
.crm-stat-box{background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;text-align:center;}
.crm-stat-num{font-family:'Space Grotesk',sans-serif;font-size:24px;font-weight:700;color:#f8fafc;}
.crm-stat-label{font-size:9px;font-weight:700;color:#475569;text-transform:uppercase;letter-spacing:1px;margin-top:4px;}
.crm-history-item{background:#03030a;border:1px solid #0f0f1a;border-radius:14px;padding:16px 18px;margin-bottom:10px;transition:border-color 0.2s;}
.crm-history-item:hover{border-color:#1e1b4b;}
.crm-history-status{display:inline-flex;padding:3px 10px;border-radius:999px;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;}
.crm-equipment-card{background:#03030a;border:1px solid #0f0f1a;border-radius:14px;padding:14px 16px;margin-bottom:10px;display:flex;align-items:center;gap:14px;}
.crm-equipment-icon{width:40px;height:40px;border-radius:12px;background:rgba(99,102,241,0.1);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;}
.crm-alert-banner{background:rgba(239,68,68,0.05);border:1px solid rgba(239,68,68,0.15);border-radius:14px;padding:14px 18px;margin-bottom:16px;display:flex;align-items:center;gap:12px;}
.crm-alert-icon{width:36px;height:36px;border-radius:10px;background:rgba(239,68,68,0.1);display:flex;align-items:center;justify-content:center;font-size:16px;color:#f87171;flex-shrink:0;}

/* ===== MOBILE PORTAL ===== */
.portal-mobile-header{background:linear-gradient(135deg,#050508,#0f0f1a);border-bottom:1px solid #1a1a2e;padding:20px 24px;text-align:center;}
.portal-mobile-title{font-family:'Space Grotesk',sans-serif;font-size:18px;font-weight:700;color:#f8fafc;}
.portal-job-card{background:#050508;border:1px solid #0f0f1a;border-radius:20px;padding:20px;margin-bottom:16px;position:relative;overflow:hidden;}
.portal-status-btn{width:100%;padding:14px!important;border-radius:14px!important;font-size:14px!important;font-weight:700!important;margin-bottom:8px!important;border:none!important;cursor:pointer!important;transition:all 0.2s ease!important;}
.portal-status-btn:hover{transform:translateY(-1px)!important;box-shadow:0 4px 15px rgba(0,0,0,0.3)!important;}
.portal-signature-pad{background:#03030a;border:2px dashed #1a1a2e;border-radius:16px;padding:20px;text-align:center;min-height:160px;display:flex;flex-direction:column;align-items:center;justify-content:center;}
.portal-signature-pad.has-sig{border-color:#10b981;background:rgba(16,185,129,0.05);}
.portal-photo-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px;}
.portal-photo-thumb{background:#03030a;border:1px solid #0f0f1a;border-radius:12px;overflow:hidden;aspect-ratio:1;}
.portal-photo-thumb img{width:100%;height:100%;object-fit:cover;}

/* ===== DATAFRAME OVERRIDES ===== */
.stDataFrame{background:#050508!important;border:1px solid #0f0f1a!important;border-radius:14px!important;}
.stDataFrame th{background:#03030a!important;color:#475569!important;font-size:10px!important;text-transform:uppercase!important;}
.stDataFrame td{color:#94a3b8!important;font-size:12px!important;}
.stAlert{background:#03030a!important;border:1px solid #1a1a2e!important;border-radius:12px!important;}
hr{border-color:#0f0f1a!important;}
</style>
""", unsafe_allow_html=True)

# --- 7. PLOTLY DARK THEME HELPER ---
def dark_fig(height=260):
    fig = go.Figure()
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter", color="#475569", size=11),
        margin=dict(l=0, r=0, t=10, b=0), height=height,
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#475569", size=10),
                    orientation="h", yanchor="bottom", y=-0.35),
        xaxis=dict(gridcolor="#0f0f1a", tickfont=dict(color="#334155", size=10), linecolor="#0f0f1a"),
        yaxis=dict(gridcolor="#0f0f1a", tickfont=dict(color="#334155", size=10), linecolor="#0f0f1a"),
    )
    return fig

CHART_CFG = {"displayModeBar": False}

# --- 8. SHARED HEADER ---
def render_header(show_back=False, back_label="← Back to Dashboard", page_title=None):
    logo_base64 = base64.b64encode(open("logo.png","rb").read()).decode() if os.path.exists("logo.png") else None
    logo_html = (
        "<img src='data:image/png;base64," + logo_base64 + "' style='height:42px;width:auto;border-radius:10px;'>"
        if logo_base64
        else "<div style='width:42px;height:42px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:20px;'>⚡</div>"
    )
    title_html = (
        "<div style='font-size:14px;font-weight:600;color:#818cf8;margin-top:2px;'>" + page_title + "</div>"
        if page_title else ""
    )
    if not AI_ENABLED:
        ai_badge = "<div style='background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2);border-radius:99px;padding:6px 14px;font-size:11px;font-weight:700;color:#f87171;'>&#9888;&#65039; AI Offline</div>"
    else:
        ai_badge = "<div style='background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.2);border-radius:99px;padding:6px 14px;font-size:11px;font-weight:700;color:#818cf8;'>&#129302; AI Active</div>"
    date_str = datetime.now().strftime("%b %d, %Y")
    header_html = (
        "<div style='display:flex;align-items:center;justify-content:space-between;"
        "margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid #0f0f1a;'>"
            "<div style='display:flex;align-items:center;gap:16px;'>"
                + logo_html +
                "<div>"
                    "<div style='font-family:Space Grotesk,sans-serif;font-size:20px;font-weight:700;"
                    "color:#f8fafc;letter-spacing:-0.5px;'>TELERON</div>"
                    "<div style='font-size:10px;font-weight:600;color:#334155;text-transform:uppercase;"
                    "letter-spacing:2px;margin-top:1px;'>Central Dispatch</div>"
                    + title_html +
                "</div>"
            "</div>"
            "<div style='display:flex;align-items:center;gap:10px;'>"
                "<div style='background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.2);"
                "border-radius:99px;padding:6px 14px;font-size:11px;font-weight:700;color:#34d399;'>"
                "&#128994; LIVE</div>"
                "<div style='background:#050508;border:1px solid #0f0f1a;border-radius:99px;"
                "padding:6px 14px;font-size:11px;color:#475569;'>" + date_str + "</div>"
                + ai_badge +
            "</div>"
        "</div>"
    )
    st.markdown(header_html, unsafe_allow_html=True)
    if show_back:
        if st.button(back_label, key="back_btn"):
            st.session_state.page = "home"
            st.rerun()

# --- STATUS PILL HTML ---
def status_pill_html(status):
    css_map = {
        "Pending Assignment": "status-pending",
        "Assigned": "status-assigned",
        "En Route": "status-en-route",
        "Arrived": "status-arrived",
        "In Progress": "status-in-progress",
        "Completed": "status-completed",
        "Invoiced": "status-invoiced",
        "Paid": "status-paid"
    }
    css = css_map.get(status, "status-pending")
    dot_color = {"Pending Assignment":"#fbbf24","Assigned":"#818cf8","En Route":"#38bdf8",
                 "Arrived":"#a78bfa","In Progress":"#f472b6","Completed":"#34d399",
                 "Invoiced":"#fb7185","Paid":"#34d399"}.get(status, "#475569")
    return f'<span class="status-pill {css}"><span class="metric-dot" style="background:{dot_color};"></span>{status}</span>'

# --- PIPELINE STEPPER HTML (FIXED) ---
def pipeline_html(current_status, timestamps=None):
    timestamps = timestamps or {}
    steps = [
        ("Pending", "Pending Assignment", "time_pending_assignment"),
        ("Assigned", "Assigned", "time_assigned"),
        ("En Route", "En Route", "time_en_route"),
        ("Arrived", "Arrived", "time_arrived"),
        ("In Progress", "In Progress", "time_in_progress"),
        ("Completed", "Completed", "time_completed"),
        ("Invoiced", "Invoiced", "time_invoiced"),
        ("Paid", "Paid", "time_paid")
    ]
    
    # Find current index safely
    current_index = -1
    for i, (_, full_status, _) in enumerate(steps):
        if full_status == current_status:
            current_index = i
            break
    
    html = '<div class="pipeline-wrap">'
    for i, (label, full_status, ts_key) in enumerate(steps):
        cls = ""
        if full_status == current_status:
            cls = "active current"
        elif current_index != -1 and i < current_index:
            cls = "done"
        
        ts_val = timestamps.get(ts_key, "")
        ts_display = f'<div class="pipeline-time">{str(ts_val)[:16] if ts_val else "—"}</div>'
        html += f'<div class="pipeline-step {cls}"><div class="pipeline-step-line"></div><div class="pipeline-dot">✓</div><div class="pipeline-label">{label}</div>{ts_display}</div>'
    html += '</div>'
    return html

# =======================================================================
# ANALYTICS PAGE — TOTAL CALLS
# =======================================================================
def page_total_calls():
    render_header(show_back=True, page_title="Total Calls Analytics")
    all_jobs = get_jobs()
    total = len(all_jobs)
    dispatched = len(all_jobs[all_jobs['status']=='Dispatched']) if not all_jobs.empty else 0
    pending = len(all_jobs[all_jobs['status']=='Pending Assignment']) if not all_jobs.empty else 0
    other = total - dispatched - pending

    st.markdown("<div class='section-header'>Key Metrics</div>", unsafe_allow_html=True)
    k1,k2,k3,k4 = st.columns(4)
    for col, val, label, color in [
        (k1, total, "Total Calls", "#818cf8"),
        (k2, dispatched, "Dispatched", "#34d399"),
        (k3, pending, "Pending", "#fbbf24"),
        (k4, other, "Other", "#64748b"),
    ]:
        with col:
            st.markdown(f"<div class='analytics-kpi'><div class='analytics-kpi-num' style='color:{color};'>{val}</div><div class='analytics-kpi-label'>{label}</div></div>", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    if not all_jobs.empty and 'timestamp' in all_jobs.columns:
        all_jobs['date'] = pd.to_datetime(all_jobs['timestamp'], errors='coerce').dt.date
        daily = all_jobs.groupby('date').size().reset_index(name='count').sort_values('date')
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128200; Daily Call Volume</div>", unsafe_allow_html=True)
            fig = dark_fig(280)
            fig.add_trace(go.Scatter(x=daily['date'].astype(str), y=daily['count'], mode='lines+markers',
                line=dict(color='#6366f1', width=2.5, shape='spline'),
                marker=dict(color='#818cf8', size=7), fill='tozeroy', fillcolor='rgba(99,102,241,0.07)', name='Calls'))
            st.plotly_chart(fig, use_container_width=True, config=CHART_CFG)
            st.markdown("</div>", unsafe_allow_html=True)
        with col2:
            st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#127789; Status Breakdown</div>", unsafe_allow_html=True)
            sc = all_jobs['status'].value_counts().reset_index(); sc.columns = ['status','count']
            fig2 = dark_fig(280)
            fig2.add_trace(go.Pie(labels=sc['status'], values=sc['count'], hole=0.62,
                marker=dict(colors=['#6366f1','#f59e0b','#10b981','#8b5cf6','#ef4444','#ec4899','#f43f5e','#34d399'], line=dict(color='#000', width=2)),
                textfont=dict(color='#475569', size=10), hovertemplate='%{label}: %{value}<extra></extra>'))
            fig2.add_annotation(text=f"<b>{total}</b>", x=0.5, y=0.5, font=dict(size=22, color='#f1f5f9', family='Space Grotesk'), showarrow=False)
            st.plotly_chart(fig2, use_container_width=True, config=CHART_CFG)
            st.markdown("</div>", unsafe_allow_html=True)
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128202; Weekly Dispatched vs Pending</div>", unsafe_allow_html=True)
        all_jobs['week'] = pd.to_datetime(all_jobs['timestamp'], errors='coerce').dt.to_period('W').astype(str)
        wd = all_jobs[all_jobs['status']=='Dispatched'].groupby('week').size().reset_index(name='dispatched')
        wp = all_jobs[all_jobs['status']=='Pending Assignment'].groupby('week').size().reset_index(name='pending')
        wk = pd.merge(wd, wp, on='week', how='outer').fillna(0).sort_values('week').tail(8)
        fig3 = dark_fig(240)
        fig3.add_trace(go.Bar(name='Dispatched', x=wk['week'], y=wk['dispatched'], marker_color='#6366f1', opacity=0.85))
        fig3.add_trace(go.Bar(name='Pending', x=wk['week'], y=wk['pending'], marker_color='#f59e0b', opacity=0.85))
        fig3.update_layout(barmode='group')
        st.plotly_chart(fig3, use_container_width=True, config=CHART_CFG)
        st.markdown("</div>", unsafe_allow_html=True)
        st.markdown("<div class='section-header' style='margin-top:8px;'>Recent Calls</div>", unsafe_allow_html=True)
        show_cols = [c for c in ['id','customer_name','phone','status','scheduled_date','assigned_tech','timestamp'] if c in all_jobs.columns]
        st.dataframe(all_jobs[show_cols].head(20), use_container_width=True, hide_index=True)
    else:
        st.info("No call data yet.")

# =======================================================================
# ANALYTICS PAGE — ACTIVE JOBS
# =======================================================================
def page_active_jobs():
    render_header(show_back=True, page_title="Active Jobs Analytics")
    all_jobs = get_jobs(); disp_jobs = get_jobs(status_filter="Dispatched"); all_techs = get_technicians()
    tech_active = len(get_technicians(status_filter="Active")); total = len(all_jobs); dispatched = len(disp_jobs)
    dr = f"{round((dispatched/total)*100)}%" if total else "0%"
    cov = f"{round(dispatched/tech_active,1)}" if tech_active and dispatched else "—"
    st.markdown("<div class='section-header'>Key Metrics</div>", unsafe_allow_html=True)
    for col, val, label, color in zip(st.columns(4), [dispatched, dr, cov, tech_active], ["Active Jobs","Dispatch Rate","Jobs / Tech","Techs Active"], ["#34d399","#818cf8","#fbbf24","#a78bfa"]):
        with col: st.markdown(f"<div class='analytics-kpi'><div class='analytics-kpi-num' style='color:{color};'>{val}</div><div class='analytics-kpi-label'>{label}</div></div>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True); col1, col2 = st.columns(2)
    with col1:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128119; Jobs per Technician</div>", unsafe_allow_html=True)
        if not disp_jobs.empty and 'assigned_tech' in disp_jobs.columns:
            tjc = disp_jobs['assigned_tech'].value_counts().reset_index(); tjc.columns = ['tech','count']
            tjc = tjc[tjc['tech'].notna() & (tjc['tech']!='')]
            fig = dark_fig(280)
            fig.add_trace(go.Bar(x=tjc['tech'], y=tjc['count'], marker=dict(color='#10b981', opacity=0.85), text=tjc['count'], textposition='outside', textfont=dict(color='#34d399', size=12)))
            st.plotly_chart(fig, use_container_width=True, config=CHART_CFG)
        else: st.markdown("<div style='text-align:center;padding:40px;color:#1e293b;'>No dispatched jobs yet</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
    with col2:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128200; Dispatch Trend</div>", unsafe_allow_html=True)
        if not all_jobs.empty and 'timestamp' in all_jobs.columns:
            all_jobs['date'] = pd.to_datetime(all_jobs['timestamp'], errors='coerce').dt.date
            dd = all_jobs[all_jobs['status']=='Dispatched'].groupby('date').size().reset_index(name='count').sort_values('date')
            if not dd.empty:
                fig2 = dark_fig(280)
                fig2.add_trace(go.Scatter(x=dd['date'].astype(str), y=dd['count'], mode='lines+markers', line=dict(color='#10b981', width=2.5, shape='spline'), marker=dict(color='#34d399', size=7), fill='tozeroy', fillcolor='rgba(16,185,129,0.07)', name='Dispatched'))
                st.plotly_chart(fig2, use_container_width=True, config=CHART_CFG)
        st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#9889; Total Workload per Technician</div>", unsafe_allow_html=True)
    if not all_jobs.empty and 'assigned_tech' in all_jobs.columns:
        wl = all_jobs['assigned_tech'].value_counts().reset_index(); wl.columns = ['tech','jobs']
        wl = wl[wl['tech'].notna() & (wl['tech']!='') & (wl['tech']!='Unassigned')].head(10)
        if not wl.empty:
            fig3 = dark_fig(max(180, len(wl)*40))
            fig3.add_trace(go.Bar(x=wl['jobs'], y=wl['tech'], orientation='h', marker=dict(color='#6366f1', opacity=0.85), text=wl['jobs'], textposition='outside', textfont=dict(color='#818cf8', size=12)))
            st.plotly_chart(fig3, use_container_width=True, config=CHART_CFG)
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-header'>Currently Dispatched Jobs</div>", unsafe_allow_html=True)
    if not disp_jobs.empty:
        show_cols = [c for c in ['id','customer_name','phone','assigned_tech','scheduled_date','keywords','status'] if c in disp_jobs.columns]
        st.dataframe(disp_jobs[show_cols], use_container_width=True, hide_index=True)
    else: st.info("No active dispatched jobs.")

# =======================================================================
# ANALYTICS PAGE — PENDING
# =======================================================================
def page_pending():
    render_header(show_back=True, page_title="Pending Queue Analytics")
    all_jobs = get_jobs(); pend_jobs = get_jobs(status_filter="Pending Assignment")
    total = len(all_jobs); pending = len(pend_jobs)
    dispatched = len(all_jobs[all_jobs['status']=='Dispatched']) if not all_jobs.empty else 0
    ppct = f"{round((pending/total)*100)}%" if total else "0%"
    ta = len(get_technicians(status_filter="Active"))
    def ql(n):
        if n==0: return "Clear"
        if n<=3: return "Low"
        if n<=8: return "Medium"
        return "High"
    st.markdown("<div class='section-header'>Key Metrics</div>", unsafe_allow_html=True)
    for col, val, label, color in zip(st.columns(4), [pending, ppct, ql(pending), ta], ["Pending Jobs","% of Total","Queue Load","Techs Ready"], ["#fbbf24","#818cf8","#34d399","#a78bfa"]):
        with col: st.markdown(f"<div class='analytics-kpi'><div class='analytics-kpi-num' style='color:{color};'>{val}</div><div class='analytics-kpi-label'>{label}</div></div>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True); col1, col2 = st.columns(2)
    with col1:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128197; Pending by Scheduled Date</div>", unsafe_allow_html=True)
        if not pend_jobs.empty and 'scheduled_date' in pend_jobs.columns:
            pbd = pend_jobs['scheduled_date'].value_counts().reset_index(); pbd.columns = ['date','count']; pbd = pbd.sort_values('date')
            fig = dark_fig(280)
            fig.add_trace(go.Bar(x=pbd['date'].astype(str), y=pbd['count'], marker=dict(color='#f59e0b', opacity=0.85), text=pbd['count'], textposition='outside', textfont=dict(color='#fbbf24', size=12)))
            st.plotly_chart(fig, use_container_width=True, config=CHART_CFG)
        else: st.markdown("<div style='text-align:center;padding:40px;color:#1e293b;'>No pending jobs</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
    with col2:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#9878;&#65039; Pending vs Dispatched vs Other</div>", unsafe_allow_html=True)
        oth = total - pending - dispatched
        fig2 = dark_fig(280)
        fig2.add_trace(go.Pie(labels=['Pending','Dispatched','Other'], values=[max(pending,0), max(dispatched,0), max(oth,0)], hole=0.60,
            marker=dict(colors=['#f59e0b','#6366f1','#334155'], line=dict(color='#000', width=2)), textfont=dict(color='#475569', size=10), hovertemplate='%{label}: %{value}<extra></extra>'))
        fig2.add_annotation(text=f"<b>{total}</b>", x=0.5, y=0.5, font=dict(size=22, color='#f1f5f9', family='Space Grotesk'), showarrow=False)
        st.plotly_chart(fig2, use_container_width=True, config=CHART_CFG)
        st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128200; Pending Jobs Over Time</div>", unsafe_allow_html=True)
    if not all_jobs.empty and 'timestamp' in all_jobs.columns:
        all_jobs['date'] = pd.to_datetime(all_jobs['timestamp'], errors='coerce').dt.date
        pt = all_jobs[all_jobs['status']=='Pending Assignment'].groupby('date').size().reset_index(name='count').sort_values('date')
        if not pt.empty:
            fig3 = dark_fig(220)
            fig3.add_trace(go.Scatter(x=pt['date'].astype(str), y=pt['count'], mode='lines+markers', line=dict(color='#f59e0b', width=2.5, shape='spline'), marker=dict(color='#fbbf24', size=7), fill='tozeroy', fillcolor='rgba(245,158,11,0.07)', name='Pending'))
            st.plotly_chart(fig3, use_container_width=True, config=CHART_CFG)
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-header'>All Pending Jobs</div>", unsafe_allow_html=True)
    if not pend_jobs.empty:
        show_cols = [c for c in ['id','customer_name','phone','scheduled_date','assigned_tech','keywords','timestamp'] if c in pend_jobs.columns]
        st.dataframe(pend_jobs[show_cols], use_container_width=True, hide_index=True)
    else: st.info("No pending jobs at the moment.")

# =======================================================================
# ANALYTICS PAGE — TECHNICIANS
# =======================================================================
def page_technicians():
    render_header(show_back=True, page_title="Technician Analytics")
    all_techs = get_technicians(); all_jobs = get_jobs()
    tt = len(all_techs); ta = len(all_techs[all_techs['status']=='Active']) if not all_techs.empty else 0
    ti = tt - ta; dispatched = len(all_jobs[all_jobs['status']=='Dispatched']) if not all_jobs.empty else 0
    cov = f"{round(dispatched/ta,1)}" if ta and dispatched else "—"
    st.markdown("<div class='section-header'>Key Metrics</div>", unsafe_allow_html=True)
    for col, val, label, color in zip(st.columns(4), [tt, ta, ti, cov], ["Total Techs","Active","Inactive","Jobs / Tech"], ["#a78bfa","#34d399","#64748b","#fbbf24"]):
        with col: st.markdown(f"<div class='analytics-kpi'><div class='analytics-kpi-num' style='color:{color};'>{val}</div><div class='analytics-kpi-label'>{label}</div></div>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True); col1, col2 = st.columns(2)
    with col1:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#127789; Technician Status</div>", unsafe_allow_html=True)
        if not all_techs.empty:
            ts = all_techs['status'].value_counts().reset_index(); ts.columns = ['status','count']
            fig = dark_fig(280)
            fig.add_trace(go.Pie(labels=ts['status'], values=ts['count'], hole=0.62, marker=dict(colors=['#8b5cf6','#475569','#f59e0b'], line=dict(color='#000', width=2)), textfont=dict(color='#475569', size=10), hovertemplate='%{label}: %{value}<extra></extra>'))
            fig.add_annotation(text=f"<b>{tt}</b>", x=0.5, y=0.5, font=dict(size=22, color='#f1f5f9', family='Space Grotesk'), showarrow=False)
            st.plotly_chart(fig, use_container_width=True, config=CHART_CFG)
        st.markdown("</div>", unsafe_allow_html=True)
    with col2:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128202; Total Jobs per Technician</div>", unsafe_allow_html=True)
        if not all_jobs.empty and 'assigned_tech' in all_jobs.columns:
            wl = all_jobs['assigned_tech'].value_counts().reset_index(); wl.columns = ['tech','jobs']
            wl = wl[wl['tech'].notna() & (wl['tech']!='') & (wl['tech']!='Unassigned')].head(10)
            if not wl.empty:
                fig2 = dark_fig(280)
                fig2.add_trace(go.Bar(x=wl['jobs'], y=wl['tech'], orientation='h', marker=dict(color='#8b5cf6', opacity=0.85), text=wl['jobs'], textposition='outside', textfont=dict(color='#a78bfa', size=12)))
                fig2.update_layout(xaxis=dict(gridcolor="#0f0f1a"), yaxis=dict(gridcolor="rgba(0,0,0,0)", tickfont=dict(color="#94a3b8", size=11)))
                st.plotly_chart(fig2, use_container_width=True, config=CHART_CFG)
        st.markdown("</div>", unsafe_allow_html=True)
    if not all_techs.empty and 'zone' in all_techs.columns:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128205; Technicians by Zone</div>", unsafe_allow_html=True)
        zc = all_techs[all_techs['zone'].notna() & (all_techs['zone']!='')]['zone'].value_counts().reset_index(); zc.columns = ['zone','count']
        if not zc.empty:
            fig3 = dark_fig(220)
            fig3.add_trace(go.Bar(x=zc['zone'], y=zc['count'], marker=dict(color='#6366f1', opacity=0.85), text=zc['count'], textposition='outside', textfont=dict(color='#818cf8', size=12)))
            st.plotly_chart(fig3, use_container_width=True, config=CHART_CFG)
        st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-header'>Full Technician Roster</div>", unsafe_allow_html=True)
    if not all_techs.empty: st.dataframe(all_techs, use_container_width=True, hide_index=True)
    else: st.info("No technicians added yet.")

# =======================================================================
# ANALYTICS PAGE — REVENUE & PERFORMANCE
# =======================================================================
def page_revenue():
    render_header(show_back=True, page_title="Revenue & Performance Analytics")
    all_jobs = get_jobs(); all_techs = get_technicians()
    def _pm(v):
        if pd.isna(v): return 0.0
        s = str(v).replace("$", "").replace(",", "").replace(" ", "").strip()
        try: return float(s) if s else 0.0
        except: return 0.0
    def _pp(v):
        if pd.isna(v): return 0.0
        s = str(v).replace("%", "").replace(" ", "").strip()
        try: return float(s) if s else 0.0
        except: return 0.0
    if not all_techs.empty:
        all_techs["avg_ticket_num"] = all_techs["avg_ticket"].apply(_pm)
        all_techs["conversion_num"] = all_techs["conversion"].apply(_pp)
    tech_revenue = []; total_revenue = 0.0
    if not all_jobs.empty and not all_techs.empty and "assigned_tech" in all_jobs.columns:
        disp_jobs = all_jobs[all_jobs["status"] == "Dispatched"]
        for _, tech in all_techs.iterrows():
            t_jobs = disp_jobs[disp_jobs["assigned_tech"] == tech["name"]]
            jc = len(t_jobs); rev = jc * tech["avg_ticket_num"] * (tech["conversion_num"] / 100.0)
            tech_revenue.append({"name": tech["name"], "jobs": jc, "avg_ticket": tech["avg_ticket_num"], "conversion": tech["conversion_num"], "revenue": rev})
            total_revenue += rev
    tr_df = pd.DataFrame(tech_revenue).sort_values("revenue", ascending=False) if tech_revenue else pd.DataFrame()
    total_jobs = len(all_jobs); dispatched = len(all_jobs[all_jobs["status"] == "Dispatched"]) if not all_jobs.empty else 0
    avg_conv = all_techs["conversion_num"].mean() if not all_techs.empty and "conversion_num" in all_techs.columns else 0.0
    te = tr_df.iloc[0]["name"] if not tr_df.empty else "—"
    trv = tr_df.iloc[0]["revenue"] if not tr_df.empty else 0.0
    st.markdown("<div class='section-header'>Revenue Overview</div>", unsafe_allow_html=True)
    for col, val, label, color in zip(st.columns(4), [f"${total_revenue:,.0f}", f"{avg_conv:.1f}%", te, f"${trv:,.0f}"], ["Total Revenue","Avg Conversion","Top Earner","Top Revenue"], ["#fb7185","#818cf8","#34d399","#fbbf24"]):
        with col: st.markdown(f"<div class='analytics-kpi'><div class='analytics-kpi-num' style='color:{color};'>{val}</div><div class='analytics-kpi-label'>{label}</div></div>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True); col1, col2 = st.columns(2)
    with col1:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128176; Revenue per Technician</div>", unsafe_allow_html=True)
        if not tr_df.empty:
            fig = dark_fig(300)
            fig.add_trace(go.Bar(x=tr_df["name"], y=tr_df["revenue"], marker=dict(color="#f43f5e", opacity=0.85), text=[f"${r:,.0f}" for r in tr_df["revenue"]], textposition="outside", textfont=dict(color="#fb7185", size=11)))
            fig.update_layout(yaxis=dict(tickprefix="$", tickfont=dict(size=10)))
            st.plotly_chart(fig, use_container_width=True, config=CHART_CFG)
        else: st.markdown("<div style='text-align:center;padding:40px;color:#1e293b;'>No revenue data yet</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
    with col2:
        st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128201; Conversion Funnel</div>", unsafe_allow_html=True)
        cc = int(dispatched * (avg_conv / 100.0)) if avg_conv else 0
        fig2 = dark_fig(300)
        fig2.add_trace(go.Funnel(y=["Total Jobs", "Dispatched", f"Converted ({avg_conv:.0f}%)"], x=[total_jobs, dispatched, cc], textposition="inside", textinfo="value+percent initial",
            marker=dict(color=["#6366f1", "#10b981", "#f43f5e"], line=dict(color="#000", width=2)), connector=dict(line=dict(color="#1e293b", width=1))))
        fig2.update_layout(font=dict(size=12), margin=dict(l=20, r=20, t=10, b=0))
        st.plotly_chart(fig2, use_container_width=True, config=CHART_CFG)
        st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("<div class='analytics-chart-box'><div class='analytics-chart-title'>&#128200; Weekly Earnings Trend</div>", unsafe_allow_html=True)
    if not all_jobs.empty and "timestamp" in all_jobs.columns and not all_techs.empty:
        all_jobs["date"] = pd.to_datetime(all_jobs["timestamp"], errors="coerce"); all_jobs["week"] = all_jobs["date"].dt.to_period("W").astype(str)
        disp = all_jobs[all_jobs["status"] == "Dispatched"].copy()
        if not disp.empty:
            tech_map = all_techs.set_index("name")[["avg_ticket_num", "conversion_num"]].to_dict("index")
            def calc_rev(row):
                t = tech_map.get(row.get("assigned_tech"))
                return t["avg_ticket_num"] * (t["conversion_num"] / 100.0) if t else 0.0
            disp["job_revenue"] = disp.apply(calc_rev, axis=1)
            weekly = disp.groupby("week")["job_revenue"].sum().reset_index().sort_values("week").tail(12)
            if not weekly.empty:
                fig3 = dark_fig(260)
                fig3.add_trace(go.Scatter(x=weekly["week"], y=weekly["job_revenue"], mode="lines+markers", line=dict(color="#f43f5e", width=2.5, shape="spline"), marker=dict(color="#fb7185", size=8), fill="tozeroy", fillcolor="rgba(244,63,94,0.07)", name="Earnings"))
                fig3.update_layout(yaxis=dict(tickprefix="$", tickfont=dict(size=10)))
                st.plotly_chart(fig3, use_container_width=True, config=CHART_CFG)
            else: st.markdown("<div style='text-align:center;padding:40px;color:#1e293b;'>No weekly data yet</div>", unsafe_allow_html=True)
        else: st.markdown("<div style='text-align:center;padding:40px;color:#1e293b;'>No dispatched jobs yet</div>", unsafe_allow_html=True)
    else: st.markdown("<div style='text-align:center;padding:40px;color:#1e293b;'>No data available</div>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-header'>Technician Performance Breakdown</div>", unsafe_allow_html=True)
    if not tr_df.empty:
        tr_df["revenue_fmt"] = tr_df["revenue"].apply(lambda x: f"${x:,.0f}")
        tr_df["avg_ticket_fmt"] = tr_df["avg_ticket"].apply(lambda x: f"${x:,.0f}")
        tr_df["conversion_fmt"] = tr_df["conversion"].apply(lambda x: f"{x:.1f}%")
        show_df = tr_df[["name", "jobs", "avg_ticket_fmt", "conversion_fmt", "revenue_fmt"]].rename(columns={"name": "Technician", "jobs": "Dispatched Jobs", "avg_ticket_fmt": "Avg Ticket", "conversion_fmt": "Conversion", "revenue_fmt": "Est. Revenue"})
        st.dataframe(show_df, use_container_width=True, hide_index=True)
    else: st.info("Add technicians with avg ticket & conversion rates to see revenue data.")

# =======================================================================
# HOME PAGE (MAIN DISPATCH INTERFACE)
# =======================================================================
def page_home():
    render_header()

    all_jobs = get_jobs(); total = len(all_jobs)
    dispatched = len(get_jobs(status_filter="Dispatched"))
    pending = len(get_jobs(status_filter="Pending Assignment"))
    other_jobs = total - dispatched - pending
    all_techs = get_technicians(); tech_total = len(all_techs)
    tech_active = len(get_technicians(status_filter="Active"))
    tech_inactive = tech_total - tech_active
    dr = f"{round((dispatched/total)*100)}%" if total else "0%"
    pp = f"{round((pending/total)*100)}%" if total else "0%"
    ap = f"{round((dispatched/total)*100)}%" if total else "0%"
    cov = f"{round(dispatched/tech_active,1)}" if tech_active and dispatched else "—"
    tbp = round((tech_active/tech_total)*100) if tech_total else 0
    tbp2 = round((dispatched/total)*100) if total else 0
    pbp = round((pending/total)*100) if total else 0

    total_revenue = 0.0; avg_conv = 0.0
    if not all_techs.empty:
        def _pm(v):
            if pd.isna(v): return 0.0
            s = str(v).replace("$","").replace(",","").replace(" ","").strip()
            try: return float(s) if s else 0.0
            except: return 0.0
        def _pp(v):
            if pd.isna(v): return 0.0
            s = str(v).replace("%","").replace(" ","").strip()
            try: return float(s) if s else 0.0
            except: return 0.0
        all_techs["avg_ticket_num"] = all_techs["avg_ticket"].apply(_pm)
        all_techs["conversion_num"] = all_techs["conversion"].apply(_pp)
        avg_conv = all_techs["conversion_num"].mean()
        if not all_jobs.empty and "assigned_tech" in all_jobs.columns:
            djobs = all_jobs[all_jobs["status"] == "Dispatched"]
            for _, t in all_techs.iterrows():
                tj = djobs[djobs["assigned_tech"] == t["name"]]
                total_revenue += len(tj) * t["avg_ticket_num"] * (t["conversion_num"] / 100.0)

    def ql(n):
        if n==0: return "Clear"
        if n<=3: return "Low"
        if n<=8: return "Medium"
        return "High"

    st.markdown("<div style='font-size:10px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px;'>&#128202; Live Dashboard — click any card to open full analytics</div>", unsafe_allow_html=True)
    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    with mc1:
        st.markdown(f'<div class="metric-card blue"><div class="metric-card-top"><div class="metric-icon icon-blue">&#128222;</div><span class="metric-badge badge-blue">All Time</span></div><div class="metric-number">{total}</div><div class="metric-title">Total Calls</div><hr class="metric-divider"><div class="metric-row"><span class="metric-row-label"><span class="metric-dot" style="background:#818cf8;"></span>Dispatched</span><span class="metric-row-val">{dispatched}</span></div><div class="metric-row"><span class="metric-row-label"><span class="metric-dot" style="background:#fbbf24;"></span>Pending</span><span class="metric-row-val">{pending}</span></div><div class="metric-row"><span class="metric-row-label"><span class="metric-dot" style="background:#1e293b;"></span>Other</span><span class="metric-row-val">{other_jobs}</span></div><div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{tbp2}%;background:linear-gradient(90deg,#6366f1,#818cf8);"></div></div><div class="metric-hint">▼ CLICK TO VIEW ANALYTICS</div></div>', unsafe_allow_html=True)
        if st.button("&#128222; Open Total Calls Analytics", key="btn_total", use_container_width=True):
            st.session_state.page = "total_calls"; st.rerun()
    with mc2:
        st.markdown(f'<div class="metric-card green"><div class="metric-card-top"><div class="metric-icon icon-green">&#128640;</div><span class="metric-badge badge-green">{ap}% of total</span></div><div class="metric-number">{dispatched}</div><div class="metric-title">Active Jobs</div><hr class="metric-divider"><div class="metric-row"><span class="metric-row-label">In Progress</span><span class="metric-row-val">{dispatched}</span></div><div class="metric-row"><span class="metric-row-label">Dispatch Rate</span><span class="metric-row-val">{dr}</span></div><div class="metric-row"><span class="metric-row-label">Jobs / Tech</span><span class="metric-row-val">{cov}</span></div><div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{tbp2}%;background:linear-gradient(90deg,#10b981,#34d399);"></div></div><div class="metric-hint">▼ CLICK TO VIEW ANALYTICS</div></div>', unsafe_allow_html=True)
        if st.button("&#128640; Open Active Jobs Analytics", key="btn_active", use_container_width=True):
            st.session_state.page = "active_jobs"; st.rerun()
    with mc3:
        st.markdown(f'<div class="metric-card amber"><div class="metric-card-top"><div class="metric-icon icon-amber">&#9203;</div><span class="metric-badge badge-amber">Awaiting</span></div><div class="metric-number">{pending}</div><div class="metric-title">Pending Assignment</div><hr class="metric-divider"><div class="metric-row"><span class="metric-row-label">Queue Load</span><span class="metric-row-val">{ql(pending)}</span></div><div class="metric-row"><span class="metric-row-label">% of Total</span><span class="metric-row-val">{pp}</span></div><div class="metric-row"><span class="metric-row-label">Techs Ready</span><span class="metric-row-val">{tech_active}</span></div><div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{pbp}%;background:linear-gradient(90deg,#f59e0b,#fbbf24);"></div></div><div class="metric-hint">▼ CLICK TO VIEW ANALYTICS</div></div>', unsafe_allow_html=True)
        if st.button("&#9203; Open Pending Queue Analytics", key="btn_pending", use_container_width=True):
            st.session_state.page = "pending"; st.rerun()
    with mc4:
        st.markdown(f'<div class="metric-card purple"><div class="metric-card-top"><div class="metric-icon icon-purple">&#128119;</div><span class="metric-badge badge-purple">{tech_active} Active</span></div><div class="metric-number">{tech_total}</div><div class="metric-title">Technicians</div><hr class="metric-divider"><div class="metric-row"><span class="metric-row-label"><span class="metric-dot" style="background:#34d399;"></span>Active</span><span class="metric-row-val">{tech_active}</span></div><div class="metric-row"><span class="metric-row-label"><span class="metric-dot" style="background:#1e293b;"></span>Inactive</span><span class="metric-row-val">{tech_inactive}</span></div><div class="metric-row"><span class="metric-row-label">Coverage</span><span class="metric-row-val">{cov} j/t</span></div><div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{tbp}%;background:linear-gradient(90deg,#8b5cf6,#a78bfa);"></div></div><div class="metric-hint">▼ CLICK TO VIEW ANALYTICS</div></div>', unsafe_allow_html=True)
        if st.button("&#128119; Open Technician Analytics", key="btn_tech", use_container_width=True):
            st.session_state.page = "technicians"; st.rerun()
    with mc5:
        st.markdown(f'<div class="metric-card" style="border-color:rgba(244,63,94,0.1);"><div class="metric-card-top"><div class="metric-icon" style="background:rgba(244,63,94,0.12);color:#fb7185;">&#128176;</div><span class="metric-badge" style="background:rgba(244,63,94,0.12);color:#fb7185;">Revenue</span></div><div class="metric-number">${total_revenue:,.0f}</div><div class="metric-title">Revenue & Performance</div><hr class="metric-divider"><div class="metric-row"><span class="metric-row-label"><span class="metric-dot" style="background:#fb7185;"></span>Avg Conversion</span><span class="metric-row-val">{avg_conv:.1f}%</span></div><div class="metric-row"><span class="metric-row-label"><span class="metric-dot" style="background:#fbbf24;"></span>Dispatched</span><span class="metric-row-val">{dispatched}</span></div><div class="metric-row"><span class="metric-row-label"><span class="metric-dot" style="background:#34d399;"></span>Techs Active</span><span class="metric-row-val">{tech_active}</span></div><div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:100%;background:linear-gradient(90deg,#f43f5e,#fb7185);"></div></div><div class="metric-hint">▼ CLICK TO VIEW ANALYTICS</div></div>', unsafe_allow_html=True)
        if st.button("&#128176; Open Revenue Analytics", key="btn_revenue", use_container_width=True):
            st.session_state.page = "revenue"; st.rerun()

    st.markdown("<hr style='border-color:#0f0f1a;margin:28px 0 24px;'>", unsafe_allow_html=True)

    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
        "&#9889; DISPATCH","&#129302; AI BOT","&#128203; SUMMARIES","&#128222; VOICE AI","&#128194; HISTORY","&#128119; ROSTER","&#128462; INVOICES","&#128100; CUSTOMERS"
    ])

    # ===== TAB 1: DISPATCH BOARD =====
    with tab1:
        gl, gr = st.columns([1,1], gap="large")
        with gl:
            st.markdown("<div class='section-header'>New Customer Call Intake</div>", unsafe_allow_html=True)
            c_name = st.text_input("Customer Full Name", placeholder="John Smith", key="ni_name")
            c_phone = st.text_input("Customer Phone Number", placeholder="+1 (555) 000-0000", key="ni_phone")
            st.markdown("<div class='address-box'><div class='address-box-title'>&#128205; Service Address</div>", unsafe_allow_html=True)
            addr_street = st.text_input("Street Address", placeholder="123 Main Street, Apt 4B", key="ni_street")
            ac1, ac2 = st.columns(2)
            with ac1:
                addr_city = st.text_input("City", placeholder="Houston", key="ni_city")
                addr_zip = st.text_input("ZIP Code", placeholder="77001", key="ni_zip")
            with ac2:
                addr_state = st.text_input("State", placeholder="Texas", key="ni_state")
                addr_country = st.selectbox("Country", ["United States","Canada","United Kingdom","Australia","Other"], key="ni_country")
            addr_notes = st.text_input("Access Notes", placeholder="Gate code 1234, Ring doorbell", key="ni_notes")
            st.markdown("</div>", unsafe_allow_html=True)
            s_transcript = st.text_area("Call Notes / Transcript", height=90, placeholder="Describe the customer's issue...", key="ni_transcript")
            c1, c2 = st.columns(2)
            with c1: s_date = st.date_input("Schedule Date", key="ni_date")
            with c2:
                atdf = get_technicians(status_filter="Active")
                tech_names = atdf['name'].tolist() if not atdf.empty else ["No Active Technicians"]
                s_tech = st.selectbox("Assign Technician", tech_names, key="ni_tech")
            if st.button("&#128178;  SAVE JOB", use_container_width=True, key="ni_save"):
                if c_name.strip() and c_phone.strip():
                    full_address = f"{addr_street}, {addr_city}, {addr_state} {addr_zip}, {addr_country}"
                    if addr_notes.strip(): full_address += f" | Notes: {addr_notes}"
                    with st.spinner("Saving job..."):
                        upsert_customer({"name": c_name.strip(), "phone": c_phone.strip(), "address": full_address, "last_contact": datetime.now().isoformat()})
                        result = insert_job({"customer_name": c_name.strip(), "phone": c_phone.strip(), "transcript": s_transcript, "status": "Pending Assignment", "scheduled_date": str(s_date), "assigned_tech": s_tech, "timestamp": datetime.now().isoformat(), "keywords": full_address})
                        if s_transcript.strip() and result.data and AI_ENABLED:
                            new_id = result.data[0]["id"]
                            summary = generate_call_summary(s_transcript, c_name, c_phone)
                            save_summary_to_db(new_id, summary)
                            st.success("&#9989; Job saved with AI summary!")
                        else:
                            st.success("&#9989; Job saved successfully!")
                    st.rerun()
                else:
                    st.error("Customer name and phone are required.")

        with gr:
            st.markdown("<div class='section-header'>Live Dispatch Board</div>", unsafe_allow_html=True)
            f1, f2 = st.columns(2)
            with f1: filter_status = st.selectbox("Filter by Status", ["All"] + STATUS_PIPELINE, key="db_filter")
            with f2: sort_by = st.selectbox("Sort by", ["Newest First", "Oldest First", "Urgency: Emergency First"], key="db_sort")
            board_jobs = get_jobs()
            if not board_jobs.empty:
                if filter_status != "All":
                    board_jobs = board_jobs[board_jobs['status'] == filter_status]
                for _, row in board_jobs.head(20).iterrows():
                    addr = str(row.get('keywords','') or '')
                    summary = parse_summary(row.get('ai_summary', None))
                    current_status = row.get('status', 'Pending Assignment')
                    if current_status not in STATUS_PIPELINE:
                        current_status = "Pending Assignment"
                    
                    st.markdown(f'<div class="job-card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;"><div class="job-card-id">JOB #{row["id"]}</div>{status_pill_html(current_status)}</div><div class="job-card-name">{row["customer_name"]}</div><div class="job-card-meta"><span>&#128241; {row["phone"]}</span>{"<span>&#128205; "+addr[:45]+"</span>" if addr else ""}{"<span>&#128295; "+summary.get("service_type","")+"</span>" if summary and summary.get("service_type") else ""}{"<span>&#9888;&#65039; "+summary.get("urgency","")+"</span>" if summary and summary.get("urgency") in ["Emergency","High"] else ""}</div></div>', unsafe_allow_html=True)
                    ts_data = {k: row.get(k, '') for k in ['time_pending_assignment','time_assigned','time_en_route','time_arrived','time_in_progress','time_completed','time_invoiced','time_paid']}
                    st.markdown(pipeline_html(current_status, ts_data), unsafe_allow_html=True)
                    btns = st.columns(4)
                    with btns[0]:
                        nxt = get_next_status(current_status)
                        if nxt != current_status:
                            if st.button(f"&#9654; {nxt}", key=f"adv_{row['id']}", use_container_width=True):
                                update_job_status(row['id'], nxt)
                                st.rerun()
                    with btns[1]:
                        if st.button(f"&#128205; Map", key=f"map_{row['id']}", use_container_width=True):
                            addr_map = addr.replace(" ", "+")
                            st.markdown(f'<a href="https://www.google.com/maps/search/?api=1&query={addr_map}" target="_blank" style="text-decoration:none;"><div style="background:#03030a;border:1px solid #0f0f1a;border-radius:10px;padding:10px 14px;font-size:11px;color:#818cf8;text-align:center;">&#128205; Open in Google Maps</div></a>', unsafe_allow_html=True)
                    with btns[2]:
                        if st.button(f"&#128172; Notes", key=f"notebtn_{row['id']}", use_container_width=True):
                            st.session_state[f"show_notes_{row['id']}"] = True
                    with btns[3]:
                        if st.button(f"&#128221; Invoice", key=f"invbtn_{row['id']}", use_container_width=True):
                            st.session_state[f"show_inv_{row['id']}"] = True
                    if st.session_state.get(f"show_notes_{row['id']}", False):
                        with st.form(f"note_form_{row['id']}"):
                            note_text = st.text_area("Add Field Note", placeholder="Parts used, customer feedback, issues...", key=f"note_txt_{row['id']}")
                            if st.form_submit_button("&#128178; Save Note"):
                                if note_text.strip():
                                    insert_job_note({"job_id": row['id'], "note": note_text.strip(), "author": "Dispatcher", "timestamp": datetime.now().isoformat()})
                                    st.success("Note saved!"); st.rerun()
                    if st.session_state.get(f"show_inv_{row['id']}", False):
                        with st.form(f"inv_form_{row['id']}"):
                            inv_amt = st.number_input("Invoice Amount ($)", min_value=0.0, value=150.0, step=10.0, key=f"inv_amt_{row['id']}")
                            inv_desc = st.text_input("Description", value=f"Service for {row['customer_name']}", key=f"inv_desc_{row['id']}")
                            if st.form_submit_button("&#128178; Create Invoice"):
                                payload = {"invoice_number": f"INV-{row['id']}-{datetime.now().strftime('%y%m%d')}", "job_id": row['id'], "customer_name": row.get('customer_name',''), "phone": row.get('phone',''), "address": str(row.get('keywords','')), "invoice_date": str(datetime.now().date()), "due_date": str((datetime.now().date() + timedelta(days=14))), "line_items": json.dumps([{"description": inv_desc, "qty": 1, "rate": inv_amt, "amount": inv_amt}]), "subtotal": inv_amt, "tax_rate": 0, "tax_amount": 0, "total": inv_amt, "notes": "", "status": "Sent", "created_at": datetime.now().isoformat()}
                                res = insert_invoice(payload)
                                if isinstance(res, dict) and res.get("error"):
                                    st.session_state.invoices_local.append({**payload, "id": f"LOCAL-{len(st.session_state.invoices_local)+1}"})
                                    st.success("&#9989; Invoice saved locally!")
                                else:
                                    st.success("&#9989; Invoice saved to database!")
                                supabase.table("jobs").update({"status": "Invoiced"}).eq("id", row['id']).execute()
                                st.rerun()
                    st.markdown("<hr style='border:none;border-top:1px solid #050508;margin:4px 0 12px;'>", unsafe_allow_html=True)
            else:
                st.markdown("<div style='text-align:center;padding:50px 20px;color:#1e293b;'><div style='font-size:36px;margin-bottom:10px;'>&#10003;</div><div style='font-size:13px;font-weight:600;'>No jobs on the board yet</div></div>", unsafe_allow_html=True)

    # ===== TAB 2: AI BOT =====
    with tab2:
        st.markdown("<div class='section-header'>WhatsApp AI Chatbot — Live Preview</div>", unsafe_allow_html=True)
        bc, ic = st.columns([1,1], gap="large")
        with bc:
            st.markdown("<div class='wa-outer'><div class='wa-header'><div class='wa-avatar'>&#9889;</div><div><div class='wa-name'>Teleron AI Assistant</div><div class='wa-status'>&#128308; Online · Powered by Groq AI</div></div></div></div>", unsafe_allow_html=True)
            if not st.session_state.wa_chat:
                st.session_state.wa_chat = [{"role":"assistant","content":"&#128075; Hello! I'm the Teleron AI Assistant.\n\nI can help you with:\n• &#128295; Booking a service appointment\n• &#10067; HVAC, plumbing & electrical questions\n• &#128680; Emergency dispatch\n• &#128176; Pricing estimates\n\nHow can I help you today?","time":datetime.now().strftime("%H:%M")}]
            chat_html = "<div class='wa-messages'>"
            for msg in st.session_state.wa_chat:
                bubble = "wa-bubble-bot" if msg["role"]=="assistant" else "wa-bubble-user"
                chat_html += f"<div class='{bubble}'>{msg['content'].replace(chr(10),'<br>')}<div class='wa-time'>{msg.get('time','')}</div></div>"
            chat_html += "</div>"
            st.markdown(chat_html, unsafe_allow_html=True)
            user_input = st.text_input("", key="wa_input", placeholder="Type a message...", label_visibility="collapsed")
            sc, cc = st.columns([3,1])
            with sc:
                if st.button("&#128228;  SEND", use_container_width=True, key="wa_send"):
                    if user_input.strip():
                        now = datetime.now().strftime("%H:%M")
                        st.session_state.wa_chat.append({"role":"user","content":user_input.strip(),"time":now})
                        history = [{"role":m["role"],"content":m["content"]} for m in st.session_state.wa_chat[:-1]]
                        with st.spinner(""):
                            bot_reply = whatsapp_bot_response(user_input.strip(), history)
                        st.session_state.wa_chat.append({"role":"assistant","content":bot_reply,"time":datetime.now().strftime("%H:%M")})
                        st.rerun()
            with cc:
                if st.button("&#128465;&#65039;", use_container_width=True, key="wa_clear"):
                    st.session_state.wa_chat = [{"role":"assistant","content":"&#128075; Hello! How can I help you today?","time":datetime.now().strftime("%H:%M")}]
                    st.rerun()
        with ic:
            st.markdown("<div style='background:#050508;border:1px solid #0f0f1a;border-radius:16px;padding:20px;'><div style='font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:18px;'>&#128241; Connect to WhatsApp</div><div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'><div style='font-size:9px;font-weight:800;color:#6366f1;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>Option 1 — Twilio</div><div style='font-size:12px;color:#475569;line-height:1.8;'>1. Sign up at twilio.com<br>2. Get WhatsApp-enabled number<br>3. Set webhook to FastAPI backend<br>4. Bot handles messages automatically</div></div><div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'><div style='font-size:9px;font-weight:800;color:#34d399;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>Option 2 — Meta Cloud API</div><div style='font-size:12px;color:#475569;line-height:1.8;'>1. Apply at business.whatsapp.com<br>2. Create Meta Business account<br>3. Connect your phone number<br>4. Set the AI webhook endpoint</div></div><div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;'><div style='font-size:9px;font-weight:800;color:#a78bfa;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>Bot Capabilities</div><div style='font-size:12px;color:#475569;line-height:2;'>&#10022; 24/7 customer support<br>&#10022; Collect name & address<br>&#10022; Detect & escalate emergencies<br>&#10022; Book appointments<br>&#10022; Pricing estimates<br>&#10022; Hand off to human dispatcher</div></div></div>", unsafe_allow_html=True)

    # ===== TAB 3: SUMMARIES =====
    with tab3:
        st.markdown("<div class='section-header'>AI Call Summaries</div>", unsafe_allow_html=True)
        fc1, fc2, fc3 = st.columns(3)
        with fc1: fu = st.selectbox("Urgency", ["All","Emergency","High","Medium","Low"], key="su")
        with fc2: fs = st.selectbox("Sentiment", ["All","Angry","Frustrated","Urgent","Confused","Calm","Satisfied"], key="ss")
        with fc3: fv = st.selectbox("Service", ["All","HVAC","Plumbing","Electrical","Appliance Repair","General Home Service"], key="sv")
        sdf = get_jobs()
        if sdf.empty: st.info("No jobs yet.")
        else:
            shown = 0
            for _, row in sdf.iterrows():
                summary = parse_summary(row.get("ai_summary",None))
                if summary:
                    if fu!="All" and summary.get("urgency","").lower()!=fu.lower(): continue
                    if fs!="All" and summary.get("sentiment","").lower()!=fs.lower(): continue
                    if fv!="All" and summary.get("service_type","").lower()!=fv.lower(): continue
                shown += 1; uv = summary.get("urgency","Unknown").lower() if summary else "unknown"
                sv2 = summary.get("sentiment","Unknown").lower() if summary else "unknown"
                uc = {"low":"urgency-low","medium":"urgency-medium","high":"urgency-high","emergency":"urgency-emergency"}.get(uv,"urgency-medium")
                sc2 = {"calm":"sentiment-calm","frustrated":"sentiment-frustrated","urgent":"sentiment-urgent","angry":"sentiment-angry","satisfied":"sentiment-satisfied","confused":"sentiment-confused"}.get(sv2,"sentiment-calm")
                addr = str(row.get('keywords','') or '—')
                if summary:
                    fhtml = "".join([f"<div class='followup-item'><span class='followup-dot'>&#8250;</span>{item}</div>" for item in summary.get("follow_up",[])])
                    st.markdown(f'<div class="summary-card"><div class="summary-card-header"><div><div class="summary-customer">{row["customer_name"]}</div><div class="summary-phone">&#128241; {row["phone"]}</div><div style="font-size:11px;color:#6366f1;margin-top:5px;">&#128205; {addr[:65]}</div></div><div style="text-align:right;"><div class="summary-job-id">JOB #{row["id"]}</div><div style="font-size:11px;color:#1e293b;margin-top:4px;">{row.get("scheduled_date","—")}</div><div style="margin-top:8px;"><span class="{uc}">{summary.get("urgency","Unknown")}</span></div></div></div><div class="summary-problem"><div class="summary-problem-label">Problem Detected</div><div class="summary-problem-value">{summary.get("problem","—")}</div></div><div class="summary-grid"><div class="summary-field"><div class="summary-field-label">Service Type</div><div class="summary-field-value">{summary.get("service_type","—")}</div></div><div class="summary-field"><div class="summary-field-label">Tech Skill Needed</div><div class="summary-field-value">{summary.get("tech_skill","—")}</div></div><div class="summary-field"><div class="summary-field-label">Sentiment</div><div class="summary-field-value"><span class="{sc2}">{summary.get("sentiment","—")}</span></div></div><div class="summary-field"><div class="summary-field-label">Assigned Tech</div><div class="summary-field-value">{row.get("assigned_tech","Unassigned")}</div></div></div><div class="summary-followup"><div class="summary-followup-label">Follow-up Actions</div>{fhtml}</div></div>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<div class="summary-card"><div class="summary-card-header"><div><div class="summary-customer">{row["customer_name"]}</div><div class="summary-phone">&#128241; {row["phone"]}</div></div><div class="summary-job-id">JOB #{row["id"]}</div></div><div style="color:#1e293b;font-size:13px;font-style:italic;">No AI summary yet.</div></div>', unsafe_allow_html=True)
                if st.button(f"&#8630;  Regenerate Summary — Job #{row['id']}", key=f"regen_{row['id']}"):
                    t = row.get("transcript","")
                    if t and str(t).strip():
                        with st.spinner("Generating..."):
                            ns = generate_call_summary(str(t), row['customer_name'], row['phone'])
                            save_summary_to_db(row['id'], ns)
                        st.success("Updated!"); st.rerun()
                    else: st.warning("No transcript found.")
                st.markdown("<hr style='border:none;border-top:1px solid #050508;margin:4px 0 12px;'>", unsafe_allow_html=True)
            if shown==0: st.info("No summaries match your filters.")

    # ===== TAB 4: VOICE AI =====
    with tab4:
        st.markdown("<div class='section-header'>Voice AI Receptionist — Live Call Log</div>", unsafe_allow_html=True)
        webhook_url = "https://teleronwebhook.pythonanywhere.com"
        try:
            resp2 = requests.get(f"{webhook_url}/health", timeout=5)
            health = resp2.json(); groq_ok = health.get("groq_key_set", False); gmail_ok = health.get("gmail_set", False)
            st.markdown(f'<div style="background:rgba(16,185,129,0.05);border:1px solid rgba(16,185,129,0.1);border-radius:14px;padding:16px 20px;margin-bottom:24px;display:flex;align-items:center;gap:16px;"><div style="width:10px;height:10px;border-radius:50%;background:#10b981;box-shadow:0 0 10px rgba(16,185,129,0.5);flex-shrink:0;"></div><div><div style="font-size:13px;font-weight:700;color:#34d399;">Webhook Online</div><div style="font-size:11px;color:#1e4a3a;margin-top:2px;">AI: {"&#10003;" if groq_ok else "&#10007;"} &nbsp;&#183;&nbsp; Email: {"&#10003;" if gmail_ok else "&#10007;"} &nbsp;&#183;&nbsp; {webhook_url}</div></div></div>', unsafe_allow_html=True)
        except:
            st.markdown('<div style="background:rgba(239,68,68,0.05);border:1px solid rgba(239,68,68,0.1);border-radius:14px;padding:16px 20px;margin-bottom:24px;display:flex;align-items:center;gap:16px;"><div style="width:10px;height:10px;border-radius:50%;background:#ef4444;flex-shrink:0;"></div><div><div style="font-size:13px;font-weight:700;color:#f87171;">Webhook Offline</div><div style="font-size:11px;color:#4a1a1a;">Check PythonAnywhere account</div></div></div>', unsafe_allow_html=True)
        all_calls_df = get_jobs(); emergency=0; high=0; total_ai=0
        if not all_calls_df.empty:
            for _, r in all_calls_df.iterrows():
                s = parse_summary(r.get("ai_summary",None))
                if s:
                    total_ai+=1; urg=s.get("urgency","").lower()
                    if urg=="emergency": emergency+=1
                    elif urg=="high": high+=1
        v1, v2, v3, v4 = st.columns(4)
        with v1: st.markdown(f'<div class="metric-card blue"><div class="metric-card-top"><div class="metric-icon icon-blue">&#128222;</div><span class="metric-badge badge-blue">AI Handled</span></div><div class="metric-number">{total_ai}</div><div class="metric-title">AI Calls</div></div>', unsafe_allow_html=True)
        with v2: st.markdown(f'<div class="metric-card purple"><div class="metric-card-top"><div class="metric-icon icon-purple">&#127758;</div><span class="metric-badge badge-purple">Auto Detect</span></div><div class="metric-number">6+</div><div class="metric-title">Languages</div></div>', unsafe_allow_html=True)
        with v3: st.markdown(f'<div class="metric-card amber"><div class="metric-card-top"><div class="metric-icon icon-amber">&#9888;&#65039;</div><span class="metric-badge badge-amber">Attention</span></div><div class="metric-number">{high}</div><div class="metric-title">High Urgency</div></div>', unsafe_allow_html=True)
        with v4: st.markdown(f'<div class="metric-card" style="border-color:rgba(239,68,68,0.1);"><div class="metric-card-top"><div class="metric-icon" style="background:rgba(239,68,68,0.1);color:#f87171;">&#128680;</div><span class="metric-badge" style="background:rgba(239,68,68,0.1);color:#f87171;">Immediate</span></div><div class="metric-number">{emergency}</div><div class="metric-title">Emergencies</div></div>', unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        vc1, vc2 = st.columns([1,1], gap="large")
        with vc1:
            st.markdown("<div class='section-header'>Recent AI Calls</div>", unsafe_allow_html=True)
            voice_jobs = get_jobs(); count=0
            if not voice_jobs.empty:
                for _, row in voice_jobs.iterrows():
                    summary = parse_summary(row.get("ai_summary",None))
                    if not summary: continue
                    count+=1; uv = summary.get("urgency","Unknown").lower(); sv2 = summary.get("sentiment","Unknown").lower()
                    uc = {"low":"urgency-low","medium":"urgency-medium","high":"urgency-high","emergency":"urgency-emergency"}.get(uv,"urgency-medium")
                    sc2 = {"calm":"sentiment-calm","frustrated":"sentiment-frustrated","urgent":"sentiment-urgent","angry":"sentiment-angry","satisfied":"sentiment-satisfied","confused":"sentiment-confused"}.get(sv2,"sentiment-calm")
                    fhtml = "".join([f"<div class='followup-item'><span class='followup-dot'>&#8250;</span>{item}</div>" for item in summary.get("follow_up",[])])
                    st.markdown(f'<div class="summary-card"><div class="summary-card-header"><div><div class="summary-customer">{row["customer_name"]}</div><div class="summary-phone">&#128241; {row["phone"]}</div><div style="font-size:10px;color:#a78bfa;margin-top:3px;">&#127758; {summary.get("language","English")}</div></div><div style="text-align:right;"><div class="summary-job-id">JOB #{row["id"]}</div><div style="font-size:11px;color:#1e293b;margin-top:4px;">{str(row.get("timestamp",""))[:10]}</div><div style="margin-top:6px;"><span class="{uc}">{summary.get("urgency","—")}</span></div></div></div><div class="summary-problem"><div class="summary-problem-label">Problem</div><div class="summary-problem-value">{summary.get("problem","—")}</div></div><div class="summary-grid"><div class="summary-field"><div class="summary-field-label">Service</div><div class="summary-field-value">{summary.get("service_type","—")}</div></div><div class="summary-field"><div class="summary-field-label">Tech Skill</div><div class="summary-field-value>{summary.get("tech_skill","—")}</div></div><div class="summary-field"><div class="summary-field-label">Sentiment</div><div class="summary-field-value"><span class="{sc2}">{summary.get("sentiment","—")}</span></div></div><div class="summary-field"><div class="summary-field-label">Address</div><div class="summary-field-value">{str(row.get("keywords","—") or "—")[:28]}</div></div></div><div class="summary-followup"><div class="summary-followup-label">Follow-up</div>{fhtml}</div></div>', unsafe_allow_html=True)
                    if row.get("status")=="Pending Assignment":
                        tdf = get_technicians(status_filter="Active")
                        tlist = tdf["name"].tolist() if not tdf.empty else ["No Active Technicians"]
                        st2 = st.selectbox(f"Assign — Job #{row['id']}", tlist, key=f"vt_{row['id']}")
                        if st.button(f"&#128640;  Dispatch #{row['id']}", key=f"vd_{row['id']}", use_container_width=True):
                            supabase.table("jobs").update({"status":"Dispatched","assigned_tech":st2}).eq("id",row["id"]).execute()
                            st.success(f"Dispatched to {st2}!"); st.rerun()
                    else: st.markdown(f"<div style='font-size:11px;color:#34d399;margin-bottom:8px;'>&#10003; {row.get('status','—')}</div>", unsafe_allow_html=True)
                    st.markdown("<hr style='border:none;border-top:1px solid #050508;margin:6px 0;'>", unsafe_allow_html=True)
            if count==0: st.info("No AI-analysed calls yet.")
        with vc2:
            st.markdown("<div class='section-header'>AI Phone Setup</div>", unsafe_allow_html=True)
            st.markdown(f"<div style='background:#050508;border:1px solid #0f0f1a;border-radius:16px;padding:20px;'><div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'><div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>Webhook URL</div><div style='font-size:12px;color:#6366f1;word-break:break-all;'>{webhook_url}/vapi-webhook</div></div><div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'><div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>AI Receptionist</div><div style='font-size:12px;color:#e2e8f0;'>Alex — Teleron AI Receptionist</div></div><div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'><div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>Languages</div><div style='font-size:12px;color:#e2e8f0;'>English &#183; Urdu &#183; Spanish &#183; Arabic &#183; Hindi &#183; French</div></div><div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;'><div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>Services</div><div style='font-size:12px;color:#e2e8f0;'>HVAC &#183; Plumbing &#183; Electrical &#183; Appliance &#183; Home Services</div></div></div>", unsafe_allow_html=True)

    # ===== TAB 5: HISTORY =====
    with tab5:
        st.markdown("<div class='section-header'>Job History</div>", unsafe_allow_html=True)
        jdf = get_jobs()
        if not jdf.empty:
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("Total Records", len(jdf))
            with c2: st.metric("Dispatched", len(jdf[jdf['status']=='Dispatched']))
            with c3: st.metric("Pending", len(jdf[jdf['status']=='Pending Assignment']))
            st.markdown("<div style='margin-top:16px;'></div>", unsafe_allow_html=True)
            status_counts = jdf['status'].value_counts().reset_index(); status_counts.columns = ['status','count']
            fig_h = dark_fig(200)
            fig_h.add_trace(go.Bar(x=status_counts['status'], y=status_counts['count'], marker=dict(color=['#6366f1','#10b981','#f59e0b','#a78bfa','#f472b6','#34d399','#fb7185','#475569'], opacity=0.85), text=status_counts['count'], textposition='outside', textfont=dict(color='#94a3b8', size=11)))
            st.plotly_chart(fig_h, use_container_width=True, config=CHART_CFG)
            st.markdown("<br>", unsafe_allow_html=True)
            st.dataframe(jdf, use_container_width=True, hide_index=True)
        else: st.info("No job records yet.")

    # ===== TAB 6: ROSTER =====
    with tab6:
        lc, rc = st.columns([1.1,0.9], gap="large")
        with lc:
            st.markdown("<div class='section-header'>Add New Technician</div>", unsafe_allow_html=True)
            with st.form("add_tech_form", clear_on_submit=True):
                t_id = st.text_input("Technician ID", placeholder="T006")
                t_name = st.text_input("Full Name", placeholder="Alex Torres")
                t_zone = st.text_input("Service Zone", placeholder="North District")
                ca, cb = st.columns(2)
                with ca: t_avg = st.text_input("Avg Ticket ($)", placeholder="$320")
                with cb: t_conv = st.text_input("Conversion Rate", placeholder="78%")
                t_status = st.selectbox("Status", ["Active","Inactive","On Leave"])
                if st.form_submit_button("&#10133;  ADD TECHNICIAN", use_container_width=True):
                    if not t_id.strip() or not t_name.strip(): st.error("ID and Name are required.")
                    else:
                        try:
                            insert_technician({"id":t_id.strip(),"name":t_name.strip(),"zone":t_zone.strip(),"avg_ticket":t_avg.strip(),"conversion":t_conv.strip(),"status":t_status})
                            st.success(f"&#9989; {t_name} added!"); st.rerun()
                        except Exception as e: st.error(f"Error: ID may already exist. ({e})")
            st.markdown("<div class='section-header' style='margin-top:28px;'>Update Status</div>", unsafe_allow_html=True)
            rdf = get_technicians()
            if not rdf.empty:
                tmap = {f"{r['name']} ({r['id']})": r['id'] for _,r in rdf.iterrows()}
                sl = st.selectbox("Select Technician", list(tmap.keys()), key="edit_sel")
                sid = tmap[sl]; cs = rdf[rdf['id']==sid]['status'].values[0]
                sopts = ["Active","Inactive","On Leave"]
                ns = st.selectbox("New Status", sopts, index=sopts.index(cs) if cs in sopts else 0, key="edit_status")
                if st.button("&#10003;  UPDATE STATUS", use_container_width=True):
                    update_tech_status(sid, ns); st.success(f"Updated to {ns}."); st.rerun()
            else: st.info("No technicians yet.")
            st.markdown("<div class='section-header' style='margin-top:28px;'>&#128241; Tech Mobile Portal</div>", unsafe_allow_html=True)
            st.markdown("<div style='background:#050508;border:1px solid #0f0f1a;border-radius:16px;padding:20px;'><div style='font-size:12px;color:#475569;line-height:1.7;margin-bottom:14px;'>Each technician gets a unique QR code to scan and open their mobile portal. From there they can update job status, add photos, notes, and collect customer signatures.</div>", unsafe_allow_html=True)
            if st.button("&#128241;  OPEN TECH PORTAL SETUP", use_container_width=True):
                st.session_state.page = "tech_portal_admin"
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with rc:
            st.markdown("<div class='section-header'>Current Roster</div>", unsafe_allow_html=True)
            rdf2 = get_technicians()
            if not rdf2.empty:
                for _, row in rdf2.iterrows():
                    badge = "badge-active" if row['status']=="Active" else "badge-inactive"
                    st.markdown(f'<div class="tech-card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;"><div style="font-size:14px;font-weight:700;color:#f1f5f9;">{row["name"]}</div><span class="{badge}">{row["status"]}</span></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;"><div style="background:#03030a;border:1px solid #0f0f1a;border-radius:8px;padding:8px 10px;"><div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;">ID</div><div style="font-size:12px;color:#94a3b8;font-weight:600;">{row["id"]}</div></div><div style="background:#03030a;border:1px solid #0f0f1a;border-radius:8px;padding:8px 10px;"><div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;">Zone</div><div style="font-size:12px;color:#94a3b8;font-weight:600;">{row.get("zone") or "—"}</div></div><div style="background:#03030a;border:1px solid #0f0f1a;border-radius:8px;padding:8px 10px;"><div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;">Avg Ticket</div><div style="font-size:12px;color:#34d399;font-weight:700;">{row.get("avg_ticket") or "—"}</div></div><div style="background:#03030a;border:1px solid #0f0f1a;border-radius:8px;padding:8px 10px;"><div style="font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;">Conversion</div><div style="font-size:12px;color:#818cf8;font-weight:700;">{row.get("conversion") or "—"}</div></div></div></div>', unsafe_allow_html=True)
                    if st.button(f"&#10005;  Remove {row['name']}", key=f"del_{row['id']}", use_container_width=True):
                        delete_technician(row['id']); st.warning(f"{row['name']} removed."); st.rerun()
                st.markdown("<div class='section-header' style='margin-top:24px;'>Full Roster Table</div>", unsafe_allow_html=True)
                st.dataframe(rdf2, use_container_width=True, hide_index=True)
            else: st.markdown("<div style='text-align:center;padding:60px 20px;color:#0f0f1a;'><div style='font-size:42px;margin-bottom:12px;'>&#128119;</div><div style='font-size:13px;font-weight:600;'>No technicians added yet</div></div>", unsafe_allow_html=True)

    # ===== TAB 7: INVOICES =====
    with tab7:
        st.markdown("<div class='section-header'>Invoice Generation Center</div>", unsafe_allow_html=True)
        inv_left, inv_right = st.columns([1, 1.3], gap="large")
        with inv_left:
            st.markdown("<div style='font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:14px;'>&#128221; Create New Invoice</div>", unsafe_allow_html=True)
            all_jobs_inv = get_jobs()
            if all_jobs_inv.empty: st.info("No jobs available to invoice.")
            else:
                job_opts = {f"#{r['id']} — {r['customer_name']} ({r.get('status', '—')})": r for _, r in all_jobs_inv.iterrows()}
                selected_job_label = st.selectbox("Select Job", list(job_opts.keys()), key="inv_job_sel")
                job_row = job_opts[selected_job_label]
                c_name = job_row.get("customer_name", "")
                c_phone = job_row.get("phone", "")
                c_addr = str(job_row.get("keywords", "") or "")
                svc_type = ""
                if job_row.get("ai_summary"):
                    try:
                        s = parse_summary(job_row["ai_summary"])
                        if s: svc_type = s.get("service_type", "")
                    except: pass
                inv_num = st.text_input("Invoice #", value=f"INV-{job_row['id']}-{datetime.now().strftime('%y%m%d')}", key="inv_num")
                inv_date = st.date_input("Invoice Date", value=datetime.now().date(), key="inv_date")
                due_date = st.date_input("Due Date", value=(datetime.now().date() + pd.Timedelta(days=14)), key="inv_due")
                tax_rate = st.number_input("Tax Rate (%)", min_value=0.0, max_value=50.0, value=8.0, step=0.5, key="inv_tax")
                st.markdown("<div style='font-size:11px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1px;margin:16px 0 10px;'>Line Items</div>", unsafe_allow_html=True)
                line_items = []
                for i in range(5):
                    c_a, c_b, c_c = st.columns([2.5, 0.8, 1.2])
                    with c_a: desc = st.text_input(f"Description {i+1}", value=svc_type if i==0 else "", placeholder="Service description", key=f"li_desc_{i}", label_visibility="collapsed")
                    with c_b: qty = st.number_input(f"Qty {i+1}", min_value=0, max_value=999, value=1 if i==0 else 0, step=1, key=f"li_qty_{i}", label_visibility="collapsed")
                    with c_c: rate = st.number_input(f"Rate {i+1}", min_value=0.0, max_value=99999.0, value=0.0, step=10.0, format="%.2f", key=f"li_rate_{i}", label_visibility="collapsed")
                    if desc.strip() and qty > 0 and rate > 0:
                        line_items.append({"description": desc, "qty": qty, "rate": rate, "amount": round(qty * rate, 2)})
                subtotal = sum(x["amount"] for x in line_items)
                tax_amt = round(subtotal * (tax_rate / 100), 2)
                total = round(subtotal + tax_amt, 2)
                st.markdown(f'<div style="background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px 16px;margin-top:10px;"><div style="display:flex;justify-content:space-between;font-size:12px;color:#475569;margin-bottom:6px;"><span>Subtotal</span><span style="color:#94a3b8;font-weight:600;">${subtotal:,.2f}</span></div><div style="display:flex;justify-content:space-between;font-size:12px;color:#475569;margin-bottom:6px;"><span>Tax ({tax_rate}%)</span><span style="color:#94a3b8;font-weight:600;">${tax_amt:,.2f}</span></div><div style="border-top:1px solid #0f0f1a;margin:8px 0;padding-top:8px;display:flex;justify-content:space-between;font-size:15px;font-weight:700;color:#f43f5e;"><span>TOTAL</span><span>${total:,.2f}</span></div></div>', unsafe_allow_html=True)
                notes = st.text_area("Payment Terms / Notes", height=68, value="Payment due within 14 days. Make checks payable to Teleron Central Dispatch.", key="inv_notes")
                c_save, c_clear = st.columns(2)
                with c_save:
                    if st.button("&#128178;  SAVE INVOICE", use_container_width=True, key="inv_save_btn"):
                        if not line_items: st.error("Add at least one line item.")
                        else:
                            payload = {"invoice_number": inv_num, "job_id": job_row["id"], "customer_name": c_name, "phone": c_phone, "address": c_addr, "invoice_date": str(inv_date), "due_date": str(due_date), "line_items": json.dumps(line_items), "subtotal": subtotal, "tax_rate": tax_rate, "tax_amount": tax_amt, "total": total, "notes": notes, "status": "Sent", "created_at": datetime.now().isoformat()}
                            with st.spinner("Saving..."):
                                res = insert_invoice(payload)
                                if isinstance(res, dict) and res.get("error"):
                                    st.warning(f"Supabase table missing? Storing locally. {res['error']}")
                                    payload["id"] = f"LOCAL-{len(st.session_state.invoices_local)+1}"
                                    st.session_state.invoices_local.append(payload)
                                    st.success("&#9989; Invoice saved locally!")
                                else: st.success("&#9989; Invoice saved to database!")
                            supabase.table("jobs").update({"status": "Invoiced"}).eq("id", job_row["id"]).execute()
                            st.rerun()
                with c_clear:
                    if st.button("&#128465;&#65039;  CLEAR", use_container_width=True, key="inv_clear_btn"):
                        for k in list(st.session_state.keys()):
                            if k.startswith("li_") or k in ["inv_num","inv_date","inv_due","inv_tax","inv_notes","inv_job_sel"]:
                                if k in st.session_state: del st.session_state[k]
                        st.rerun()
        with inv_right:
            st.markdown("<div style='font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:14px;'>&#128196; Live Preview</div>", unsafe_allow_html=True)
            preview_items = []; 
            for i in range(5):
                d = st.session_state.get(f"li_desc_{i}", ""); q = st.session_state.get(f"li_qty_{i}", 0); r = st.session_state.get(f"li_rate_{i}", 0.0)
                if d and q > 0 and r > 0: preview_items.append({"desc": d, "qty": q, "rate": r, "amt": round(q*r, 2)})
            p_tax = st.session_state.get("inv_tax", 8.0); p_sub = sum(x["amt"] for x in preview_items); p_tax_amt = round(p_sub * (p_tax/100), 2); p_total = round(p_sub + p_tax_amt, 2)
            p_num = st.session_state.get("inv_num", "INV-000"); p_date = st.session_state.get("inv_date", datetime.now().date()); p_due = st.session_state.get("inv_due", datetime.now().date()); p_notes = st.session_state.get("inv_notes", "")
            sel_job_name, sel_job_phone, sel_job_addr = "Customer", "—", "—"
            try:
                all_j = get_jobs()
                if not all_j.empty:
                    opts = {f"#{r['id']} — {r['customer_name']} ({r.get('status','—')})": r for _,r in all_j.iterrows()}
                    cur = st.session_state.get("inv_job_sel", list(opts.keys())[0])
                    if cur in opts:
                        jr = opts[cur]; sel_job_name = jr.get("customer_name", "Customer"); sel_job_phone = jr.get("phone", "—"); sel_job_addr = str(jr.get("keywords", "") or "—")
            except: pass
            rows_html = ""
            for it in preview_items:
                rows_html += f"<tr><td style='padding:10px 12px;font-size:12px;color:#cbd5e1;border-bottom:1px solid #0f0f1a;'>{it['desc']}</td><td style='padding:10px 12px;font-size:12px;color:#94a3b8;text-align:center;border-bottom:1px solid #0f0f1a;'>{it['qty']}</td><td style='padding:10px 12px;font-size:12px;color:#94a3b8;text-align:right;border-bottom:1px solid #0f0f1a;'>${it['rate']:,.2f}</td><td style='padding:10px 12px;font-size:12px;color:#e2e8f0;text-align:right;font-weight:600;border-bottom:1px solid #0f0f1a;'>${it['amt']:,.2f}</td></tr>"
            if not rows_html: rows_html = "<tr><td colspan='4' style='padding:20px;text-align:center;color:#334155;font-size:12px;'>No line items yet. Add services on the left.</td></tr>"
            preview_html = f"""<div style='background:#050508;border:1px solid #0f0f1a;border-radius:20px;padding:28px 32px;max-width:540px;margin:0 auto;box-shadow:0 20px 60px rgba(0,0,0,0.4);'><div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px;'><div><div style='font-family:Space Grotesk,sans-serif;font-size:22px;font-weight:700;color:#f8fafc;letter-spacing:-0.5px;'>TELERON</div><div style='font-size:10px;font-weight:600;color:#334155;text-transform:uppercase;letter-spacing:2px;'>Central Dispatch · HVAC & Home Services</div></div><div style='text-align:right;'><div style='font-size:11px;font-weight:700;color:#f43f5e;text-transform:uppercase;letter-spacing:1px;'>INVOICE</div><div style='font-size:16px;font-weight:700;color:#f1f5f9;margin-top:4px;'>{p_num}</div></div></div><div style='display:flex;justify-content:space-between;margin-bottom:24px;padding-bottom:20px;border-bottom:1px solid #0f0f1a;'><div><div style='font-size:9px;font-weight:800;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>Bill To</div><div style='font-size:13px;font-weight:700;color:#f1f5f9;'>{sel_job_name}</div><div style='font-size:11px;color:#475569;margin-top:3px;'>&#128241; {sel_job_phone}</div><div style='font-size:11px;color:#475569;margin-top:2px;max-width:180px;line-height:1.5;'>&#128205; {sel_job_addr[:80]}</div></div><div style='text-align:right;'><div style='font-size:9px;font-weight:800;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>Invoice Details</div><div style='font-size:11px;color:#94a3b8;margin-bottom:4px;'>Date: <span style='color:#cbd5e1;'>{p_date}</span></div><div style='font-size:11px;color:#94a3b8;'>Due: <span style='color:#cbd5e1;'>{p_due}</span></div></div></div><table style='width:100%;border-collapse:collapse;margin-bottom:16px;'><thead><tr style='background:#03030a;'><th style='padding:10px 12px;font-size:9px;font-weight:800;color:#475569;text-transform:uppercase;letter-spacing:1px;text-align:left;border-bottom:1px solid #1a1a2e;'>Description</th><th style='padding:10px 12px;font-size:9px;font-weight:800;color:#475569;text-transform:uppercase;letter-spacing:1px;text-align:center;border-bottom:1px solid #1a1a2e;'>Qty</th><th style='padding:10px 12px;font-size:9px;font-weight:800;color:#475569;text-transform:uppercase;letter-spacing:1px;text-align:right;border-bottom:1px solid #1a1a2e;'>Rate</th><th style='padding:10px 12px;font-size:9px;font-weight:800;color:#475569;text-transform:uppercase;letter-spacing:1px;text-align:right;border-bottom:1px solid #1a1a2e;'>Amount</th></tr></thead><tbody>{rows_html}</tbody></table><div style='display:flex;justify-content:flex-end;margin-bottom:20px;'><div style='width:220px;'><div style='display:flex;justify-content:space-between;font-size:12px;color:#475569;margin-bottom:6px;'><span>Subtotal</span><span style='color:#94a3b8;font-weight:600;'>${p_sub:,.2f}</span></div><div style='display:flex;justify-content:space-between;font-size:12px;color:#475569;margin-bottom:10px;'><span>Tax ({p_tax}%)</span><span style='color:#94a3b8;font-weight:600;'>${p_tax_amt:,.2f}</span></div><div style='border-top:1px solid #1a1a2e;padding-top:10px;display:flex;justify-content:space-between;font-size:16px;font-weight:700;color:#f43f5e;'><span>TOTAL</span><span>${p_total:,.2f}</span></div></div></div><div style='background:#03030a;border:1px solid #0f0f1a;border-radius:10px;padding:12px 14px;margin-bottom:16px;'><div style='font-size:9px;font-weight:800;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>Notes & Payment Terms</div><div style='font-size:11px;color:#475569;line-height:1.6;'>{p_notes}</div></div><div style='text-align:center;font-size:10px;color:#1e293b;padding-top:8px;border-top:1px solid #0f0f1a;'>Thank you for choosing Teleron Central Dispatch · support@teleron.com · (555) 019-2834</div></div>"""
            st.markdown(preview_html, unsafe_allow_html=True)
            st.markdown("<div class='section-header' style='margin-top:28px;'>Invoice History</div>", unsafe_allow_html=True)
            inv_db = get_invoices(); inv_local = pd.DataFrame(st.session_state.get("invoices_local", []))
            if not inv_db.empty or not inv_local.empty:
                if not inv_db.empty and not inv_local.empty: inv_all = pd.concat([inv_db, inv_local], ignore_index=True)
                elif not inv_db.empty: inv_all = inv_db
                else: inv_all = inv_local
                inv_all = inv_all.sort_values("created_at", ascending=False) if "created_at" in inv_all.columns else inv_all
                for _, inv in inv_all.head(20).iterrows():
                    li = []; 
                    try:
                        raw = inv.get("line_items", "[]")
                        if isinstance(raw, str): li = json.loads(raw)
                        else: li = raw
                    except: li = []
                    li_count = len(li)
                    st.markdown(f"<div style='background:#050508;border:1px solid #0f0f1a;border-radius:14px;padding:16px 18px;margin-bottom:10px;'><div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;'><div style='font-size:14px;font-weight:700;color:#f1f5f9;'>{inv.get('invoice_number','—')}</div><span style='background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.2);border-radius:99px;padding:3px 10px;font-size:10px;font-weight:700;color:#818cf8;'>{inv.get('status','—')}</span></div><div style='font-size:12px;color:#475569;margin-bottom:6px;'>&#128100; {inv.get('customer_name','—')} · &#128241; {inv.get('phone','—')}</div><div style='display:flex;justify-content:space-between;align-items:center;'><div style='font-size:11px;color:#334155;'>&#128197; {inv.get('invoice_date','—')} · {li_count} items · Tax {inv.get('tax_rate',0)}%</div><div style='font-size:14px;font-weight:700;color:#f43f5e;'>${inv.get('total',0):,.2f}</div></div></div>", unsafe_allow_html=True)
                    c1, c2, c3 = st.columns([1,1,1])
                    with c1:
                        if st.button("&#10003;  Mark Paid", key=f"inv_pay_{inv.get('id','x')}", use_container_width=True):
                            update_invoice_status(inv.get('id'), "Paid"); st.success("Marked Paid!"); st.rerun()
                    with c2:
                        if st.button("&#9993;&#65039;  Resend", key=f"inv_resend_{inv.get('id','x')}", use_container_width=True):
                            st.info("Email integration ready — connect SMTP to send.")
                    with c3:
                        if st.button("&#128465;&#65039;  Delete", key=f"inv_del_{inv.get('id','x')}", use_container_width=True):
                            delete_invoice(inv.get('id')); st.warning("Deleted."); st.rerun()
            else: st.markdown("<div style='text-align:center;padding:40px;color:#1e293b;'><div style='font-size:32px;margin-bottom:10px;'>&#128462;</div><div style='font-size:13px;font-weight:600;'>No invoices yet. Create your first one on the left.</div></div>", unsafe_allow_html=True)

    # ===== TAB 8: CUSTOMER CRM =====
    with tab8:
        st.markdown("<div class='section-header'>&#128100; Customer Relationship Manager</div>", unsafe_allow_html=True)
        crm_search = st.text_input("Search by name or phone...", placeholder="Enter customer name or phone number", key="crm_search_input")

        if crm_search and len(crm_search) >= 2:
            all_cust = get_customers()
            all_jobs_crm = get_jobs()
            matches = []
            if not all_cust.empty:
                for _, c in all_cust.iterrows():
                    if crm_search.lower() in str(c.get('name','')).lower() or crm_search in str(c.get('phone','')):
                        matches.append(c)
            if not all_jobs_crm.empty:
                for _, j in all_jobs_crm.iterrows():
                    found = False
                    for m in matches:
                        if m.get('phone') == j.get('phone'): found = True; break
                    if not found and (crm_search.lower() in str(j.get('customer_name','')).lower() or crm_search in str(j.get('phone',''))):
                        matches.append({"name": j.get('customer_name'), "phone": j.get('phone'), "address": j.get('keywords',''), "_from_job": True})

            if matches:
                st.markdown(f"<div style='font-size:11px;color:#475569;margin-bottom:14px;'>Found {len(matches)} matching customer(s)</div>", unsafe_allow_html=True)
                for cust in matches:
                    cname = cust.get('name', 'Unknown')
                    cphone = cust.get('phone', '—')
                    caddr = str(cust.get('address', '') or cust.get('keywords', '') or '—')
                    cjobs = get_customer_jobs(cphone) if cphone != '—' else pd.DataFrame()
                    cinv = get_customer_invoices(cphone) if cphone != '—' else pd.DataFrame()
                    total_jobs_c = len(cjobs)
                    completed_jobs = len(cjobs[cjobs['status'] == 'Completed']) if not cjobs.empty else 0
                    total_inv_c = len(cinv)
                    unpaid = sum(cinv[cinv['status'] != 'Paid']['total']) if not cinv.empty and 'total' in cinv.columns else 0.0
                    equip = get_equipment(cphone) if cphone != '—' else pd.DataFrame()

                    st.markdown(f'<div class="crm-profile-card"><div style="display:flex;align-items:flex-start;gap:20px;"><div class="crm-avatar">&#128100;</div><div style="flex:1;"><div class="crm-name">{cname}</div><div class="crm-phone">&#128241; {cphone}</div><div style="font-size:11px;color:#475569;margin-top:3px;max-width:300px;line-height:1.5;">&#128205; {caddr[:100]}</div></div><div style="text-align:right;">{status_pill_html("Paid") if completed_jobs > 0 else status_pill_html("Pending Assignment")}</div></div><div class="crm-stats"><div class="crm-stat-box"><div class="crm-stat-num" style="color:#818cf8;">{total_jobs_c}</div><div class="crm-stat-label">Total Jobs</div></div><div class="crm-stat-box"><div class="crm-stat-num" style="color:#34d399;">{completed_jobs}</div><div class="crm-stat-label">Completed</div></div><div class="crm-stat-box"><div class="crm-stat-num" style="color:#fbbf24;">{total_inv_c}</div><div class="crm-stat-label">Invoices</div></div><div class="crm-stat-box"><div class="crm-stat-num" style="color:{"#f87171" if unpaid > 0 else "#34d399"};">${unpaid:,.0f}</div><div class="crm-stat-label">Balance Due</div></div></div></div>', unsafe_allow_html=True)

                    if unpaid > 0:
                        st.markdown(f'<div class="crm-alert-banner"><div class="crm-alert-icon">&#9888;&#65039;</div><div><div style="font-size:12px;font-weight:700;color:#f87171;">Outstanding Balance</div><div style="font-size:11px;color:#475569;">Customer has ${unpaid:,.2f} in unpaid invoices. Follow up recommended.</div></div></div>', unsafe_allow_html=True)

                    st.markdown("<div style='font-size:11px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin:20px 0 14px;'>&#128221; Service History</div>", unsafe_allow_html=True)
                    if not cjobs.empty:
                        for _, job in cjobs.head(8).iterrows():
                            j_summary = parse_summary(job.get("ai_summary", None))
                            urgency_color = "#f87171" if j_summary and j_summary.get("urgency") == "Emergency" else "#fbbf24" if j_summary and j_summary.get("urgency") == "High" else "#475569"
                            problem_html = f'<div style="font-size:11px;color:{urgency_color};margin-bottom:4px;">Problem: {j_summary.get("problem","")[:80]}</div>' if j_summary else ""
                            st.markdown(f'<div class="crm-history-item"><div class="crm-history-status">{status_pill_html(job.get("status","—"))}</div><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;"><div style="font-size:13px;font-weight:700;color:#f1f5f9;">JOB #{job["id"]}</div><div style="font-size:10px;color:#334155;">{str(job.get("scheduled_date",""))}</div></div><div style="font-size:12px;color:#475569;margin-bottom:6px;">{job.get("keywords","")[:60]}</div>{problem_html}<div style="font-size:10px;color:#334155;">Tech: {job.get("assigned_tech","Unassigned")}</div></div>', unsafe_allow_html=True)
                    else:
                        st.markdown("<div style='text-align:center;padding:30px;color:#1e293b;font-size:13px;'>No service history found</div>", unsafe_allow_html=True)

                    st.markdown("<div style='font-size:11px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin:20px 0 14px;'>&#128295; Equipment on File</div>", unsafe_allow_html=True)
                    if not equip.empty:
                        for _, eq in equip.iterrows():
                            st.markdown(f'<div class="crm-equipment-card"><div class="crm-equipment-icon">&#128295;</div><div><div style="font-size:13px;font-weight:700;color:#f1f5f9;">{eq.get("name","—")}</div><div style="font-size:11px;color:#475569;">{eq.get("type","—")} · Installed: {eq.get("install_date","—")} · {eq.get("warranty_status","—")}</div></div></div>', unsafe_allow_html=True)
                    else:
                        st.markdown("<div style='text-align:center;padding:20px;color:#1e293b;font-size:12px;'>No equipment recorded. Add equipment to track warranty & maintenance.</div>", unsafe_allow_html=True)

                    with st.form(f"add_equip_{cphone}"):
                        st.markdown("<div style='font-size:10px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;'>Add Equipment</div>", unsafe_allow_html=True)
                        eq1, eq2 = st.columns(2)
                        with eq1: eq_name = st.text_input("Equipment Name", placeholder="Carrier Infinity 26", key=f"eq_name_{cphone}")
                        with eq2: eq_type = st.selectbox("Type", ["HVAC Unit","Water Heater","Electrical Panel","Plumbing Fixture","Appliance","Generator","Other"], key=f"eq_type_{cphone}")
                        eq3, eq4 = st.columns(2)
                        with eq3: eq_model = st.text_input("Model #", placeholder="24VNA6", key=f"eq_model_{cphone}")
                        with eq4: eq_install = st.date_input("Install Date", key=f"eq_install_{cphone}")
                        eq_warranty = st.selectbox("Warranty Status", ["Under Warranty","Extended","Expired","Unknown"], key=f"eq_warranty_{cphone}")
                        if st.form_submit_button("&#10133; Add Equipment"):
                            if eq_name.strip():
                                insert_equipment({"customer_phone": cphone, "name": eq_name.strip(), "type": eq_type, "model": eq_model.strip(), "install_date": str(eq_install), "warranty_status": eq_warranty})
                                st.success("Equipment added!"); st.rerun()

                    st.markdown("<hr style='border-color:#0f0f1a;margin:24px 0;'>", unsafe_allow_html=True)
            else:
                st.info("No customers found. Try a different search, or create a job to auto-add customers.")
        else:
            st.markdown("<div style='text-align:center;padding:60px 20px;'><div style='font-size:42px;margin-bottom:16px;'>&#128100;</div><div style='font-size:14px;font-weight:600;color:#334155;margin-bottom:8px;'>Customer CRM</div><div style='font-size:12px;color:#475569;max-width:360px;margin:0 auto;line-height:1.7;'>Search by customer name or phone number to view their complete profile, service history, equipment on file, outstanding invoices, and AI-analyzed call summaries.</div></div>", unsafe_allow_html=True)

# =======================================================================
# TECH PORTAL ADMIN — QR Code Generator
# =======================================================================
def page_tech_portal_admin():
    render_header(show_back=True, page_title="Tech Mobile Portal Setup")
    st.markdown("<div class='section-header'>&#128241; Technician Mobile Portal Access</div>", unsafe_allow_html=True)
    st.markdown("<div style='font-size:12px;color:#475569;line-height:1.7;margin-bottom:24px;'>Each technician can access their mobile portal by scanning a unique QR code. From the portal they can update job status in real-time, upload photos, add field notes, and collect customer signatures. No app installation required.</div>", unsafe_allow_html=True)

    all_techs = get_technicians()
    if all_techs.empty:
        st.info("No technicians found. Add technicians in the ROSTER tab first.")
        return

    for _, tech in all_techs.iterrows():
        col1, col2, col3 = st.columns([1, 2, 1])
        with col1:
            st.markdown(f'<div style="background:linear-gradient(135deg,#6366f1,#8b5cf6);width:52px;height:52px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;">&#128119;</div>', unsafe_allow_html=True)
        with col2:
            st.markdown(f'<div style="font-size:15px;font-weight:700;color:#f1f5f9;">{tech["name"]}</div><div style="font-size:11px;color:#475569;">{tech.get("zone","No zone")} · {tech["status"]}</div>', unsafe_allow_html=True)
        with col3:
            if st.button(f"&#128241; Portal", key=f"portal_btn_{tech['id']}", use_container_width=True):
                st.session_state.portal_tech_id = tech['id']
                st.session_state.portal_tech_name = tech['name']
                st.session_state.page = "tech_portal"
                st.rerun()
        st.markdown("<hr style='border-color:#0f0f1a;margin:12px 0;'>", unsafe_allow_html=True)

# =======================================================================
# TECH MOBILE PORTAL — Technician Field Interface
# =======================================================================
def page_tech_portal():
    tech_id = st.session_state.get("portal_tech_id")
    tech_name = st.session_state.get("portal_tech_name", "Technician")
    if not tech_id:
        st.error("No technician selected. Go back and select a technician.")
        if st.button("&#8592; Back to Portal Setup"):
            st.session_state.page = "tech_portal_admin"
            st.rerun()
        return

    st.markdown(f"""
    <div class="portal-mobile-header">
        <div style="font-size:10px;font-weight:600;color:#475569;text-transform:uppercase;letter-spacing:2px;margin-bottom:4px;">Teleron Field Portal</div>
        <div class="portal-mobile-title">{tech_name}</div>
        <div style="font-size:11px;color:#34d399;margin-top:4px;">&#128994; Online · {datetime.now().strftime("%I:%M %p")}</div>
    </div>
    """, unsafe_allow_html=True)

    if st.button("&#8592; Log Out", use_container_width=True):
        st.session_state.portal_tech_id = None
        st.session_state.portal_tech_name = None
        st.session_state.page = "tech_portal_admin"
        st.rerun()

    st.markdown("<div class='section-header' style='margin-top:16px;'>&#128208; My Active Jobs</div>", unsafe_allow_html=True)

    all_jobs = get_jobs()
    tech_jobs = all_jobs[all_jobs['assigned_tech'] == tech_name] if not all_jobs.empty and 'assigned_tech' in all_jobs.columns else pd.DataFrame()
    active_statuses = ["Assigned", "En Route", "Arrived", "In Progress"]
    active_jobs = tech_jobs[tech_jobs['status'].isin(active_statuses)] if not tech_jobs.empty else pd.DataFrame()

    if active_jobs.empty:
        st.markdown("<div style='text-align:center;padding:40px 20px;'><div style='font-size:36px;margin-bottom:10px;'>&#9989;</div><div style='font-size:13px;font-weight:600;color:#334155;'>No active jobs right now</div><div style='font-size:11px;color:#475569;margin-top:6px;'>Check back when the dispatcher assigns you a job.</div></div>", unsafe_allow_html=True)
    else:
        for _, job in active_jobs.iterrows():
            addr = str(job.get('keywords','') or '—')
            current_status = job.get('status', 'Assigned')
            if current_status not in STATUS_PIPELINE:
                current_status = "Assigned"
            
            st.markdown(f"""
            <div class="portal-job-card">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    <div style="font-size:14px;font-weight:700;color:#f1f5f9;">JOB #{job['id']}</div>
                    {status_pill_html(current_status)}
                </div>
                <div style="font-size:16px;font-weight:700;color:#f8fafc;margin-bottom:6px;">{job['customer_name']}</div>
                <div style="font-size:12px;color:#475569;margin-bottom:4px;">&#128241; {job['phone']}</div>
                <div style="font-size:12px;color:#475569;margin-bottom:14px;line-height:1.5;">&#128205; {addr[:80]}</div>
            </div>
            """, unsafe_allow_html=True)

            ts_data = {k: job.get(k, '') for k in ['time_pending_assignment','time_assigned','time_en_route','time_arrived','time_in_progress','time_completed','time_invoiced','time_paid']}
            st.markdown(pipeline_html(current_status, ts_data), unsafe_allow_html=True)

            st.markdown("<div style='font-size:10px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1px;margin:14px 0 10px;'>Update Status</div>", unsafe_allow_html=True)
            nxt = get_next_status(current_status)
            prev = get_prev_status(current_status)
            b1, b2 = st.columns(2)
            with b1:
                if nxt != current_status:
                    if st.button(f"&#9654; Mark {nxt}", key=f"tp_next_{job['id']}", use_container_width=True):
                        update_job_status(job['id'], nxt)
                        log_sms(job.get('phone',''), f"Teleron Update: Your technician {tech_name} is now {nxt.lower()}. JOB #{job['id']}")
                        st.success(f"Status updated to {nxt}!"); st.rerun()
            with b2:
                if prev != current_status:
                    if st.button(f"&#9664; Back to {prev}", key=f"tp_prev_{job['id']}", use_container_width=True):
                        update_job_status(job['id'], prev)
                        st.rerun()

            st.markdown("<div style='font-size:10px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1px;margin:16px 0 10px;'>Field Notes</div>", unsafe_allow_html=True)
            with st.form(f"tp_note_{job['id']}"):
                note_text = st.text_area("Add a note", placeholder="Parts used, issues found, customer comments...", key=f"tp_note_txt_{job['id']}", height=80)
                if st.form_submit_button("&#128178; Save Note"):
                    if note_text.strip():
                        insert_job_note({"job_id": job['id'], "note": note_text.strip(), "author": tech_name, "timestamp": datetime.now().isoformat()})
                        st.success("Note saved!"); st.rerun()

            st.markdown("<div style='font-size:10px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1px;margin:16px 0 10px;'>Photos</div>", unsafe_allow_html=True)
            uploaded = st.file_uploader("Upload photo", type=["jpg","jpeg","png"], key=f"tp_photo_{job['id']}")
            if uploaded:
                img_b64 = base64.b64encode(uploaded.read()).decode()
                if st.button(f"&#128247; Save Photo", key=f"tp_save_photo_{job['id']}", use_container_width=True):
                    insert_job_photo({"job_id": job['id'], "image_b64": img_b64[:50000], "caption": uploaded.name, "timestamp": datetime.now().isoformat()})
                    st.success("Photo saved!"); st.rerun()

            photos = get_job_photos(job['id'])
            if not photos.empty:
                for _, ph in photos.iterrows():
                    img_data = ph.get('image_b64','')
                    if img_data:
                        st.markdown(f'<div style="border-radius:12px;overflow:hidden;border:1px solid #0f0f1a;margin-bottom:8px;"><img src="data:image/jpeg;base64,{img_data}" style="width:100%;display:block;"></div>', unsafe_allow_html=True)

            st.markdown("<div style='font-size:10px;font-weight:700;color:#334155;text-transform:uppercase;letter-spacing:1px;margin:16px 0 10px;'>Customer Signature</div>", unsafe_allow_html=True)
            sigs = get_job_signatures(job['id'])
            if not sigs.empty:
                st.markdown("<div style='background:rgba(16,185,129,0.05);border:1px solid rgba(16,185,129,0.15);border-radius:10px;padding:10px 14px;font-size:12px;color:#34d399;'>&#9989; Signature collected</div>", unsafe_allow_html=True)
            else:
                sig_name = st.text_input("Customer Name for Signature", key=f"tp_sig_name_{job['id']}")
                if sig_name.strip() and st.button(f"&#9998; Collect Signature", key=f"tp_sig_{job['id']}", use_container_width=True):
                    insert_signature({"job_id": job['id'], "customer_name": sig_name.strip(), "technician_name": tech_name, "timestamp": datetime.now().isoformat()})
                    st.success("Signature recorded!"); st.rerun()

            st.markdown("<hr style='border-color:#0f0f1a;margin:24px 0;'>", unsafe_allow_html=True)

    st.markdown("<div class='section-header'>&#9989; Recently Completed</div>", unsafe_allow_html=True)
    done_jobs = tech_jobs[tech_jobs['status'].isin(["Completed","Invoiced","Paid"])] if not tech_jobs.empty else pd.DataFrame()
    if not done_jobs.empty:
        for _, job in done_jobs.head(5).iterrows():
            st.markdown(f'<div class="portal-job-card" style="opacity:0.7;"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;"><div style="font-size:13px;font-weight:700;color:#f1f5f9;">JOB #{job["id"]}</div>{status_pill_html(job.get("status","Completed"))}</div><div style="font-size:14px;font-weight:600;color:#f8fafc;">{job["customer_name"]}</div><div style="font-size:11px;color:#475569;">{str(job.get("keywords",""))[:50]}</div></div>', unsafe_allow_html=True)
    else:
        st.markdown("<div style='text-align:center;padding:20px;color:#1e293b;font-size:12px;'>No completed jobs yet</div>", unsafe_allow_html=True)

# =======================================================================
# CUSTOMER CRM FULL PAGE
# =======================================================================
def page_customer_crm():
    render_header(show_back=True, page_title="Customer CRM")
    st.markdown("<div class='section-header'>&#128100; Customer Search</div>", unsafe_allow_html=True)
    search = st.text_input("Search customer by name or phone", placeholder="John Smith or +1555000...", key="crm_full_search")
    if search and len(search) >= 2:
        all_cust = get_customers(); all_jobs_crm = get_jobs()
        matches = []
        if not all_cust.empty:
            for _, c in all_cust.iterrows():
                if search.lower() in str(c.get('name','')).lower() or search in str(c.get('phone','')):
                    matches.append(dict(c))
        if not all_jobs_crm.empty:
            for _, j in all_jobs_crm.iterrows():
                found = any(m.get('phone') == j.get('phone') for m in matches)
                if not found and (search.lower() in str(j.get('customer_name','')).lower() or search in str(j.get('phone',''))):
                    matches.append({"name": j.get('customer_name'), "phone": j.get('phone'), "address": j.get('keywords',''), "_from_job": True})
        if matches:
            for cust in matches:
                cname = cust.get('name', 'Unknown'); cphone = cust.get('phone', '—')
                caddr = str(cust.get('address','') or cust.get('keywords','') or '—')
                cjobs = get_customer_jobs(cphone) if cphone != '—' else pd.DataFrame()
                cinv = get_customer_invoices(cphone) if cphone != '—' else pd.DataFrame()
                total_jobs_c = len(cjobs); completed_jobs = len(cjobs[cjobs['status'] == 'Completed']) if not cjobs.empty else 0
                total_inv_c = len(cinv); unpaid = sum(cinv[cinv['status'] != 'Paid']['total']) if not cinv.empty and 'total' in cinv.columns else 0.0
                equip = get_equipment(cphone) if cphone != '—' else pd.DataFrame()
                st.markdown(f'<div class="crm-profile-card"><div class="crm-avatar">&#128100;</div><div class="crm-name">{cname}</div><div class="crm-phone">&#128241; {cphone}</div><div style="font-size:11px;color:#475569;margin-top:4px;max-width:400px;">&#128205; {caddr[:120]}</div><div class="crm-stats"><div class="crm-stat-box"><div class="crm-stat-num" style="color:#818cf8;">{total_jobs_c}</div><div class="crm-stat-label">Total Jobs</div></div><div class="crm-stat-box"><div class="crm-stat-num" style="color:#34d399;">{completed_jobs}</div><div class="crm-stat-label">Completed</div></div><div class="crm-stat-box"><div class="crm-stat-num" style="color:#fbbf24;">{total_inv_c}</div><div class="crm-stat-label">Invoices</div></div><div class="crm-stat-box"><div class="crm-stat-num" style="color:{"#f87171" if unpaid > 0 else "#34d399"};">${unpaid:,.0f}</div><div class="crm-stat-label">Balance Due</div></div></div></div>', unsafe_allow_html=True)
                if unpaid > 0: st.markdown(f'<div class="crm-alert-banner"><div class="crm-alert-icon">&#9888;&#65039;</div><div><div style="font-size:12px;font-weight:700;color:#f87171;">Outstanding Balance: ${unpaid:,.2f}</div><div style="font-size:11px;color:#475569;">Follow up recommended for payment collection.</div></div></div>', unsafe_allow_html=True)
                t1, t2, t3 = st.tabs(["&#128221; History", "&#128295; Equipment", "&#128172; Quick Actions"])
                with t1:
                    if not cjobs.empty:
                        for _, job in cjobs.iterrows():
                            js = parse_summary(job.get("ai_summary"))
                            problem_html2 = f'<div style="font-size:11px;color:#fbbf24;margin-top:4px;">Problem: {js.get("problem","")[:60]}</div>' if js else ""
                            st.markdown(f'<div class="crm-history-item"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;"><div style="font-size:13px;font-weight:700;color:#f1f5f9;">JOB #{job["id"]}</div>{status_pill_html(job.get("status","—"))}</div><div style="font-size:12px;color:#475569;">{job.get("keywords","")[:60]}</div>{problem_html2}<div style="font-size:10px;color:#334155;margin-top:4px;">Tech: {job.get("assigned_tech","Unassigned")} · {str(job.get("scheduled_date",""))}</div></div>', unsafe_allow_html=True)
                    else: st.info("No job history.")
                with t2:
                    if not equip.empty:
                        for _, eq in equip.iterrows():
                            st.markdown(f'<div class="crm-equipment-card"><div class="crm-equipment-icon">&#128295;</div><div><div style="font-size:13px;font-weight:700;color:#f1f5f9;">{eq.get("name","—")}</div><div style="font-size:11px;color:#475569;">{eq.get("type","—")} · {eq.get("model","—")} · Installed: {eq.get("install_date","—")}</div></div></div>', unsafe_allow_html=True)
                    else: st.info("No equipment on file.")
                    with st.form(f"eq_add_{cphone}"):
                        e1, e2 = st.columns(2)
                        with e1: en = st.text_input("Equipment Name", key=f"eqn_{cphone}")
                        with e2: et = st.selectbox("Type", ["HVAC Unit","Water Heater","Electrical Panel","Plumbing Fixture","Appliance","Generator","Other"], key=f"eqt_{cphone}")
                        e3, e4 = st.columns(2)
                        with e3: em = st.text_input("Model", key=f"eqm_{cphone}")
                        with e4: ei = st.date_input("Install Date", key=f"eqi_{cphone}")
                        ew = st.selectbox("Warranty", ["Under Warranty","Extended","Expired","Unknown"], key=f"eqw_{cphone}")
                        if st.form_submit_button("&#10133; Add Equipment"):
                            if en.strip(): insert_equipment({"customer_phone": cphone, "name": en.strip(), "type": et, "model": em.strip(), "install_date": str(ei), "warranty_status": ew}); st.success("Added!"); st.rerun()
                with t3:
                    if st.button("&#128222; Call Customer", use_container_width=True):
                        st.info(f"Dialing {cphone}... (Integrate with call provider)")
                    if st.button("&#128172; Send SMS", use_container_width=True):
                        st.info("SMS panel ready. Connect Twilio to send.")
                    if st.button("&#128221; Book Follow-up Job", use_container_width=True):
                        st.session_state.page = "home"
                        st.session_state._book_for_customer = cname
                        st.session_state._book_for_phone = cphone
                        st.rerun()
                st.markdown("<hr style='border-color:#0f0f1a;margin:24px 0;'>", unsafe_allow_html=True)
        else: st.info("No customers found.")
    else: st.markdown("<div style='text-align:center;padding:60px;'><div style='font-size:42px;margin-bottom:16px;'>&#128100;</div><div style='font-size:14px;font-weight:600;color:#334155;'>Search for a customer to view their full profile</div></div>", unsafe_allow_html=True)

# =======================================================================
# ROUTER
# =======================================================================
page = st.session_state.get("page", "home")

if page == "total_calls":
    page_total_calls()
elif page == "active_jobs":
    page_active_jobs()
elif page == "pending":
    page_pending()
elif page == "technicians":
    page_technicians()
elif page == "revenue":
    page_revenue()
elif page == "tech_portal_admin":
    page_tech_portal_admin()
elif page == "tech_portal":
    page_tech_portal()
elif page == "customer_crm":
    page_customer_crm()
else:
    page_home()