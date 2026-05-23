import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import os
import base64
import json
import requests
from supabase import create_client, Client
import plotly.express as px
import plotly.graph_objects as go

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
def get_jobs(status_filter=None):
    try:
        query = supabase.table("jobs").select("*").order("id", desc=True)
        if status_filter: query = query.eq("status", status_filter)
        result = query.execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except Exception as e:
        st.error(f"Error fetching jobs: {e}"); return pd.DataFrame()

def get_technicians(status_filter=None):
    try:
        query = supabase.table("technicians").select("*").order("name")
        if status_filter: query = query.eq("status", status_filter)
        result = query.execute()
        return pd.DataFrame(result.data) if result.data else pd.DataFrame()
    except Exception as e:
        st.error(f"Error fetching technicians: {e}"); return pd.DataFrame()

def insert_job(data): return supabase.table("jobs").insert(data).execute()
def update_job_status(job_id, status): return supabase.table("jobs").update({"status":status}).eq("id",job_id).execute()
def delete_job(job_id): return supabase.table("jobs").delete().eq("id",job_id).execute()
def insert_technician(data): return supabase.table("technicians").insert(data).execute()
def update_tech_status(tech_id, status): return supabase.table("technicians").update({"status":status}).eq("id",tech_id).execute()
def delete_technician(tech_id): return supabase.table("technicians").delete().eq("id",tech_id).execute()

# --- 5. TRACKING PAGE FUNCTIONS ---
def show_tracking_page(metric_type):
    """Display detailed tracking page for selected metric"""
    
    # Back button
    if st.button("← Back to Dashboard", use_container_width=False):
        st.session_state.page = "dashboard"
        st.rerun()
    
    st.markdown(f"<div class='section-header' style='margin-top: 20px;'>📊 {metric_type} Analytics Dashboard</div>", unsafe_allow_html=True)
    
    # Get data
    all_jobs = get_jobs()
    all_techs = get_technicians()
    
    # Show sample data for debugging
    if all_jobs.empty:
        st.warning("No job data found in database. Please add some jobs first.")
        return
    
    if metric_type == "Total Calls":
        show_calls_analytics(all_jobs, all_techs)
    elif metric_type == "Active Jobs":
        show_active_jobs_analytics(all_jobs, all_techs)
    elif metric_type == "Pending Assignment":
        show_pending_analytics(all_jobs, all_techs)
    elif metric_type == "Technicians":
        show_tech_analytics(all_jobs, all_techs)

