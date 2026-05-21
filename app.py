import streamlit as st
import pandas as pd
from datetime import datetime
import os
import base64
import json
import anthropic
from supabase import create_client, Client

# --- 1. PAGE CONFIG ---
st.set_page_config(page_title="Teleron Central Dispatch", page_icon="logo.png", layout="wide")

# --- 2. SUPABASE CONNECTION ---
SUPABASE_URL = "https://fjtngjxvarpboretvrzl.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZqdG5nanh2YXJwYm9yZXR2cnpsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkzMTExOTIsImV4cCI6MjA5NDg4NzE5Mn0.UuWxjqPX1YRmhPS6qzSUpX9iaJ0_URC8nk8Yvbps374"

@st.cache_resource
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

supabase = get_supabase()

# --- 2b. ANTHROPIC CLIENT ---
anthropic_client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])

# --- 2c. AI SUMMARY FUNCTION ---
def generate_call_summary(transcript: str, customer_name: str, phone: str) -> dict:
    if not transcript or not transcript.strip():
        return {
            "problem": "No transcript provided",
            "urgency": "Unknown",
            "service_type": "Unknown",
            "tech_skill": "General",
            "sentiment": "Neutral",
            "follow_up": ["Obtain call transcript"]
        }
    prompt = f"""You are an expert HVAC and home services BPO dispatcher AI.
Analyze this customer call transcript and return a JSON summary.

Customer: {customer_name}
Phone: {phone}
Transcript:
\"\"\"{transcript}\"\"\"

Return ONLY a valid JSON object with exactly these fields:
{{
  "problem": "One clear sentence describing the main issue",
  "urgency": "One of: Low / Medium / High / Emergency",
  "service_type": "One of: HVAC / Plumbing / Electrical / Appliance Repair / General Home Service / Unknown",
  "tech_skill": "What specific skill the technician needs",
  "sentiment": "One of: Calm / Frustrated / Urgent / Angry / Satisfied / Confused",
  "follow_up": ["action item 1", "action item 2", "action item 3"]
}}

Be concise. Base everything strictly on the transcript. Do not invent details."""
    try:
        message = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        return {
            "problem": f"Summary failed: {e}",
            "urgency": "Unknown",
            "service_type": "Unknown",
            "tech_skill": "Unknown",
            "sentiment": "Unknown",
            "follow_up": ["Retry summary generation"]
        }

def save_summary_to_db(job_id: int, summary: dict):
    try:
        supabase.table("jobs").update({"ai_summary": json.dumps(summary)}).eq("id", job_id).execute()
    except Exception:
        pass

def parse_summary(raw_summary) -> dict | None:
    """Safely parse ai_summary from DB — handles str, dict, float, None."""
    if raw_summary is None:
        return None
    if isinstance(raw_summary, dict):
        return raw_summary
    if isinstance(raw_summary, str) and raw_summary.strip():
        try:
            return json.loads(raw_summary)
        except Exception:
            return None
    return None

# --- 3. DB HELPER FUNCTIONS ---
def get_jobs(status_filter=None):
    try:
        query = supabase.table("jobs").select("*").order("id", desc=True)
        if status_filter:
            query = query.eq("status", status_filter)
        result = query.execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except Exception as e:
        st.error(f"Error fetching jobs: {e}")
        return pd.DataFrame()

def get_technicians(status_filter=None):
    try:
        query = supabase.table("technicians").select("*").order("name")
        if status_filter:
            query = query.eq("status", status_filter)
        result = query.execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except Exception as e:
        st.error(f"Error fetching technicians: {e}")
        return pd.DataFrame()

def insert_job(data: dict):
    return supabase.table("jobs").insert(data).execute()

def update_job_status(job_id: int, status: str):
    return supabase.table("jobs").update({"status": status}).eq("id", job_id).execute()

def delete_job(job_id: int):
    return supabase.table("jobs").delete().eq("id", job_id).execute()

def insert_technician(data: dict):
    return supabase.table("technicians").insert(data).execute()

def update_tech_status(tech_id: str, status: str):
    return supabase.table("technicians").update({"status": status}).eq("id", tech_id).execute()

def delete_technician(tech_id: str):
    return supabase.table("technicians").delete().eq("id", tech_id).execute()

