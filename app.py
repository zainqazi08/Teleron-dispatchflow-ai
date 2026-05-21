import streamlit as st
import pandas as pd
from datetime import datetime
import os
import base64
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
    .stApp { background-color: #090d16; color: #ffffff; font-family: 'Inter', sans-serif; }
    header, footer { visibility: hidden !important; }
    .block-container { padding-top: 1rem !important; max-width: 96% !important; }
    .metric-box {
        background: linear-gradient(135deg, #111827, #0f172a);
        border: 1px solid #1e293b;
        padding: 20px; border-radius: 12px; margin-bottom: 20px;
    }
    .metric-number { font-size: 32px; font-weight: 700; color: #ffffff; }
    .metric-label { font-size: 12px; font-weight: 700; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }
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
    </style>
""", unsafe_allow_html=True)

# --- 5. BRANDING ---
logo_base64 = base64.b64encode(open("logo.png", "rb").read()).decode() if os.path.exists("logo.png") else None
if logo_base64:
    st.markdown(
        f"""<div style="display:flex; align-items:center; gap:14px; margin-bottom:20px;">
        <img src="data:image/png;base64,{logo_base64}" style="height:45px; width:auto;">
        <h2 style="color:#ffffff; margin:0;">TELERON Central Dispatch</h2></div>""",
        unsafe_allow_html=True
    )
else:
    st.markdown("<h2 style='color:#ffffff; margin-bottom:20px;'>⚡ TELERON Central Dispatch</h2>", unsafe_allow_html=True)

# --- 6. METRICS ROW ---
all_jobs      = get_jobs()
active_count  = len(get_jobs(status_filter="Dispatched"))
pending_count = len(get_jobs(status_filter="Pending Assignment"))
tech_count    = len(get_technicians())
total         = len(all_jobs)

m1, m2, m3, m4 = st.columns(4)
with m1: st.markdown(f"<div class='metric-box'><div class='metric-number'>{total}</div><div class='metric-label'>Total Calls</div></div>", unsafe_allow_html=True)
with m2: st.markdown(f"<div class='metric-box'><div class='metric-number'>{active_count}</div><div class='metric-label'>Active Jobs</div></div>", unsafe_allow_html=True)
with m3: st.markdown(f"<div class='metric-box'><div class='metric-number'>{pending_count}</div><div class='metric-label'>Pending</div></div>", unsafe_allow_html=True)
with m4: st.markdown(f"<div class='metric-box'><div class='metric-number'>{tech_count}</div><div class='metric-label'>Technicians</div></div>", unsafe_allow_html=True)

# --- 7. TABS ---
tab1, tab2, tab3 = st.tabs(["🎛️ DISPATCH CONTROL BOARD", "🗂️ ALL JOB HISTORY RECORDS", "👷 TECHNICIAN ROSTER & STATS"])

# ── TAB 1 ─────────────────────────────────────────────────────────────────────
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
                insert_job({
                    "customer_name":  c_name,
                    "phone":          c_phone,
                    "transcript":     s_transcript,
                    "status":         "Pending Assignment",
                    "scheduled_date": str(s_date),
                    "assigned_tech":  s_tech,
                    "timestamp":      datetime.now().isoformat()
                })
                st.success("Job saved!")
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

# ── TAB 2 ─────────────────────────────────────────────────────────────────────
with tab2:
    jobs_df = get_jobs()
    if not jobs_df.empty:
        st.dataframe(jobs_df, use_container_width=True, hide_index=True)
    else:
        st.info("No job records yet.")

# ── TAB 3 — TECHNICIAN ROSTER ─────────────────────────────────────────────────
with tab3:
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