def show_calls_analytics(jobs_df, techs_df):
    """Total Calls Analytics"""
    
    # Ensure timestamp is datetime
    if 'timestamp' in jobs_df.columns:
        jobs_df['timestamp'] = pd.to_datetime(jobs_df['timestamp'], errors='coerce')
        # Filter out rows with null timestamps
        jobs_df = jobs_df.dropna(subset=['timestamp'])
    
    if jobs_df.empty:
        st.info("No calls data available. Please add some jobs first.")
        return
    
    # Date range filter
    col1, col2, col3 = st.columns([1,1,2])
    with col1:
        days_back = st.selectbox("Time Period", ["Last 7 Days", "Last 30 Days", "Last 90 Days", "All Time"], key="calls_period")
    with col2:
        if days_back != "All Time":
            days = {"Last 7 Days": 7, "Last 30 Days": 30, "Last 90 Days": 90}[days_back]
            cutoff_date = datetime.now() - timedelta(days=days)
            filtered_jobs = jobs_df[jobs_df['timestamp'] >= cutoff_date]
        else:
            filtered_jobs = jobs_df
    with col3:
        st.metric("Total Calls in Period", len(filtered_jobs))
    
    if filtered_jobs.empty:
        st.info(f"No call data available for the selected period ({days_back}).")
        return
    
    # Daily call volume
    filtered_jobs['date'] = filtered_jobs['timestamp'].dt.date
    daily_calls = filtered_jobs.groupby('date').size().reset_index(name='count')
    
    if not daily_calls.empty:
        fig1 = px.line(daily_calls, x='date', y='count', title='📞 Daily Call Volume',
                       labels={'date': 'Date', 'count': 'Number of Calls'})
        fig1.update_layout(template='plotly_dark', xaxis_title="Date", yaxis_title="Calls")
        st.plotly_chart(fig1, use_container_width=True)
    else:
        st.info("Not enough data for daily trend chart.")
    
    # Hourly distribution
    filtered_jobs['hour'] = filtered_jobs['timestamp'].dt.hour
    hourly_calls = filtered_jobs.groupby('hour').size().reset_index(name='count')
    
    if not hourly_calls.empty:
        fig2 = px.bar(hourly_calls, x='hour', y='count', title='⏰ Calls by Hour',
                      labels={'hour': 'Hour of Day', 'count': 'Number of Calls'})
        fig2.update_layout(template='plotly_dark', xaxis=dict(tickmode='linear', tick0=0, dtick=2))
        st.plotly_chart(fig2, use_container_width=True)
    
    col1, col2 = st.columns(2)
    with col1:
        # Day of week
        filtered_jobs['weekday'] = filtered_jobs['timestamp'].dt.day_name()
        weekday_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        weekday_calls = filtered_jobs.groupby('weekday').size().reindex(weekday_order).reset_index()
        weekday_calls.columns = ['day', 'count']
        weekday_calls = weekday_calls.fillna(0)
        
        if not weekday_calls.empty:
            fig3 = px.bar(weekday_calls, x='day', y='count', title='📅 Calls by Day of Week',
                         labels={'day': 'Day', 'count': 'Calls'})
            fig3.update_layout(template='plotly_dark')
            st.plotly_chart(fig3, use_container_width=True)
    
    with col2:
        # Status distribution
        status_counts = filtered_jobs['status'].value_counts()
        if not status_counts.empty:
            fig4 = px.pie(values=status_counts.values, names=status_counts.index, title='🎯 Call Status Distribution')
            fig4.update_layout(template='plotly_dark')
            st.plotly_chart(fig4, use_container_width=True)
    
    # Urgency trends
    urgency_data = []
    for _, row in filtered_jobs.iterrows():
        summary = parse_summary(row.get('ai_summary', None))
        if summary and 'urgency' in summary:
            urgency_data.append({'date': row['date'], 'urgency': summary['urgency']})
    
    if urgency_data:
        urgency_df = pd.DataFrame(urgency_data)
        urgency_trend = urgency_df.groupby(['date', 'urgency']).size().reset_index(name='count')
        fig5 = px.line(urgency_trend, x='date', y='count', color='urgency', 
                      title='⚠️ Urgency Levels Over Time')
        fig5.update_layout(template='plotly_dark')
        st.plotly_chart(fig5, use_container_width=True)
    
    # Summary metrics
    st.markdown("<div class='section-header' style='margin-top: 20px;'>📈 Key Metrics</div>", unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        avg_daily = len(filtered_jobs) / filtered_jobs['date'].nunique()
        st.metric("Avg Calls/Day", f"{avg_daily:.1f}")
    with m2:
        if not daily_calls.empty:
            peak_day = daily_calls.loc[daily_calls['count'].idxmax()]
            st.metric("Peak Day", f"{peak_day['date']}", f"{peak_day['count']} calls")
        else:
            st.metric("Peak Day", "N/A")
    with m3:
        if not hourly_calls.empty:
            busiest_hour = hourly_calls.loc[hourly_calls['count'].idxmax(), 'hour']
            st.metric("Busiest Hour", f"{int(busiest_hour)}:00")
        else:
            st.metric("Busiest Hour", "N/A")
    with m4:
        completion_rate = (len(filtered_jobs[filtered_jobs['status'] == 'Dispatched']) / len(filtered_jobs) * 100) if len(filtered_jobs) > 0 else 0
        st.metric("Completion Rate", f"{completion_rate:.1f}%")

def show_active_jobs_analytics(jobs_df, techs_df):
    """Active Jobs Analytics"""
    
    if 'timestamp' in jobs_df.columns:
        jobs_df['timestamp'] = pd.to_datetime(jobs_df['timestamp'], errors='coerce')
    
    active_jobs = jobs_df[jobs_df['status'] == 'Dispatched'] if not jobs_df.empty else pd.DataFrame()
    
    if active_jobs.empty:
        st.info("No active/dispatched jobs found. Dispatch some jobs first to see analytics.")
        return
    
    col1, col2, col3 = st.columns([1,1,2])
    with col1:
        days_back = st.selectbox("Time Period", ["Last 7 Days", "Last 30 Days", "Last 90 Days", "All Time"], key="active_period")
    with col2:
        if days_back != "All Time" and 'timestamp' in active_jobs.columns:
            days = {"Last 7 Days": 7, "Last 30 Days": 30, "Last 90 Days": 90}[days_back]
            cutoff_date = datetime.now() - timedelta(days=days)
            filtered_active = active_jobs[active_jobs['timestamp'] >= cutoff_date]
        else:
            filtered_active = active_jobs
    with col3:
        st.metric("Active Jobs in Period", len(filtered_active))
    
    if filtered_active.empty:
        st.info(f"No active jobs data for {days_back}.")
        return
    
    if 'timestamp' in filtered_active.columns:
        filtered_active['date'] = filtered_active['timestamp'].dt.date
        daily_active = filtered_active.groupby('date').size().reset_index(name='count')
        
        if not daily_active.empty:
            fig1 = px.line(daily_active, x='date', y='count', title='🚀 Active Jobs Trend',
                           labels={'date': 'Date', 'count': 'Active Jobs'})
            fig1.update_layout(template='plotly_dark')
            st.plotly_chart(fig1, use_container_width=True)
    
    col1, col2 = st.columns(2)
    with col1:
        if 'assigned_tech' in filtered_active.columns:
            tech_counts = filtered_active['assigned_tech'].value_counts().head(10)
            if not tech_counts.empty:
                fig2 = px.bar(x=tech_counts.values, y=tech_counts.index, orientation='h',
                             title='👷 Jobs by Technician', labels={'x': 'Number of Jobs', 'y': 'Technician'})
                fig2.update_layout(template='plotly_dark')
                st.plotly_chart(fig2, use_container_width=True)
    
    with col2:
        service_types = []
        for _, row in filtered_active.iterrows():
            summary = parse_summary(row.get('ai_summary', None))
            if summary and 'service_type' in summary:
                service_types.append(summary['service_type'])
        if service_types:
            service_df = pd.Series(service_types).value_counts()
            fig3 = px.pie(values=service_df.values, names=service_df.index, title='🔧 Service Type Distribution')
            fig3.update_layout(template='plotly_dark')
            st.plotly_chart(fig3, use_container_width=True)
    
    # Performance metrics
    st.markdown("<div class='section-header' style='margin-top: 20px;'>⏱️ Performance Metrics</div>", unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Total Active Jobs", len(filtered_active))
    with m2:
        unique_techs = filtered_active['assigned_tech'].nunique() if 'assigned_tech' in filtered_active.columns else 0
        st.metric("Technicians Engaged", unique_techs)
    with m3:
        jobs_per_tech = len(filtered_active) / unique_techs if unique_techs > 0 else 0
        st.metric("Avg Jobs/Tech", f"{jobs_per_tech:.1f}")
    with m4:
        if not techs_df.empty:
            total_techs = len(techs_df)
            active_techs = len(techs_df[techs_df['status'] == 'Active'])
            util_rate = (active_techs/total_techs*100) if total_techs > 0 else 0
            st.metric("Tech Utilization", f"{util_rate:.1f}%")

def show_pending_analytics(jobs_df, techs_df):
    """Pending Assignment Analytics"""
    
    if 'timestamp' in jobs_df.columns:
        jobs_df['timestamp'] = pd.to_datetime(jobs_df['timestamp'], errors='coerce')
    
    pending_jobs = jobs_df[jobs_df['status'] == 'Pending Assignment'] if not jobs_df.empty else pd.DataFrame()
    
    if pending_jobs.empty:
        st.info("No pending jobs found. The dispatch queue is clear!")
        return
    
    col1, col2, col3 = st.columns([1,1,2])
    with col1:
        days_back = st.selectbox("Time Period", ["Last 7 Days", "Last 30 Days", "Last 90 Days", "All Time"], key="pending_period")
    with col2:
        if days_back != "All Time" and 'timestamp' in pending_jobs.columns:
            days = {"Last 7 Days": 7, "Last 30 Days": 30, "Last 90 Days": 90}[days_back]
            cutoff_date = datetime.now() - timedelta(days=days)
            filtered_pending = pending_jobs[pending_jobs['timestamp'] >= cutoff_date]
        else:
            filtered_pending = pending_jobs
    with col3:
        st.metric("Pending Jobs in Period", len(filtered_pending))
    
    if filtered_pending.empty:
        st.info(f"No pending jobs for {days_back}.")
        return
    
    if 'timestamp' in filtered_pending.columns:
        filtered_pending['date'] = filtered_pending['timestamp'].dt.date
        daily_pending = filtered_pending.groupby('date').size().reset_index(name='count')
        
        if not daily_pending.empty:
            fig1 = px.line(daily_pending, x='date', y='count', title='⏳ Pending Jobs Trend',
                           labels={'date': 'Date', 'count': 'Pending Jobs'})
            fig1.update_layout(template='plotly_dark')
            st.plotly_chart(fig1, use_container_width=True)
    
    col1, col2 = st.columns(2)
    with col1:
        # Aging analysis
        filtered_pending['days_pending'] = (datetime.now() - filtered_pending['timestamp']).dt.days
        aging_bins = pd.cut(filtered_pending['days_pending'], bins=[0,1,3,7,30,float('inf')], 
                            labels=['<1 day', '1-3 days', '3-7 days', '7-30 days', '30+ days'])
        aging_counts = aging_bins.value_counts()
        
        if not aging_counts.empty:
            fig2 = px.bar(x=aging_counts.values, y=aging_counts.index, orientation='h',
                         title='📆 Job Aging Analysis', labels={'x': 'Number of Jobs', 'y': 'Pending Duration'})
            fig2.update_layout(template='plotly_dark')
            st.plotly_chart(fig2, use_container_width=True)
    
    with col2:
        urgency_levels = []
        for _, row in filtered_pending.iterrows():
            summary = parse_summary(row.get('ai_summary', None))
            if summary and 'urgency' in summary:
                urgency_levels.append(summary['urgency'])
        if urgency_levels:
            urgency_df = pd.Series(urgency_levels).value_counts()
            fig3 = px.bar(x=urgency_df.values, y=urgency_df.index, orientation='h',
                         title='⚠️ Urgency of Pending Jobs', labels={'x': 'Count', 'y': 'Urgency'})
            fig3.update_layout(template='plotly_dark')
            st.plotly_chart(fig3, use_container_width=True)
    
    # Queue analytics
    st.markdown("<div class='section-header' style='margin-top: 20px;'>📊 Queue Analytics</div>", unsafe_allow_html=True)
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        avg_pending_days = filtered_pending['days_pending'].mean()
        st.metric("Avg Wait Time", f"{avg_pending_days:.1f} days")
    with m2:
        oldest_job = filtered_pending['days_pending'].max()
        st.metric("Oldest Pending", f"{oldest_job:.0f} days")
    with m3:
        techs_available = len(techs_df[techs_df['status'] == 'Active']) if not techs_df.empty else 0
        st.metric("Available Techs", techs_available)
    with m4:
        pending_rate = (len(filtered_pending) / len(jobs_df) * 100) if len(jobs_df) > 0 else 0
        st.metric("Pending Rate", f"{pending_rate:.1f}%")

def show_tech_analytics(jobs_df, techs_df):
    """Technicians Analytics"""
    
    if techs_df.empty:
        st.info("No technicians found. Please add technicians first.")
        return
    
    col1, col2, col3 = st.columns([1,1,2])
    with col1:
        days_back = st.selectbox("Time Period", ["Last 7 Days", "Last 30 Days", "Last 90 Days", "All Time"], key="tech_period")
    with col2:
        if days_back != "All Time" and 'timestamp' in jobs_df.columns:
            days = {"Last 7 Days": 7, "Last 30 Days": 30, "Last 90 Days": 90}[days_back]
            cutoff_date = datetime.now() - timedelta(days=days)
            jobs_df['timestamp'] = pd.to_datetime(jobs_df['timestamp'], errors='coerce')
            filtered_jobs = jobs_df[jobs_df['timestamp'] >= cutoff_date]
        else:
            filtered_jobs = jobs_df
    with col3:
        st.metric("Active Technicians", len(techs_df[techs_df['status'] == 'Active']) if not techs_df.empty else 0)
    
    # Technician status distribution
    tech_status = techs_df['status'].value_counts()
    if not tech_status.empty:
        fig1 = px.pie(values=tech_status.values, names=tech_status.index, title='👥 Technician Status')
        fig1.update_layout(template='plotly_dark')
        st.plotly_chart(fig1, use_container_width=True)
    
    col1, col2 = st.columns(2)
    with col1:
        if not filtered_jobs.empty and 'assigned_tech' in filtered_jobs.columns:
            tech_performance = filtered_jobs['assigned_tech'].value_counts().head(10)
            if not tech_performance.empty:
                fig2 = px.bar(x=tech_performance.values, y=tech_performance.index, orientation='h',
                             title='📈 Jobs Completed by Technician', labels={'x': 'Jobs', 'y': 'Technician'})
                fig2.update_layout(template='plotly_dark')
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.info("No job assignments recorded yet.")
    
    with col2:
        if 'zone' in techs_df.columns:
            zone_counts = techs_df['zone'].value_counts()
            if not zone_counts.empty:
                fig3 = px.pie(values=zone_counts.values, names=zone_counts.index, title='📍 Technicians by Zone')
                fig3.update_layout(template='plotly_dark')
                st.plotly_chart(fig3, use_container_width=True)
    
    # Performance metrics
    st.markdown("<div class='section-header' style='margin-top: 20px;'>🏆 Performance Metrics</div>", unsafe_allow_html=True)
    
    if not filtered_jobs.empty and 'assigned_tech' in filtered_jobs.columns:
        tech_performance = filtered_jobs['assigned_tech'].value_counts()
        top_tech = tech_performance.index[0] if len(tech_performance) > 0 else "N/A"
        top_jobs = tech_performance.values[0] if len(tech_performance) > 0 else 0
        
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Top Performer", top_tech, f"{top_jobs} jobs")
        with m2:
            total_techs = len(techs_df)
            active_techs = len(techs_df[techs_df['status'] == 'Active'])
            st.metric("Tech Utilization", f"{(active_techs/total_techs*100):.1f}%" if total_techs > 0 else "0%")
        with m3:
            if 'avg_ticket' in techs_df.columns:
                avg_ticket = pd.to_numeric(techs_df['avg_ticket'].str.replace('$', '').str.replace(',', ''), errors='coerce').mean()
                st.metric("Avg Ticket Value", f"${avg_ticket:.0f}" if not pd.isna(avg_ticket) else "N/A")
        with m4:
            if 'conversion' in techs_df.columns:
                avg_conv = techs_df['conversion'].str.replace('%', '').astype(float).mean()
                st.metric("Avg Conversion", f"{avg_conv:.1f}%" if not pd.isna(avg_conv) else "N/A")
    else:
        st.info("Not enough data for performance metrics. Add some dispatched jobs first.")

# --- 6. PREMIUM BLACK CSS ---
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Space+Grotesk:wght@400;500;600;700&display=swap');

/* ── GLOBAL RESET ── */
*, *::before, *::after { box-sizing: border-box; }
.stApp {
    background: #000000;
    color: #e2e8f0;
    font-family: 'Inter', sans-serif;
}
header, footer { visibility: hidden !important; }
.block-container { padding-top: 1.5rem !important; max-width: 97% !important; }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: #0a0a0a; }
::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 99px; }

/* ── ALL STREAMLIT INPUTS → DARK ── */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div > div,
.stDateInput > div > div > input,
.stNumberInput > div > div > input {
    background-color: #0a0a0a !important;
    color: #e2e8f0 !important;
    border: 1px solid #1a1a2e !important;
    border-radius: 10px !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 13px !important;
    padding: 10px 14px !important;
    transition: border-color 0.2s ease !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {
    border-color: #6366f1 !important;
    box-shadow: 0 0 0 3px rgba(99,102,241,0.1) !important;
    outline: none !important;
}
.stTextInput label, .stTextArea label, .stSelectbox label,
.stDateInput label, .stNumberInput label {
    color: #64748b !important;
    font-size: 11px !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 1px !important;
}
/* Selectbox dropdown */
.stSelectbox > div > div > div { background-color: #0a0a0a !important; color: #e2e8f0 !important; }
[data-baseweb="select"] > div { background-color: #0a0a0a !important; border-color: #1a1a2e !important; }
[data-baseweb="menu"] { background-color: #0d0d0d !important; border: 1px solid #1a1a2e !important; }
[data-baseweb="option"] { background-color: #0d0d0d !important; color: #e2e8f0 !important; }
[data-baseweb="option"]:hover { background-color: #1a1a2e !important; }

/* ── BUTTONS ── */
.stButton > button {
    background: linear-gradient(135deg, #6366f1, #4f46e5) !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 10px !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 12px !important;
    font-weight: 700 !important;
    letter-spacing: 0.5px !important;
    padding: 10px 20px !important;
    transition: all 0.2s ease !important;
    text-transform: uppercase !important;
}
.stButton > button:hover {
    background: linear-gradient(135deg, #7c3aed, #6366f1) !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 8px 25px rgba(99,102,241,0.35) !important;
}

/* ── FORM ── */
div[data-testid="stForm"] {
    background: #050508 !important;
    border: 1px solid #0f0f1a !important;
    border-radius: 16px !important;
    padding: 24px !important;
}

/* ── TABS ── */
.stTabs [data-baseweb="tab-list"] {
    background: #050508 !important;
    border-radius: 14px !important;
    padding: 6px !important;
    gap: 4px !important;
    border: 1px solid #0f0f1a !important;
    margin-bottom: 24px !important;
}
.stTabs [data-baseweb="tab"] {
    background: transparent !important;
    color: #475569 !important;
    border-radius: 10px !important;
    font-size: 12px !important;
    font-weight: 600 !important;
    letter-spacing: 0.3px !important;
    padding: 8px 16px !important;
    border: none !important;
    transition: all 0.2s ease !important;
}
.stTabs [aria-selected="true"] {
    background: linear-gradient(135deg, #6366f1, #4f46e5) !important;
    color: #ffffff !important;
    box-shadow: 0 4px 15px rgba(99,102,241,0.3) !important;
}
.stTabs [data-baseweb="tab-highlight"] { display: none !important; }
.stTabs [data-baseweb="tab-border"] { display: none !important; }

/* ── METRICS GRID ── */
.metrics-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 32px;
}
.metric-card {
    background: #050508;
    border: 1px solid #0f0f1a;
    border-radius: 20px;
    padding: 22px 24px 20px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.3s ease, transform 0.2s ease;
}
.metric-card::after {
    content: '';
    position: absolute;
    top: 0; right: 0;
    width: 80px; height: 80px;
    border-radius: 50%;
    opacity: 0.06;
    transform: translate(20px, -20px);
}
.metric-card.blue::after   { background: #6366f1; }
.metric-card.green::after  { background: #10b981; }
.metric-card.amber::after  { background: #f59e0b; }
.metric-card.purple::after { background: #8b5cf6; }
.metric-card:hover { border-color: #1a1a2e; transform: translateY(-2px); }
.metric-card-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.metric-icon {
    width: 44px; height: 44px; border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px;
}
.icon-blue   { background: rgba(99,102,241,0.12);  color: #818cf8; }
.icon-green  { background: rgba(16,185,129,0.12);  color: #34d399; }
.icon-amber  { background: rgba(245,158,11,0.12);  color: #fbbf24; }
.icon-purple { background: rgba(139,92,246,0.12);  color: #a78bfa; }
.metric-badge {
    font-size: 10px; font-weight: 700;
    padding: 4px 10px; border-radius: 999px;
    letter-spacing: 0.5px; text-transform: uppercase;
}
.badge-blue   { background: rgba(99,102,241,0.12);  color: #818cf8; }
.badge-green  { background: rgba(16,185,129,0.12);  color: #34d399; }
.badge-amber  { background: rgba(245,158,11,0.12);  color: #fbbf24; }
.badge-purple { background: rgba(139,92,246,0.12);  color: #a78bfa; }
.metric-number {
    font-family: 'Space Grotesk', sans-serif;
    font-size: 42px; font-weight: 700;
    line-height: 1; margin-bottom: 4px;
    letter-spacing: -2px; color: #f8fafc;
}
.metric-title {
    font-size: 10px; font-weight: 700; color: #334155;
    text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 20px;
}
.metric-divider { border: none; border-top: 1px solid #0f0f1a; margin: 0 0 14px; }
.metric-row { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #334155; margin-bottom: 8px; }
.metric-row-label { display: flex; align-items: center; gap: 7px; }
.metric-row-val { font-weight: 700; font-size: 12px; color: #94a3b8; }
.metric-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.metric-bar-wrap { height: 3px; background: #0f0f1a; border-radius: 99px; margin-top: 16px; overflow: hidden; }
.metric-bar-fill { height: 100%; border-radius: 99px; }

/* ── GLASS CARDS ── */
.glass-card {
    background: #050508;
    border: 1px solid #0f0f1a;
    border-radius: 20px;
    padding: 24px;
    margin-bottom: 16px;
    transition: border-color 0.2s ease;
}
.glass-card:hover { border-color: #1a1a2e; }

/* ── JOB CARDS ── */
.job-card {
    background: #050508;
    border: 1px solid #0f0f1a;
    border-radius: 16px;
    padding: 18px 20px;
    margin-bottom: 12px;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
    position: relative;
}
.job-card:hover { border-color: #1e1b4b; box-shadow: 0 4px 20px rgba(99,102,241,0.08); }
.job-card-id {
    font-size: 10px; font-weight: 700; color: #334155;
    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px;
}
.job-card-name { font-size: 15px; font-weight: 700; color: #f1f5f9; margin-bottom: 4px; }
.job-card-meta { font-size: 12px; color: #334155; display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; }
.job-card-meta span { display: flex; align-items: center; gap: 5px; }

/* ── SECTION HEADER ── */
.section-header {
    font-size: 11px; font-weight: 700; color: #334155;
    text-transform: uppercase; letter-spacing: 2px;
    border-bottom: 1px solid #0f0f1a;
    padding-bottom: 12px; margin-bottom: 20px;
    display: flex; align-items: center; gap: 8px;
}
.section-header::before {
    content: '';
    display: inline-block;
    width: 3px; height: 14px;
    background: linear-gradient(180deg, #6366f1, #8b5cf6);
    border-radius: 99px;
}

/* ── ADDRESS BOX ── */
.address-box {
    background: #03030a;
    border: 1px solid #0f0f1a;
    border-left: 3px solid #6366f1;
    border-radius: 14px;
    padding: 18px 20px;
    margin: 14px 0 18px 0;
}
.address-box-title {
    font-size: 10px; font-weight: 800; color: #6366f1;
    text-transform: uppercase; letter-spacing: 2px;
    margin-bottom: 14px; display: flex; align-items: center; gap: 7px;
}

/* ── BADGES ── */
.badge-active   { background:rgba(16,185,129,0.1);  color:#34d399; border:1px solid rgba(16,185,129,0.2);  border-radius:99px; padding:3px 10px; font-size:10px; font-weight:700; letter-spacing:0.5px; }
.badge-inactive { background:rgba(71,85,105,0.1);   color:#64748b; border:1px solid rgba(71,85,105,0.2);  border-radius:99px; padding:3px 10px; font-size:10px; font-weight:700; letter-spacing:0.5px; }
.badge-pending  { background:rgba(245,158,11,0.1);  color:#fbbf24; border:1px solid rgba(245,158,11,0.2); border-radius:99px; padding:3px 10px; font-size:10px; font-weight:700; letter-spacing:0.5px; }
.badge-dispatch { background:rgba(99,102,241,0.1);  color:#818cf8; border:1px solid rgba(99,102,241,0.2); border-radius:99px; padding:3px 10px; font-size:10px; font-weight:700; letter-spacing:0.5px; }

/* ── URGENCY & SENTIMENT BADGES ── */
.urgency-badge, .sentiment-badge { display:inline-block; font-size:10px; font-weight:700; padding:3px 10px; border-radius:999px; letter-spacing:0.5px; }
.urgency-low       { background:rgba(16,185,129,0.1);  color:#34d399; border:1px solid rgba(16,185,129,0.2);  }
.urgency-medium    { background:rgba(245,158,11,0.1);  color:#fbbf24; border:1px solid rgba(245,158,11,0.2);  }
.urgency-high      { background:rgba(249,115,22,0.1);  color:#fb923c; border:1px solid rgba(249,115,22,0.2);  }
.urgency-emergency { background:rgba(239,68,68,0.1);   color:#f87171; border:1px solid rgba(239,68,68,0.2);   }
.sentiment-calm       { background:rgba(16,185,129,0.1); color:#34d399; }
.sentiment-frustrated { background:rgba(245,158,11,0.1); color:#fbbf24; }
.sentiment-urgent     { background:rgba(249,115,22,0.1); color:#fb923c; }
.sentiment-angry      { background:rgba(239,68,68,0.1);  color:#f87171; }
.sentiment-satisfied  { background:rgba(99,102,241,0.1); color:#818cf8; }
.sentiment-confused   { background:rgba(139,92,246,0.1); color:#a78bfa; }

/* ── SUMMARY CARDS ── */
.summary-card { background:#050508; border:1px solid #0f0f1a; border-radius:20px; padding:24px 26px; margin-bottom:16px; transition: border-color 0.2s; }
.summary-card:hover { border-color: #1a1a2e; }
.summary-card-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:18px; padding-bottom:16px; border-bottom:1px solid #0f0f1a; }
.summary-customer { font-size:16px; font-weight:700; color:#f1f5f9; }
.summary-phone { font-size:12px; color:#334155; margin-top:4px; }
.summary-job-id { font-size:10px; color:#334155; font-weight:700; text-transform:uppercase; letter-spacing:1px; }
.summary-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:12px; }
.summary-field { background:#03030a; border:1px solid #0f0f1a; border-radius:12px; padding:12px 14px; }
.summary-field-label { font-size:9px; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; color:#334155; margin-bottom:6px; }
.summary-field-value { font-size:13px; font-weight:600; color:#cbd5e1; }
.summary-problem { background:#03030a; border:1px solid #0f0f1a; border-left:3px solid #6366f1; border-radius:12px; padding:14px 16px; margin-bottom:12px; }
.summary-problem-label { font-size:9px; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; color:#334155; margin-bottom:6px; }
.summary-problem-value { font-size:14px; color:#e2e8f0; line-height:1.6; }
.summary-followup { background:#03030a; border:1px solid #0f0f1a; border-radius:12px; padding:14px 16px; }
.summary-followup-label { font-size:9px; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; color:#334155; margin-bottom:10px; }
.followup-item { font-size:12px; color:#475569; padding:4px 0; display:flex; align-items:flex-start; gap:10px; }
.followup-dot { color:#6366f1; font-weight:900; flex-shrink:0; margin-top:1px; }

/* ── WHATSAPP ── */
.wa-outer { background:#03030a; border:1px solid #0f0f1a; border-radius:20px; overflow:hidden; max-width:460px; margin:0 auto; }
.wa-header { background:linear-gradient(135deg,#075E54,#128C7E); padding:16px 20px; display:flex; align-items:center; gap:14px; }
.wa-avatar { width:42px; height:42px; border-radius:50%; background:linear-gradient(135deg,#25D366,#128C7E); display:flex; align-items:center; justify-content:center; font-size:20px; }
.wa-name { font-size:15px; font-weight:700; color:#fff; font-family:'Inter',sans-serif; }
.wa-status { font-size:11px; color:rgba(255,255,255,0.7); margin-top:2px; }
.wa-messages { padding:16px; min-height:340px; max-height:400px; overflow-y:auto; background:#020205; }
.wa-bubble-bot { background:#0f0f1a; border-radius:0 14px 14px 14px; padding:11px 15px; margin-bottom:10px; max-width:84%; font-size:13px; line-height:1.55; color:#cbd5e1; border:1px solid #1a1a2e; }
.wa-bubble-user { background:linear-gradient(135deg,#1e1b4b,#312e81); border-radius:14px 0 14px 14px; padding:11px 15px; margin-bottom:10px; max-width:84%; margin-left:auto; font-size:13px; line-height:1.55; color:#e0e7ff; }
.wa-time { font-size:10px; color:#334155; margin-top:4px; text-align:right; }

/* ── TECH CARD ── */
.tech-card { background:#050508; border:1px solid #0f0f1a; border-radius:16px; padding:18px 20px; margin-bottom:12px; transition:border-color 0.2s,box-shadow 0.2s; }
.tech-card:hover { border-color:#1e1b4b; box-shadow:0 4px 20px rgba(99,102,241,0.06); }

/* ── ALERT / WARNING ── */
.stAlert { background:#03030a !important; border:1px solid #1a1a2e !important; border-radius:12px !important; color:#94a3b8 !important; }

/* ── DATAFRAME ── */
.stDataFrame { background:#050508 !important; border:1px solid #0f0f1a !important; border-radius:14px !important; }
.stDataFrame th { background:#03030a !important; color:#475569 !important; font-size:10px !important; text-transform:uppercase !important; letter-spacing:1px !important; }
.stDataFrame td { color:#94a3b8 !important; font-size:12px !important; }

/* ── METRIC (native) ── */
[data-testid="metric-container"] { background:#050508 !important; border:1px solid #0f0f1a !important; border-radius:14px !important; padding:16px !important; }
[data-testid="metric-container"] label { color:#334155 !important; font-size:10px !important; text-transform:uppercase !important; letter-spacing:1px !important; }
[data-testid="metric-container"] [data-testid="stMetricValue"] { color:#f1f5f9 !important; font-family:'Space Grotesk',sans-serif !important; }

/* ── DIVIDER ── */
hr { border-color: #0f0f1a !important; }

/* ── INFO BOX ── */
.stInfo { background:#03030a !important; border:1px solid #1a1a2e !important; color:#475569 !important; border-radius:12px !important; }

/* ── SPINNER ── */
.stSpinner > div { border-color: #6366f1 !important; }

/* Plotly charts dark theme */
.js-plotly-plot .plotly .main-svg { background: #050508 !important; }
</style>
""", unsafe_allow_html=True)

# --- 7. SESSION STATE FOR PAGE NAVIGATION ---
if 'page' not in st.session_state:
    st.session_state.page = "dashboard"

# --- 8. MAIN APP LOGIC ---
if st.session_state.page == "dashboard":
    # --- HEADER ---
    logo_base64 = base64.b64encode(open("logo.png","rb").read()).decode() if os.path.exists("logo.png") else None

    st.markdown(f"""
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:32px; padding-bottom:20px; border-bottom:1px solid #0f0f1a;">
        <div style="display:flex; align-items:center; gap:16px;">
            {"<img src='data:image/png;base64,"+logo_base64+"' style='height:42px;width:auto;border-radius:10px;'>" if logo_base64 else "<div style='width:42px;height:42px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:20px;'>⚡</div>"}
            <div>
                <div style="font-family:'Space Grotesk',sans-serif; font-size:20px; font-weight:700; color:#f8fafc; letter-spacing:-0.5px;">TELERON</div>
                <div style="font-size:10px; font-weight:600; color:#334155; text-transform:uppercase; letter-spacing:2px; margin-top:1px;">Central Dispatch</div>
            </div>
        </div>
        <div style="display:flex; align-items:center; gap:10px;">
            <div style="background:rgba(16,185,129,0.1); border:1px solid rgba(16,185,129,0.2); border-radius:99px; padding:6px 14px; font-size:11px; font-weight:700; color:#34d399;">
                🟢 LIVE
            </div>
            <div style="background:#050508; border:1px solid #0f0f1a; border-radius:99px; padding:6px 14px; font-size:11px; color:#475569;">
                {datetime.now().strftime("%b %d, %Y")}
            </div>
            {"<div style='background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2);border-radius:99px;padding:6px 14px;font-size:11px;font-weight:700;color:#f87171;'>⚠️ AI Offline</div>" if not AI_ENABLED else "<div style='background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.2);border-radius:99px;padding:6px 14px;font-size:11px;font-weight:700;color:#818cf8;'>🤖 AI Active</div>"}
        </div>
    </div>
    """, unsafe_allow_html=True)

    # --- METRICS (Clickable Cards) ---
    all_jobs      = get_jobs()
    total         = len(all_jobs)
    dispatched    = len(get_jobs(status_filter="Dispatched"))
    pending       = len(get_jobs(status_filter="Pending Assignment"))
    other_jobs    = total - dispatched - pending
    all_techs     = get_technicians()
    tech_total    = len(all_techs)
    tech_active   = len(get_technicians(status_filter="Active"))
    tech_inactive = tech_total - tech_active
    dispatch_rate   = f"{round((dispatched/total)*100)}%" if total else "0%"
    pending_pct     = f"{round((pending/total)*100)}%" if total else "0%"
    active_pct      = f"{round((dispatched/total)*100)}%" if total else "0%"
    coverage        = f"{round(dispatched/tech_active,1)}" if tech_active and dispatched else "—"
    tech_bar_pct    = round((tech_active/tech_total)*100)  if tech_total else 0
    total_bar_pct   = round((dispatched/total)*100)        if total else 0
    pending_bar_pct = round((pending/total)*100)           if total else 0
    def queue_label(n):
        if n==0: return "Clear"
        if n<=3: return "Low"
        if n<=8: return "Medium"
        return "High"

    # Create 4 columns for the metric cards
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        # Card content
        st.markdown(f"""
        <div class="metric-card blue">
            <div class="metric-card-top">
                <div class="metric-icon icon-blue">📞</div>
                <span class="metric-badge badge-blue">All Time</span>
            </div>
            <div class="metric-number">{total}</div>
            <div class="metric-title">Total Calls</div>
            <hr class="metric-divider">
            <div class="metric-row">
                <span class="metric-row-label"><span class="metric-dot" style="background:#818cf8;"></span>Dispatched</span>
                <span class="metric-row-val">{dispatched}</span>
            </div>
            <div class="metric-row">
                <span class="metric-row-label"><span class="metric-dot" style="background:#fbbf24;"></span>Pending</span>
                <span class="metric-row-val">{pending}</span>
            </div>
            <div class="metric-row">
                <span class="metric-row-label"><span class="metric-dot" style="background:#1e293b;"></span>Other</span>
                <span class="metric-row-val">{other_jobs}</span>
            </div>
            <div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{total_bar_pct}%;background:linear-gradient(90deg,#6366f1,#818cf8);"></div></div>
        </div>
        """, unsafe_allow_html=True)
        # Button below the card
        if st.button("📊 View Analytics", key="btn_total", use_container_width=True):
            st.session_state.page = "Total Calls"
            st.rerun()
    
    with col2:
        st.markdown(f"""
        <div class="metric-card green">
            <div class="metric-card-top">
                <div class="metric-icon icon-green">🚀</div>
                <span class="metric-badge badge-green">{active_pct}% of total</span>
            </div>
            <div class="metric-number">{dispatched}</div>
            <div class="metric-title">Active Jobs</div>
            <hr class="metric-divider">
            <div class="metric-row"><span class="metric-row-label">In Progress</span><span class="metric-row-val">{dispatched}</span></div>
            <div class="metric-row"><span class="metric-row-label">Dispatch Rate</span><span class="metric-row-val">{dispatch_rate}</span></div>
            <div class="metric-row"><span class="metric-row-label">Jobs / Tech</span><span class="metric-row-val">{coverage}</span></div>
            <div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{total_bar_pct}%;background:linear-gradient(90deg,#10b981,#34d399);"></div></div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("📊 View Analytics", key="btn_active", use_container_width=True):
            st.session_state.page = "Active Jobs"
            st.rerun()
    
    with col3:
        st.markdown(f"""
        <div class="metric-card amber">
            <div class="metric-card-top">
                <div class="metric-icon icon-amber">⏳</div>
                <span class="metric-badge badge-amber">Awaiting</span>
            </div>
            <div class="metric-number">{pending}</div>
            <div class="metric-title">Pending Assignment</div>
            <hr class="metric-divider">
            <div class="metric-row"><span class="metric-row-label">Queue Load</span><span class="metric-row-val">{queue_label(pending)}</span></div>
            <div class="metric-row"><span class="metric-row-label">% of Total</span><span class="metric-row-val">{pending_pct}</span></div>
            <div class="metric-row"><span class="metric-row-label">Techs Ready</span><span class="metric-row-val">{tech_active}</span></div>
            <div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{pending_bar_pct}%;background:linear-gradient(90deg,#f59e0b,#fbbf24);"></div></div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("📊 View Analytics", key="btn_pending", use_container_width=True):
            st.session_state.page = "Pending Assignment"
            st.rerun()
    
    with col4:
        st.markdown(f"""
        <div class="metric-card purple">
            <div class="metric-card-top">
                <div class="metric-icon icon-purple">👷</div>
                <span class="metric-badge badge-purple">{tech_active} Active</span>
            </div>
            <div class="metric-number">{tech_total}</div>
            <div class="metric-title">Technicians</div>
            <hr class="metric-divider">
            <div class="metric-row">
                <span class="metric-row-label"><span class="metric-dot" style="background:#34d399;"></span>Active</span>
                <span class="metric-row-val">{tech_active}</span>
            </div>
            <div class="metric-row">
                <span class="metric-row-label"><span class="metric-dot" style="background:#1e293b;"></span>Inactive</span>
                <span class="metric-row-val">{tech_inactive}</span>
            </div>
            <div class="metric-row"><span class="metric-row-label">Coverage</span><span class="metric-row-val">{coverage} j/t</span></div>
            <div class="metric-bar-wrap"><div class="metric-bar-fill" style="width:{tech_bar_pct}%;background:linear-gradient(90deg,#8b5cf6,#a78bfa);"></div></div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("📊 View Analytics", key="btn_tech", use_container_width=True):
            st.session_state.page = "Technicians"
            st.rerun()

    # --- TABS ---
    tab1,tab2,tab3,tab4,tab5,tab6 = st.tabs([
        "⚡ DISPATCH","🤖 AI BOT","📋 SUMMARIES","📞 VOICE AI","🗂️ HISTORY","👷 ROSTER"
    ])

    # ── TAB 1 ─────────────────────────────────────────────────────────────────────
    with tab1:
        gl, gr = st.columns([1,1], gap="large")
        with gl:
            st.markdown("<div class='section-header'>New Customer Call Intake</div>", unsafe_allow_html=True)
            c_name  = st.text_input("Customer Full Name", placeholder="John Smith")
            c_phone = st.text_input("Customer Phone Number", placeholder="+1 (555) 000-0000")
            st.markdown("<div class='address-box'><div class='address-box-title'>📍 Service Address</div>", unsafe_allow_html=True)
            addr_street = st.text_input("Street Address", placeholder="123 Main Street, Apt 4B")
            ac1,ac2 = st.columns(2)
            with ac1:
                addr_city = st.text_input("City", placeholder="Houston")
                addr_zip  = st.text_input("ZIP Code", placeholder="77001")
            with ac2:
                addr_state   = st.text_input("State", placeholder="Texas")
                addr_country = st.selectbox("Country", ["United States","Canada","United Kingdom","Australia","Other"])
            addr_notes = st.text_input("Access Notes", placeholder="Gate code 1234, Ring doorbell")
            st.markdown("</div>", unsafe_allow_html=True)
            s_transcript = st.text_area("Call Notes / Transcript", height=90, placeholder="Describe the customer's issue...")
            c1,c2 = st.columns(2)
            with c1: s_date = st.date_input("Schedule Date")
            with c2:
                atdf = get_technicians(status_filter="Active")
                tech_names = atdf['name'].tolist() if not atdf.empty else ["No Active Technicians"]
                s_tech = st.selectbox("Assign Technician", tech_names)
            if st.button("💾  SAVE JOB", use_container_width=True):
                if c_name.strip() and c_phone.strip():
                    full_address = f"{addr_street}, {addr_city}, {addr_state} {addr_zip}, {addr_country}"
                    if addr_notes.strip(): full_address += f" | Notes: {addr_notes}"
                    with st.spinner("Saving job..."):
                        result = insert_job({"customer_name":c_name,"phone":c_phone,"transcript":s_transcript,"status":"Pending Assignment","scheduled_date":str(s_date),"assigned_tech":s_tech,"timestamp":datetime.now().isoformat(),"keywords":full_address})
                        if s_transcript.strip() and result.data and AI_ENABLED:
                            new_id = result.data[0]["id"]
                            summary = generate_call_summary(s_transcript, c_name, c_phone)
                            save_summary_to_db(new_id, summary)
                            st.success("✅ Job saved with AI summary!")
                        else:
                            st.success("✅ Job saved successfully!")
                    st.rerun()
                else:
                    st.error("Customer name and phone are required.")

        with gr:
            st.markdown("<div class='section-header'>Waiting Dispatch Queue</div>", unsafe_allow_html=True)
            queue_df = get_jobs(status_filter="Pending Assignment")
            if not queue_df.empty:
                for _,row in queue_df.iterrows():
                    addr = str(row.get('keywords','') or '')
                    st.markdown(f"""
                    <div class='job-card'>
                        <div class='job-card-id'>JOB #{row['id']}</div>
                        <div class='job-card-name'>{row['customer_name']}</div>
                        <div class='job-card-meta'>
                            <span>📱 {row['phone']}</span>
                            {'<span>📍 '+addr[:45]+'</span>' if addr else ''}
                        </div>
                        {'<div style="font-size:12px;color:#334155;margin-top:8px;padding-top:8px;border-top:1px solid #0f0f1a;">'+str(row.get("transcript",""))[:80]+'</div>' if row.get("transcript") else ''}
                    </div>""", unsafe_allow_html=True)
                    c1,c2 = st.columns([0.7,0.3])
                    with c1:
                        if st.button("🚀  DISPATCH", key=f"d_{row['id']}", use_container_width=True):
                            update_job_status(row['id'], "Dispatched"); st.rerun()
                    with c2:
                        if st.button("✕  CANCEL", key=f"c_{row['id']}", use_container_width=True):
                            delete_job(row['id']); st.rerun()
            else:
                st.markdown("<div style='text-align:center;padding:50px 20px;color:#1e293b;'><div style='font-size:36px;margin-bottom:10px;'>✓</div><div style='font-size:13px;font-weight:600;'>Queue is clear</div></div>", unsafe_allow_html=True)

    # ── TAB 2 ─────────────────────────────────────────────────────────────────────
    with tab2:
        st.markdown("<div class='section-header'>WhatsApp AI Chatbot — Live Preview</div>", unsafe_allow_html=True)
        bc,ic = st.columns([1,1], gap="large")
        with bc:
            st.markdown("""<div class='wa-outer'>
                <div class='wa-header'>
                    <div class='wa-avatar'>⚡</div>
                    <div><div class='wa-name'>Teleron AI Assistant</div>
                    <div class='wa-status'>🟢 Online · Powered by Groq AI</div></div>
                </div></div>""", unsafe_allow_html=True)
            if "wa_chat" not in st.session_state:
                st.session_state.wa_chat = [{"role":"assistant","content":"👋 Hello! I'm the Teleron AI Assistant.\n\nI can help you with:\n• 🔧 Booking a service appointment\n• ❓ HVAC, plumbing & electrical questions\n• 🚨 Emergency dispatch\n• 💰 Pricing estimates\n\nHow can I help you today?","time":datetime.now().strftime("%H:%M")}]
            chat_html = "<div class='wa-messages'>"
            for msg in st.session_state.wa_chat:
                bubble = "wa-bubble-bot" if msg["role"]=="assistant" else "wa-bubble-user"
                chat_html += f"<div class='{bubble}'>{msg['content'].replace(chr(10),'<br>')}<div class='wa-time'>{msg.get('time','')}</div></div>"
            chat_html += "</div>"
            st.markdown(chat_html, unsafe_allow_html=True)
            user_input = st.text_input("", key="wa_input", placeholder="Type a message...", label_visibility="collapsed")
            sc,cc = st.columns([3,1])
            with sc:
                if st.button("📤  SEND", use_container_width=True):
                    if user_input.strip():
                        now = datetime.now().strftime("%H:%M")
                        st.session_state.wa_chat.append({"role":"user","content":user_input.strip(),"time":now})
                        history = [{"role":m["role"],"content":m["content"]} for m in st.session_state.wa_chat[:-1]]
                        with st.spinner(""):
                            bot_reply = whatsapp_bot_response(user_input.strip(), history)
                        st.session_state.wa_chat.append({"role":"assistant","content":bot_reply,"time":datetime.now().strftime("%H:%M")})
                        st.rerun()
            with cc:
                if st.button("🗑️", use_container_width=True):
                    st.session_state.wa_chat = [{"role":"assistant","content":"👋 Hello! How can I help you today?","time":datetime.now().strftime("%H:%M")}]
                    st.rerun()

        with ic:
            st.markdown("""
            <div class='glass-card'>
                <div style='font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:18px;'>📱 Connect to WhatsApp</div>
                <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'>
                    <div style='font-size:9px;font-weight:800;color:#6366f1;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>Option 1 — Twilio</div>
                    <div style='font-size:12px;color:#475569;line-height:1.8;'>1. Sign up at twilio.com<br>2. Get WhatsApp-enabled number<br>3. Set webhook to FastAPI backend<br>4. Bot handles messages automatically</div>
                </div>
                <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'>
                    <div style='font-size:9px;font-weight:800;color:#34d399;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>Option 2 — Meta Cloud API</div>
                    <div style='font-size:12px;color:#475569;line-height:1.8;'>1. Apply at business.whatsapp.com<br>2. Create Meta Business account<br>3. Connect your phone number<br>4. Set the AI webhook endpoint</div>
                </div>
                <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;'>
                    <div style='font-size:9px;font-weight:800;color:#a78bfa;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>Bot Capabilities</div>
                    <div style='font-size:12px;color:#475569;line-height:2;'>✦ 24/7 customer support<br>✦ Collect name & address<br>✦ Detect & escalate emergencies<br>✦ Book appointments<br>✦ Pricing estimates<br>✦ Hand off to human dispatcher</div>
                </div>
            </div>
            <div style='background:rgba(16,185,129,0.05);border:1px solid rgba(16,185,129,0.1);border-radius:14px;padding:16px;'>
                <div style='font-size:11px;font-weight:700;color:#34d399;margin-bottom:6px;'>✦ Powered by Groq — 100% Free</div>
                <div style='font-size:12px;color:#1e4a3a;line-height:1.7;'>Runs on Llama 3 70B. 14,400 free requests/day — no credit card needed.</div>
            </div>
            """, unsafe_allow_html=True)

    # ── TAB 3 ─────────────────────────────────────────────────────────────────────
    with tab3:
        st.markdown("<div class='section-header'>AI Call Summaries</div>", unsafe_allow_html=True)
        fc1,fc2,fc3 = st.columns(3)
        with fc1: fu = st.selectbox("Urgency",   ["All","Emergency","High","Medium","Low"], key="su")
        with fc2: fs = st.selectbox("Sentiment", ["All","Angry","Frustrated","Urgent","Confused","Calm","Satisfied"], key="ss")
        with fc3: fv = st.selectbox("Service",   ["All","HVAC","Plumbing","Electrical","Appliance Repair","General Home Service"], key="sv")
        sdf = get_jobs()
        if sdf.empty:
            st.info("No jobs yet. Save a job with a transcript to generate summaries.")
        else:
            shown = 0
            for _,row in sdf.iterrows():
                summary = parse_summary(row.get("ai_summary",None))
                if summary:
                    if fu!="All" and summary.get("urgency","").lower()!=fu.lower(): continue
                    if fs!="All" and summary.get("sentiment","").lower()!=fs.lower(): continue
                    if fv!="All" and summary.get("service_type","").lower()!=fv.lower(): continue
                shown+=1
                uv = summary.get("urgency","Unknown").lower() if summary else "unknown"
                sv2 = summary.get("sentiment","Unknown").lower() if summary else "unknown"
                uc = {"low":"urgency-low","medium":"urgency-medium","high":"urgency-high","emergency":"urgency-emergency"}.get(uv,"urgency-medium")
                sc2 = {"calm":"sentiment-calm","frustrated":"sentiment-frustrated","urgent":"sentiment-urgent","angry":"sentiment-angry","satisfied":"sentiment-satisfied","confused":"sentiment-confused"}.get(sv2,"sentiment-calm")
                addr = str(row.get('keywords','') or '—')
                if summary:
                    fhtml = "".join([f"<div class='followup-item'><span class='followup-dot'>›</span>{item}</div>" for item in summary.get("follow_up",[])])
                    st.markdown(f"""
                    <div class='summary-card'>
                        <div class='summary-card-header'>
                            <div>
                                <div class='summary-customer'>{row['customer_name']}</div>
                                <div class='summary-phone'>📱 {row['phone']}</div>
                                <div style='font-size:11px;color:#6366f1;margin-top:5px;'>📍 {addr[:65]}</div>
                            </div>
                            <div style='text-align:right;'>
                                <div class='summary-job-id'>JOB #{row['id']}</div>
                                <div style='font-size:11px;color:#1e293b;margin-top:4px;'>{row.get('scheduled_date','—')}</div>
                                <div style='margin-top:8px;'><span class='urgency-badge {uc}'>{summary.get('urgency','Unknown')}</span></div>
                            </div>
                        </div>
                        <div class='summary-problem'><div class='summary-problem-label'>Problem Detected</div><div class='summary-problem-value'>{summary.get('problem','—')}</div></div>
                        <div class='summary-grid'>
                            <div class='summary-field'><div class='summary-field-label'>Service Type</div><div class='summary-field-value'>{summary.get('service_type','—')}</div></div>
                            <div class='summary-field'><div class='summary-field-label'>Tech Skill Needed</div><div class='summary-field-value'>{summary.get('tech_skill','—')}</div></div>
                            <div class='summary-field'><div class='summary-field-label'>Sentiment</div><div class='summary-field-value'><span class='sentiment-badge {sc2}'>{summary.get('sentiment','—')}</span></div></div>
                            <div class='summary-field'><div class='summary-field-label'>Assigned Tech</div><div class='summary-field-value'>{row.get('assigned_tech','Unassigned')}</div></div>
                        </div>
                        <div class='summary-followup'><div class='summary-followup-label'>Follow-up Actions</div>{fhtml}</div>
                    </div>""", unsafe_allow_html=True)
                else:
                    st.markdown(f"""<div class='summary-card'><div class='summary-card-header'><div><div class='summary-customer'>{row['customer_name']}</div><div class='summary-phone'>📱 {row['phone']}</div></div><div class='summary-job-id'>JOB #{row['id']}</div></div><div style='color:#1e293b;font-size:13px;font-style:italic;'>No AI summary yet.</div></div>""", unsafe_allow_html=True)
                if st.button(f"↺  Regenerate Summary — Job #{row['id']}", key=f"regen_{row['id']}"):
                    t = row.get("transcript","")
                    if t and str(t).strip():
                        with st.spinner("Generating..."):
                            ns = generate_call_summary(str(t), row['customer_name'], row['phone'])
                            save_summary_to_db(row['id'], ns)
                        st.success(f"Updated!"); st.rerun()
                    else: st.warning("No transcript found.")
                st.markdown("<hr style='border:none;border-top:1px solid #050508;margin:4px 0 12px;'>", unsafe_allow_html=True)
            if shown==0: st.info("No summaries match your filters.")

    # ── TAB 4 ─────────────────────────────────────────────────────────────────────
    with tab4:
        st.markdown("<div class='section-header'>Voice AI Receptionist — Live Call Log</div>", unsafe_allow_html=True)
        webhook_url = "https://teleronwebhook.pythonanywhere.com"
        try:
            resp2 = requests.get(f"{webhook_url}/health", timeout=5)
            health = resp2.json()
            groq_ok  = health.get("groq_key_set", False)
            gmail_ok = health.get("gmail_set", False)
            st.markdown(f"""<div style="background:rgba(16,185,129,0.05);border:1px solid rgba(16,185,129,0.1);border-radius:14px;padding:16px 20px;margin-bottom:24px;display:flex;align-items:center;gap:16px;">
                <div style="width:10px;height:10px;border-radius:50%;background:#10b981;box-shadow:0 0 10px rgba(16,185,129,0.5);flex-shrink:0;"></div>
                <div><div style="font-size:13px;font-weight:700;color:#34d399;">Webhook Online</div>
                <div style="font-size:11px;color:#1e4a3a;margin-top:2px;">AI: {"✓" if groq_ok else "✗"} &nbsp;·&nbsp; Email: {"✓" if gmail_ok else "✗"} &nbsp;·&nbsp; {webhook_url}</div></div>
            </div>""", unsafe_allow_html=True)
        except:
            st.markdown("""<div style="background:rgba(239,68,68,0.05);border:1px solid rgba(239,68,68,0.1);border-radius:14px;padding:16px 20px;margin-bottom:24px;display:flex;align-items:center;gap:16px;">
                <div style="width:10px;height:10px;border-radius:50%;background:#ef4444;flex-shrink:0;"></div>
                <div><div style="font-size:13px;font-weight:700;color:#f87171;">Webhook Offline</div>
                <div style="font-size:11px;color:#4a1a1a;">Check PythonAnywhere account</div></div>
            </div>""", unsafe_allow_html=True)

        all_calls_df = get_jobs()
        emergency=0; high=0; total_ai=0
        if not all_calls_df.empty:
            for _,r in all_calls_df.iterrows():
                s = parse_summary(r.get("ai_summary",None))
                if s:
                    total_ai+=1
                    urg=s.get("urgency","").lower()
                    if urg=="emergency": emergency+=1
                    elif urg=="high": high+=1

        v1,v2,v3,v4 = st.columns(4)
        with v1: st.markdown(f"""<div class='metric-card blue'><div class='metric-card-top'><div class='metric-icon icon-blue'>📞</div><span class='metric-badge badge-blue'>AI Handled</span></div><div class='metric-number'>{total_ai}</div><div class='metric-title'>AI Calls</div></div>""", unsafe_allow_html=True)
        with v2: st.markdown(f"""<div class='metric-card purple'><div class='metric-card-top'><div class='metric-icon icon-purple'>🌍</div><span class='metric-badge badge-purple'>Auto Detect</span></div><div class='metric-number'>6+</div><div class='metric-title'>Languages</div></div>""", unsafe_allow_html=True)
        with v3: st.markdown(f"""<div class='metric-card amber'><div class='metric-card-top'><div class='metric-icon icon-amber'>⚠️</div><span class='metric-badge badge-amber'>Attention</span></div><div class='metric-number'>{high}</div><div class='metric-title'>High Urgency</div></div>""", unsafe_allow_html=True)
        with v4: st.markdown(f"""<div class='metric-card' style='border-color:rgba(239,68,68,0.1);'><div class='metric-card-top'><div class='metric-icon' style='background:rgba(239,68,68,0.1);color:#f87171;'>🚨</div><span class='metric-badge' style='background:rgba(239,68,68,0.1);color:#f87171;'>Immediate</span></div><div class='metric-number'>{emergency}</div><div class='metric-title'>Emergencies</div></div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        vc1,vc2 = st.columns([1,1], gap="large")
        with vc1:
            st.markdown("<div class='section-header'>Recent AI Calls</div>", unsafe_allow_html=True)
            voice_jobs = get_jobs()
            count=0
            if not voice_jobs.empty:
                for _,row in voice_jobs.iterrows():
                    summary = parse_summary(row.get("ai_summary",None))
                    if not summary: continue
                    count+=1
                    uv = summary.get("urgency","Unknown").lower()
                    sv2 = summary.get("sentiment","Unknown").lower()
                    uc = {"low":"urgency-low","medium":"urgency-medium","high":"urgency-high","emergency":"urgency-emergency"}.get(uv,"urgency-medium")
                    sc2 = {"calm":"sentiment-calm","frustrated":"sentiment-frustrated","urgent":"sentiment-urgent","angry":"sentiment-angry","satisfied":"sentiment-satisfied","confused":"sentiment-confused"}.get(sv2,"sentiment-calm")
                    fhtml = "".join([f"<div class='followup-item'><span class='followup-dot'>›</span>{item}</div>" for item in summary.get("follow_up",[])])
                    st.markdown(f"""
                    <div class='summary-card'>
                        <div class='summary-card-header'>
                            <div><div class='summary-customer'>{row['customer_name']}</div><div class='summary-phone'>📱 {row['phone']}</div><div style='font-size:10px;color:#a78bfa;margin-top:3px;'>🌍 {summary.get("language","English")}</div></div>
                            <div style='text-align:right;'><div class='summary-job-id'>JOB #{row['id']}</div><div style='font-size:11px;color:#1e293b;margin-top:4px;'>{str(row.get("timestamp",""))[:10]}</div><div style='margin-top:6px;'><span class='urgency-badge {uc}'>{summary.get("urgency","—")}</span></div></div>
                        </div>
                        <div class='summary-problem'><div class='summary-problem-label'>Problem</div><div class='summary-problem-value'>{summary.get("problem","—")}</div></div>
                        <div class='summary-grid'>
                            <div class='summary-field'><div class='summary-field-label'>Service</div><div class='summary-field-value'>{summary.get("service_type","—")}</div></div>
                            <div class='summary-field'><div class='summary-field-label'>Tech Skill</div><div class='summary-field-value'>{summary.get("tech_skill","—")}</div></div>
                            <div class='summary-field'><div class='summary-field-label'>Sentiment</div><div class='summary-field-value'><span class='sentiment-badge {sc2}'>{summary.get("sentiment","—")}</span></div></div>
                            <div class='summary-field'><div class='summary-field-label'>Address</div><div class='summary-field-value'>{str(row.get("keywords","—") or "—")[:28]}</div></div>
                        </div>
                        <div class='summary-followup'><div class='summary-followup-label'>Follow-up</div>{fhtml}</div>
                    </div>""", unsafe_allow_html=True)
                    if row.get("status")=="Pending Assignment":
                        tdf = get_technicians(status_filter="Active")
                        tlist = tdf["name"].tolist() if not tdf.empty else ["No Active Technicians"]
                        st2 = st.selectbox(f"Assign — Job #{row['id']}", tlist, key=f"vt_{row['id']}")
                        if st.button(f"🚀  Dispatch #{row['id']}", key=f"vd_{row['id']}", use_container_width=True):
                            supabase.table("jobs").update({"status":"Dispatched","assigned_tech":st2}).eq("id",row["id"]).execute()
                            st.success(f"Dispatched to {st2}!"); st.rerun()
                    else:
                        st.markdown(f"<div style='font-size:11px;color:#34d399;margin-bottom:8px;'>✓ {row.get('status','—')}</div>", unsafe_allow_html=True)
                    st.markdown("<hr style='border:none;border-top:1px solid #050508;margin:6px 0;'>", unsafe_allow_html=True)
            if count==0: st.info("No AI-analysed calls yet.")

        with vc2:
            st.markdown("<div class='section-header'>AI Phone Setup</div>", unsafe_allow_html=True)
            st.markdown(f"""
            <div class='glass-card'>
                <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'>
                    <div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>Webhook URL</div>
                    <div style='font-size:12px;color:#6366f1;word-break:break-all;'>{webhook_url}/vapi-webhook</div>
                </div>
                <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'>
                    <div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>AI Receptionist</div>
                    <div style='font-size:12px;color:#e2e8f0;'>Alex — Teleron AI Receptionist</div>
                </div>
                <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;margin-bottom:10px;'>
                    <div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>Languages</div>
                    <div style='font-size:12px;color:#e2e8f0;'>English · Urdu · Spanish · Arabic · Hindi · French</div>
                </div>
                <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:12px;padding:14px;'>
                    <div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px;'>Services</div>
                    <div style='font-size:12px;color:#e2e8f0;'>HVAC · Plumbing · Electrical · Appliance · Home Services</div>
                </div>
            </div>
            <div style='background:rgba(16,185,129,0.04);border:1px solid rgba(16,185,129,0.08);border-radius:14px;padding:16px;margin-bottom:12px;'>
                <div style='font-size:10px;font-weight:700;color:#34d399;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;'>What Alex Does</div>
                <div style='font-size:12px;color:#1e4a3a;line-height:2;'>✦ Collects name & phone<br>✦ Gets full service address<br>✦ Identifies the problem<br>✦ Detects urgency level<br>✦ Speaks customer's language<br>✦ Saves job to dispatch board<br>✦ Emails summary to team</div>
            </div>
            <div style='background:rgba(245,158,11,0.04);border:1px solid rgba(245,158,11,0.08);border-radius:14px;padding:16px;'>
                <div style='font-size:10px;font-weight:700;color:#fbbf24;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;'>Test Your Voice AI</div>
                <div style='font-size:12px;color:#3d2e00;line-height:1.7;'>Call your Vapi number. Alex will answer and the job appears here within 30 seconds.</div>
            </div>
            """, unsafe_allow_html=True)

    # ── TAB 5 ─────────────────────────────────────────────────────────────────────
    with tab5:
        st.markdown("<div class='section-header'>Job History</div>", unsafe_allow_html=True)
        jdf = get_jobs()
        if not jdf.empty:
            c1,c2,c3 = st.columns(3)
            with c1: st.metric("Total Records", len(jdf))
            with c2: st.metric("Dispatched", len(jdf[jdf['status']=='Dispatched']))
            with c3: st.metric("Pending", len(jdf[jdf['status']=='Pending Assignment']))
            st.markdown("<br>", unsafe_allow_html=True)
            st.dataframe(jdf, use_container_width=True, hide_index=True)
        else:
            st.info("No job records yet.")

    # ── TAB 6 ─────────────────────────────────────────────────────────────────────
    with tab6:
        lc,rc = st.columns([1.1,0.9], gap="large")
        with lc:
            st.markdown("<div class='section-header'>Add New Technician</div>", unsafe_allow_html=True)
            with st.form("add_tech_form", clear_on_submit=True):
                t_id   = st.text_input("Technician ID", placeholder="T006")
                t_name = st.text_input("Full Name",      placeholder="Alex Torres")
                t_zone = st.text_input("Service Zone",   placeholder="North District")
                ca,cb = st.columns(2)
                with ca: t_avg = st.text_input("Avg Ticket ($)", placeholder="$320")
                with cb: t_conv = st.text_input("Conversion Rate", placeholder="78%")
                t_status = st.selectbox("Status", ["Active","Inactive","On Leave"])
                if st.form_submit_button("➕  ADD TECHNICIAN", use_container_width=True):
                    if not t_id.strip() or not t_name.strip():
                        st.error("ID and Name are required.")
                    else:
                        try:
                            insert_technician({"id":t_id.strip(),"name":t_name.strip(),"zone":t_zone.strip(),"avg_ticket":t_avg.strip(),"conversion":t_conv.strip(),"status":t_status})
                            st.success(f"✅ {t_name} added!"); st.rerun()
                        except Exception as e:
                            st.error(f"Error: ID may already exist. ({e})")

            st.markdown("<div class='section-header' style='margin-top:28px;'>Update Status</div>", unsafe_allow_html=True)
            rdf = get_technicians()
            if not rdf.empty:
                tmap = {f"{r['name']} ({r['id']})": r['id'] for _,r in rdf.iterrows()}
                sl = st.selectbox("Select Technician", list(tmap.keys()), key="edit_sel")
                sid = tmap[sl]
                cs = rdf[rdf['id']==sid]['status'].values[0]
                sopts = ["Active","Inactive","On Leave"]
                ns = st.selectbox("New Status", sopts, index=sopts.index(cs) if cs in sopts else 0, key="edit_status")
                if st.button("✓  UPDATE STATUS", use_container_width=True):
                    update_tech_status(sid, ns); st.success(f"Updated to {ns}."); st.rerun()
            else:
                st.info("No technicians yet.")

        with rc:
            st.markdown("<div class='section-header'>Current Roster</div>", unsafe_allow_html=True)
            rdf2 = get_technicians()
            if not rdf2.empty:
                for _,row in rdf2.iterrows():
                    badge = "badge-active" if row['status']=="Active" else "badge-inactive"
                    st.markdown(f"""
                    <div class='tech-card'>
                        <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;'>
                            <div style='font-size:14px;font-weight:700;color:#f1f5f9;'>{row['name']}</div>
                            <span class='{badge}'>{row['status']}</span>
                        </div>
                        <div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;'>
                            <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:8px;padding:8px 10px;'>
                                <div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;'>ID</div>
                                <div style='font-size:12px;color:#94a3b8;font-weight:600;'>{row['id']}</div>
                            </div>
                            <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:8px;padding:8px 10px;'>
                                <div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;'>Zone</div>
                                <div style='font-size:12px;color:#94a3b8;font-weight:600;'>{row.get('zone') or '—'}</div>
                            </div>
                            <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:8px;padding:8px 10px;'>
                                <div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;'>Avg Ticket</div>
                                <div style='font-size:12px;color:#34d399;font-weight:700;'>{row.get('avg_ticket') or '—'}</div>
                            </div>
                            <div style='background:#03030a;border:1px solid #0f0f1a;border-radius:8px;padding:8px 10px;'>
                                <div style='font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;'>Conversion</div>
                                <div style='font-size:12px;color:#818cf8;font-weight:700;'>{row.get('conversion') or '—'}</div>
                            </div>
                        </div>
                    </div>""", unsafe_allow_html=True)
                    if st.button(f"✕  Remove {row['name']}", key=f"del_{row['id']}", use_container_width=True):
                        delete_technician(row['id']); st.warning(f"{row['name']} removed."); st.rerun()
                st.markdown("<div class='section-header' style='margin-top:24px;'>Full Roster Table</div>", unsafe_allow_html=True)
                st.dataframe(rdf2, use_container_width=True, hide_index=True)
            else:
                st.markdown("<div style='text-align:center;padding:60px 20px;color:#0f0f1a;'><div style='font-size:42px;margin-bottom:12px;'>👷</div><div style='font-size:13px;font-weight:600;'>No technicians added yet</div></div>", unsafe_allow_html=True)

else:
    # Show tracking page for the selected metric
    show_tracking_page(st.session_state.page)