# --- 4. CSS THEME ---
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    .stApp { background-color: #090d16; color: #ffffff; font-family: 'Inter', sans-serif; }
    header, footer { visibility: hidden !important; }
    .block-container { padding-top: 1rem !important; max-width: 96% !important; }

    .metrics-grid {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 16px;
        margin-bottom: 28px;
    }
    @media (max-width: 1100px) { .metrics-grid { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 600px)  { .metrics-grid { grid-template-columns: 1fr; } }

    .metric-card {
        background: #111827; border: 1px solid #1e293b;
        border-radius: 16px; padding: 20px 22px 18px;
        transition: border-color 0.2s ease;
    }
    .metric-card:hover { border-color: #334155; }
    .metric-card-top {
        display: flex; justify-content: space-between;
        align-items: flex-start; margin-bottom: 16px;
    }
    .metric-icon {
        width: 42px; height: 42px; border-radius: 10px;
        display: flex; align-items: center; justify-content: center;
        font-size: 18px; flex-shrink: 0;
    }
    .metric-badge {
        font-size: 11px; font-weight: 600;
        padding: 4px 10px; border-radius: 999px; letter-spacing: 0.3px;
    }
    .metric-number {
        font-size: 40px; font-weight: 700;
        line-height: 1; margin-bottom: 4px; letter-spacing: -1px;
    }
    .metric-title {
        font-size: 11px; font-weight: 700; color: #64748b;
        text-transform: uppercase; letter-spacing: 1px; margin-bottom: 18px;
    }
    .metric-divider { border: none; border-top: 1px solid #1e293b; margin: 0 0 14px; }
    .metric-row {
        display: flex; justify-content: space-between;
        align-items: center; font-size: 12px; color: #64748b; margin-bottom: 8px;
    }
    .metric-row-label { display: flex; align-items: center; gap: 6px; }
    .metric-row-val { font-weight: 600; font-size: 12px; color: #e2e8f0; }
    .metric-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
    .metric-bar-wrap { height: 4px; background: #1e293b; border-radius: 99px; margin-top: 14px; overflow: hidden; }
    .metric-bar-fill { height: 100%; border-radius: 99px; }

    .icon-blue   { background: #0c2a4a; color: #60a5fa; }
    .icon-green  { background: #052e16; color: #4ade80; }
    .icon-amber  { background: #2d1a00; color: #fbbf24; }
    .icon-purple { background: #1e1b4b; color: #a78bfa; }
    .badge-blue   { background: #0c2a4a; color: #60a5fa; }
    .badge-green  { background: #052e16; color: #4ade80; }
    .badge-amber  { background: #2d1a00; color: #fbbf24; }
    .badge-purple { background: #1e1b4b; color: #a78bfa; }

    .job-card {
        background: #111827; border: 1px solid #1e293b;
        border-radius: 12px; padding: 18px; margin-bottom: 15px;
    }
    .section-header {
        color: #ffffff; font-size: 15px; font-weight: 700;
        text-transform: uppercase; letter-spacing: 1px;
        border-bottom: 1px solid #1e293b; padding-bottom: 10px; margin-bottom: 16px;
    }
    .badge-active {
        background: #052e16; color: #4ade80;
        border: 1px solid #166534; border-radius: 20px;
        padding: 2px 10px; font-size: 11px; font-weight: 700;
    }
    .badge-inactive {
        background: #1c1917; color: #a8a29e;
        border: 1px solid #44403c; border-radius: 20px;
        padding: 2px 10px; font-size: 11px; font-weight: 700;
    }
    div[data-testid="stForm"] {
        background: #111827; border: 1px solid #1e293b;
        border-radius: 12px; padding: 20px;
    }

    /* CALL SUMMARY CARDS */
    .summary-card {
        background: #111827; border: 1px solid #1e293b;
        border-radius: 16px; padding: 22px 24px; margin-bottom: 18px;
        transition: border-color 0.2s ease;
    }
    .summary-card:hover { border-color: #334155; }
    .summary-card-header {
        display: flex; justify-content: space-between;
        align-items: flex-start; margin-bottom: 16px;
        padding-bottom: 14px; border-bottom: 1px solid #1e293b;
    }
    .summary-customer { font-size: 16px; font-weight: 700; color: #ffffff; }
    .summary-phone    { font-size: 12px; color: #64748b; margin-top: 3px; }
    .summary-job-id   { font-size: 11px; color: #475569; font-weight: 600; letter-spacing: 0.5px; }
    .summary-grid {
        display: grid; grid-template-columns: 1fr 1fr;
        gap: 12px; margin-bottom: 14px;
    }
    .summary-field {
        background: #0f172a; border: 1px solid #1e293b;
        border-radius: 10px; padding: 12px 14px;
    }
    .summary-field-label {
        font-size: 10px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 1px; color: #475569; margin-bottom: 5px;
    }
    .summary-field-value { font-size: 13px; font-weight: 600; color: #e2e8f0; line-height: 1.4; }
    .summary-problem {
        background: #0f172a; border: 1px solid #1e293b;
        border-left: 3px solid #60a5fa; border-radius: 10px;
        padding: 12px 14px; margin-bottom: 12px;
    }
    .summary-problem-label {
        font-size: 10px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 1px; color: #475569; margin-bottom: 5px;
    }
    .summary-problem-value { font-size: 14px; color: #e2e8f0; line-height: 1.5; }
    .summary-followup {
        background: #0f172a; border: 1px solid #1e293b;
        border-radius: 10px; padding: 12px 14px;
    }
    .summary-followup-label {
        font-size: 10px; font-weight: 700; text-transform: uppercase;
        letter-spacing: 1px; color: #475569; margin-bottom: 8px;
    }
    .followup-item {
        font-size: 12px; color: #94a3b8;
        padding: 3px 0; display: flex; align-items: flex-start; gap: 8px;
    }
    .followup-dot { color: #60a5fa; font-weight: 900; flex-shrink: 0; }

    .urgency-low       { background:#052e16; color:#4ade80; border:1px solid #166534; }
    .urgency-medium    { background:#2d1a00; color:#fbbf24; border:1px solid #92400e; }
    .urgency-high      { background:#1c0a00; color:#fb923c; border:1px solid #9a3412; }
    .urgency-emergency { background:#3b0a0a; color:#f87171; border:1px solid #991b1b; }
    .sentiment-calm       { background:#052e16; color:#4ade80; }
    .sentiment-frustrated { background:#2d1a00; color:#fbbf24; }
    .sentiment-urgent     { background:#1c0a00; color:#fb923c; }
    .sentiment-angry      { background:#3b0a0a; color:#f87171; }
    .sentiment-satisfied  { background:#0c2a4a; color:#60a5fa; }
    .sentiment-confused   { background:#1e1b4b; color:#a78bfa; }
    .urgency-badge, .sentiment-badge {
        display: inline-block; font-size: 11px; font-weight: 700;
        padding: 3px 10px; border-radius: 999px; letter-spacing: 0.3px;
    }
    </style>
""", unsafe_allow_html=True)

# --- 5. BRANDING ---
logo_base64 = base64.b64encode(open("logo.png", "rb").read()).decode() if os.path.exists("logo.png") else None
if logo_base64:
    st.markdown(
        f"""<div style="display:flex; align-items:center; gap:14px; margin-bottom:24px;">
        <img src="data:image/png;base64,{logo_base64}" style="height:45px; width:auto;">
        <h2 style="color:#ffffff; margin:0; font-size:22px; font-weight:700;">TELERON Central Dispatch</h2></div>""",
        unsafe_allow_html=True
    )
else:
    st.markdown("<h2 style='color:#ffffff; margin-bottom:24px;'>⚡ TELERON Central Dispatch</h2>", unsafe_allow_html=True)

# --- 6. METRIC DATA ---
all_jobs      = get_jobs()
total         = len(all_jobs)
dispatched    = len(get_jobs(status_filter="Dispatched"))
pending       = len(get_jobs(status_filter="Pending Assignment"))
other_jobs    = total - dispatched - pending
all_techs     = get_technicians()
tech_total    = len(all_techs)
tech_active   = len(get_technicians(status_filter="Active"))
tech_inactive = tech_total - tech_active

dispatch_rate   = f"{round((dispatched / total) * 100)}%" if total else "0%"
pending_pct     = f"{round((pending / total) * 100)}%" if total else "0%"
active_pct      = f"{round((dispatched / total) * 100)}% of total" if total else "0% of total"
coverage        = f"{round(dispatched / tech_active, 1)} jobs/tech" if tech_active and dispatched else "—"
tech_bar_pct    = round((tech_active / tech_total) * 100) if tech_total else 0
total_bar_pct   = round((dispatched / total) * 100) if total else 0
pending_bar_pct = round((pending / total) * 100) if total else 0

def queue_label(n):
    if n == 0: return "Clear"
    if n <= 3: return "Low"
    if n <= 8: return "Medium"
    return "High"

# --- 7. METRIC CARDS ---
st.markdown(f"""
<div class="metrics-grid">

  <div class="metric-card">
    <div class="metric-card-top">
      <div class="metric-icon icon-blue">📞</div>
      <span class="metric-badge badge-blue">All time</span>
    </div>
    <div class="metric-number">{total}</div>
    <div class="metric-title">Total Calls</div>
    <hr class="metric-divider">
    <div class="metric-row">
      <span class="metric-row-label"><span class="metric-dot" style="background:#60a5fa;"></span>Dispatched</span>
      <span class="metric-row-val">{dispatched}</span>
    </div>
    <div class="metric-row">
      <span class="metric-row-label"><span class="metric-dot" style="background:#fbbf24;"></span>Pending</span>
      <span class="metric-row-val">{pending}</span>
    </div>
    <div class="metric-row">
      <span class="metric-row-label"><span class="metric-dot" style="background:#475569;"></span>Other</span>
      <span class="metric-row-val">{other_jobs}</span>
    </div>
    <div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{total_bar_pct}%; background:#60a5fa;"></div></div>
  </div>

  <div class="metric-card">
    <div class="metric-card-top">
      <div class="metric-icon icon-green">🚀</div>
      <span class="metric-badge badge-green">{active_pct}</span>
    </div>
    <div class="metric-number">{dispatched}</div>
    <div class="metric-title">Active Jobs</div>
    <hr class="metric-divider">
    <div class="metric-row"><span class="metric-row-label">In progress</span><span class="metric-row-val">{dispatched}</span></div>
    <div class="metric-row"><span class="metric-row-label">Dispatch rate</span><span class="metric-row-val">{dispatch_rate}</span></div>
    <div class="metric-row"><span class="metric-row-label">Jobs per tech</span><span class="metric-row-val">{coverage}</span></div>
    <div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{total_bar_pct}%; background:#4ade80;"></div></div>
  </div>

  <div class="metric-card">
    <div class="metric-card-top">
      <div class="metric-icon icon-amber">⏳</div>
      <span class="metric-badge badge-amber">Awaiting dispatch</span>
    </div>
    <div class="metric-number">{pending}</div>
    <div class="metric-title">Pending Assignment</div>
    <hr class="metric-divider">
    <div class="metric-row"><span class="metric-row-label">Queue load</span><span class="metric-row-val">{queue_label(pending)}</span></div>
    <div class="metric-row"><span class="metric-row-label">Of total calls</span><span class="metric-row-val">{pending_pct}</span></div>
    <div class="metric-row"><span class="metric-row-label">Techs available</span><span class="metric-row-val">{tech_active} active</span></div>
    <div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{pending_bar_pct}%; background:#fbbf24;"></div></div>
  </div>

  <div class="metric-card">
    <div class="metric-card-top">
      <div class="metric-icon icon-purple">👷</div>
      <span class="metric-badge badge-purple">{tech_active} active</span>
    </div>
    <div class="metric-number">{tech_total}</div>
    <div class="metric-title">Technicians</div>
    <hr class="metric-divider">
    <div class="metric-row">
      <span class="metric-row-label"><span class="metric-dot" style="background:#4ade80;"></span>Active</span>
      <span class="metric-row-val">{tech_active}</span>
    </div>
    <div class="metric-row">
      <span class="metric-row-label"><span class="metric-dot" style="background:#475569;"></span>Inactive / leave</span>
      <span class="metric-row-val">{tech_inactive}</span>
    </div>
    <div class="metric-row"><span class="metric-row-label">Coverage ratio</span><span class="metric-row-val">{coverage}</span></div>
    <div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{tech_bar_pct}%; background:#a78bfa;"></div></div>
  </div>

</div>
""", unsafe_allow_html=True)

# --- 8. TABS ---
tab1, tab2, tab3, tab4 = st.tabs([
    "🎛️ DISPATCH CONTROL BOARD",
    "📋 CALL SUMMARIES",
    "🗂️ ALL JOB HISTORY RECORDS",
    "👷 TECHNICIAN ROSTER & STATS"
])

# ── TAB 1 — DISPATCH BOARD ────────────────────────────────────────────────────
with tab1:
    grid_left, grid_right = st.columns([1, 1])

    with grid_left:
        st.markdown("<h4 style='color:#ffffff;'>📞 New Customer Call Intake</h4>", unsafe_allow_html=True)
        c_name       = st.text_input("Customer Full Name")
        c_phone      = st.text_input("Customer Phone Number")
        s_transcript = st.text_area("Call Conversation", height=100)
        c1, c2 = st.columns(2)
        with c1:
            s_date = st.date_input("Schedule Date")
        with c2:
            active_tech_df = get_technicians(status_filter="Active")
            tech_names = active_tech_df['name'].tolist() if not active_tech_df.empty else ["No Active Technicians"]
            s_tech = st.selectbox("Assign Tech", tech_names)

        if st.button("SAVE JOB", use_container_width=True):
            if c_name.strip() and c_phone.strip():
                with st.spinner("Saving job and generating AI summary..."):
                    result = insert_job({
                        "customer_name":  c_name,
                        "phone":          c_phone,
                        "transcript":     s_transcript,
                        "status":         "Pending Assignment",
                        "scheduled_date": str(s_date),
                        "assigned_tech":  s_tech,
                        "timestamp":      datetime.now().isoformat()
                    })
                    if s_transcript.strip() and result.data:
                        new_job_id = result.data[0]["id"]
                        summary = generate_call_summary(s_transcript, c_name, c_phone)
                        save_summary_to_db(new_job_id, summary)
                        st.success("✅ Job saved with AI summary!")
                    else:
                        st.success("✅ Job saved!")
                st.rerun()
            else:
                st.error("Please fill in customer name and phone.")

    with grid_right:
        st.markdown("<h4 style='color:#ffffff;'>🚨 Waiting Dispatch Queue</h4>", unsafe_allow_html=True)
        queue_df = get_jobs(status_filter="Pending Assignment")
        if not queue_df.empty:
            for _, row in queue_df.iterrows():
                st.markdown(
                    f"<div class='job-card'><b>Job #{row['id']} — {row['customer_name']}</b>"
                    f"<br><small style='color:#94a3b8;'>📱 {row['phone']}</small>"
                    f"<br><small>{row['transcript']}</small></div>",
                    unsafe_allow_html=True
                )
                c1, c2 = st.columns([0.7, 0.3])
                with c1:
                    if st.button("🚀 DISPATCH", key=f"d_{row['id']}", use_container_width=True):
                        update_job_status(row['id'], "Dispatched")
                        st.rerun()
                with c2:
                    if st.button("❌ CANCEL", key=f"c_{row['id']}", use_container_width=True):
                        delete_job(row['id'])
                        st.rerun()
        else:
            st.info("No jobs waiting for dispatch.")

# ── TAB 2 — CALL SUMMARIES ────────────────────────────────────────────────────
with tab2:
    st.markdown("<h4 style='color:#ffffff; margin-bottom:4px;'>📋 AI Call Summaries</h4>", unsafe_allow_html=True)
    st.markdown("<p style='color:#64748b; font-size:13px; margin-bottom:20px;'>AI-powered analysis of every customer call — problem, urgency, tech skill needed, sentiment and follow-up actions.</p>", unsafe_allow_html=True)

    fcol1, fcol2, fcol3 = st.columns([1, 1, 1])
    with fcol1:
        filter_urgency   = st.selectbox("Filter by Urgency",   ["All", "Emergency", "High", "Medium", "Low"], key="sum_urgency")
    with fcol2:
        filter_sentiment = st.selectbox("Filter by Sentiment", ["All", "Angry", "Frustrated", "Urgent", "Confused", "Calm", "Satisfied"], key="sum_sentiment")
    with fcol3:
        filter_service   = st.selectbox("Filter by Service",   ["All", "HVAC", "Plumbing", "Electrical", "Appliance Repair", "General Home Service"], key="sum_service")

    summaries_df = get_jobs()

    if summaries_df.empty:
        st.info("No jobs yet. Save a job with a call transcript to generate summaries.")
    else:
        shown = 0
        for _, row in summaries_df.iterrows():
            summary = parse_summary(row.get("ai_summary", None))

            # Apply filters
            if summary:
                if filter_urgency   != "All" and summary.get("urgency",   "").lower() != filter_urgency.lower():   continue
                if filter_sentiment != "All" and summary.get("sentiment", "").lower() != filter_sentiment.lower(): continue
                if filter_service   != "All" and summary.get("service_type", "").lower() != filter_service.lower(): continue

            shown += 1

            urgency_val   = summary.get("urgency",   "Unknown").lower() if summary else "unknown"
            sentiment_val = summary.get("sentiment", "Unknown").lower() if summary else "unknown"

            urgency_class = {
                "low": "urgency-low", "medium": "urgency-medium",
                "high": "urgency-high", "emergency": "urgency-emergency"
            }.get(urgency_val, "urgency-medium")

            sentiment_class = {
                "calm": "sentiment-calm", "frustrated": "sentiment-frustrated",
                "urgent": "sentiment-urgent", "angry": "sentiment-angry",
                "satisfied": "sentiment-satisfied", "confused": "sentiment-confused"
            }.get(sentiment_val, "sentiment-calm")

            if summary:
                followup_items = summary.get("follow_up", [])
                followup_html  = "".join([
                    f"<div class='followup-item'><span class='followup-dot'>›</span>{item}</div>"
                    for item in followup_items
                ])
                st.markdown(f"""
                <div class='summary-card'>
                    <div class='summary-card-header'>
                        <div>
                            <div class='summary-customer'>👤 {row['customer_name']}</div>
                            <div class='summary-phone'>📱 {row['phone']}</div>
                        </div>
                        <div style='text-align:right;'>
                            <div class='summary-job-id'>JOB #{row['id']}</div>
                            <div style='font-size:11px; color:#475569; margin-top:4px;'>{row.get('scheduled_date','—')}</div>
                            <div style='margin-top:6px;'>
                                <span class='urgency-badge {urgency_class}'>{summary.get('urgency','Unknown')}</span>
                            </div>
                        </div>
                    </div>
                    <div class='summary-problem'>
                        <div class='summary-problem-label'>🔧 Problem Detected</div>
                        <div class='summary-problem-value'>{summary.get('problem','—')}</div>
                    </div>
                    <div class='summary-grid'>
                        <div class='summary-field'>
                            <div class='summary-field-label'>🛠️ Service Type</div>
                            <div class='summary-field-value'>{summary.get('service_type','—')}</div>
                        </div>
                        <div class='summary-field'>
                            <div class='summary-field-label'>👷 Tech Skill Needed</div>
                            <div class='summary-field-value'>{summary.get('tech_skill','—')}</div>
                        </div>
                        <div class='summary-field'>
                            <div class='summary-field-label'>😊 Customer Sentiment</div>
                            <div class='summary-field-value'>
                                <span class='sentiment-badge {sentiment_class}'>{summary.get('sentiment','—')}</span>
                            </div>
                        </div>
                        <div class='summary-field'>
                            <div class='summary-field-label'>👤 Assigned Tech</div>
                            <div class='summary-field-value'>{row.get('assigned_tech','Unassigned')}</div>
                        </div>
                    </div>
                    <div class='summary-followup'>
                        <div class='summary-followup-label'>✅ Follow-up Actions</div>
                        {followup_html}
                    </div>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div class='summary-card'>
                    <div class='summary-card-header'>
                        <div>
                            <div class='summary-customer'>👤 {row['customer_name']}</div>
                            <div class='summary-phone'>📱 {row['phone']}</div>
                        </div>
                        <div class='summary-job-id'>JOB #{row['id']}</div>
                    </div>
                    <div style='color:#475569; font-size:13px; font-style:italic; padding:8px 0;'>
                        No AI summary yet — click below to generate one.
                    </div>
                </div>
                """, unsafe_allow_html=True)

            if st.button(f"🔄 Regenerate Summary — Job #{row['id']}", key=f"regen_{row['id']}"):
                transcript = row.get("transcript", "")
                if transcript and str(transcript).strip():
                    with st.spinner(f"Generating AI summary for Job #{row['id']}..."):
                        new_summary = generate_call_summary(str(transcript), row['customer_name'], row['phone'])
                        save_summary_to_db(row['id'], new_summary)
                    st.success(f"✅ Summary updated for Job #{row['id']}!")
                    st.rerun()
                else:
                    st.warning("No transcript found for this job.")

            st.markdown("<hr style='border:none; border-top:1px solid #0f172a; margin:4px 0 12px;'>", unsafe_allow_html=True)

        if shown == 0:
            st.info("No summaries match your current filters.")

# ── TAB 3 — ALL JOB HISTORY ───────────────────────────────────────────────────
with tab3:
    jobs_df = get_jobs()
    if not jobs_df.empty:
        st.dataframe(jobs_df, use_container_width=True, hide_index=True)
    else:
        st.info("No job records yet.")

# ── TAB 4 — TECHNICIAN ROSTER ─────────────────────────────────────────────────
with tab4:
    left_col, right_col = st.columns([1.1, 0.9])

    with left_col:
        st.markdown("<div class='section-header'>➕ Add New Technician</div>", unsafe_allow_html=True)
        with st.form("add_tech_form", clear_on_submit=True):
            t_id   = st.text_input("Technician ID (unique)", placeholder="e.g. T006")
            t_name = st.text_input("Full Name",              placeholder="e.g. Alex Torres")
            t_zone = st.text_input("Service Zone",           placeholder="e.g. North District")
            col_a, col_b = st.columns(2)
            with col_a:
                t_avg_ticket = st.text_input("Avg Ticket ($)", placeholder="e.g. $320")
            with col_b:
                t_conversion = st.text_input("Conversion Rate", placeholder="e.g. 78%")
            t_status = st.selectbox("Status", ["Active", "Inactive", "On Leave"])

            if st.form_submit_button("ADD TECHNICIAN", use_container_width=True):
                if not t_id.strip() or not t_name.strip():
                    st.error("Technician ID and Name are required.")
                else:
                    try:
                        insert_technician({
                            "id":         t_id.strip(),
                            "name":       t_name.strip(),
                            "zone":       t_zone.strip(),
                            "avg_ticket": t_avg_ticket.strip(),
                            "conversion": t_conversion.strip(),
                            "status":     t_status
                        })
                        st.success(f"✅ {t_name} added!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: ID '{t_id}' may already exist. ({e})")

        st.markdown("<div class='section-header' style='margin-top:24px;'>✏️ Update Technician Status</div>", unsafe_allow_html=True)
        roster_df = get_technicians()
        if not roster_df.empty:
            tech_map   = {f"{r['name']} ({r['id']})": r['id'] for _, r in roster_df.iterrows()}
            sel_label  = st.selectbox("Select Technician", list(tech_map.keys()), key="edit_sel")
            sel_id     = tech_map[sel_label]
            cur_status = roster_df[roster_df['id'] == sel_id]['status'].values[0]
            status_opts = ["Active", "Inactive", "On Leave"]
            new_status  = st.selectbox("New Status", status_opts,
                                       index=status_opts.index(cur_status) if cur_status in status_opts else 0,
                                       key="edit_status")
            if st.button("UPDATE STATUS", use_container_width=True):
                update_tech_status(sel_id, new_status)
                st.success(f"Updated to {new_status}.")
                st.rerun()
        else:
            st.info("No technicians yet.")

    with right_col:
        st.markdown("<div class='section-header'>👷 Current Roster</div>", unsafe_allow_html=True)
        roster_df = get_technicians()
        if not roster_df.empty:
            for _, row in roster_df.iterrows():
                badge = "badge-active" if row['status'] == "Active" else "badge-inactive"
                st.markdown(f"""
                    <div class='job-card'>
                        <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;'>
                            <b style='font-size:15px;'>👤 {row['name']}</b>
                            <span class='{badge}'>{row['status']}</span>
                        </div>
                        <div style='color:#94a3b8; font-size:12px; line-height:1.8;'>
                            🆔 <b style='color:#fff;'>{row['id']}</b> &nbsp;|&nbsp;
                            📍 <b style='color:#fff;'>{row.get('zone') or '—'}</b><br>
                            💰 <b style='color:#4ade80;'>{row.get('avg_ticket') or '—'}</b> &nbsp;|&nbsp;
                            🎯 <b style='color:#60a5fa;'>{row.get('conversion') or '—'}</b>
                        </div>
                    </div>
                """, unsafe_allow_html=True)
                if st.button(f"🗑️ Remove {row['name']}", key=f"del_{row['id']}", use_container_width=True):
                    delete_technician(row['id'])
                    st.warning(f"{row['name']} removed.")
                    st.rerun()

            st.markdown("<div class='section-header' style='margin-top:20px;'>📋 Full Roster Table</div>", unsafe_allow_html=True)
            st.dataframe(roster_df, use_container_width=True, hide_index=True)
        else:
            st.markdown("""
                <div style='text-align:center; padding:40px; color:#475569;'>
                    <div style='font-size:40px;'>👷</div>
                    <div style='font-size:14px; margin-top:10px;'>No technicians added yet.</div>
                </div>
            """, unsafe_allow_html=True)