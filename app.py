"""
Bus Charging Scheduler — Streamlit UI
======================================
Layout:
  1. Scenario selector
  2. Scenario input view (route, stations, weights, bus list)
  3. Per-bus timetable
  4. Per-station charging order
"""
import os
import sys
from pathlib import Path

# Make the project root importable regardless of working directory
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import streamlit as st
import pandas as pd

from scheduler.loader import load_all_scenarios
from scheduler.engine import schedule_scenario
from scheduler.models import Scenario, ScheduleResult, BusSchedule

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Bus Charging Scheduler",
    page_icon="🚌",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIRECTION_LABEL = {"BK": "Bengaluru → Kochi", "KB": "Kochi → Bengaluru"}
OPERATOR_COLORS = {
    "kpn":      "#1E88E5",
    "freshbus": "#43A047",
    "flixbus":  "#8E24AA",
}

def fmt_min(m: int) -> str:
    """Minutes-since-midnight → 'HH:MM'."""
    h = (m // 60) % 24
    mn = m % 60
    return f"{h:02d}:{mn:02d}"

def operator_badge(op: str) -> str:
    color = OPERATOR_COLORS.get(op, "#777")
    return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:4px;font-size:0.78rem;font-weight:600">{op.upper()}</span>'

def direction_arrow(d: str) -> str:
    return "→ Kochi" if d == "BK" else "→ Bengaluru"

# ---------------------------------------------------------------------------
# Load all scenarios (cached)
# ---------------------------------------------------------------------------

@st.cache_data
def get_scenarios():
    scenarios_dir = ROOT / "scenarios"
    return load_all_scenarios(scenarios_dir)

@st.cache_data
def run_schedule(scenario_name: str):
    scenarios = get_scenarios()
    scenario = scenarios[scenario_name]
    return scenario, schedule_scenario(scenario)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .section-header {
        font-size: 1.1rem;
        font-weight: 700;
        color: #1a1a2e;
        border-bottom: 2px solid #e0e0e0;
        padding-bottom: 4px;
        margin-top: 1.5rem;
        margin-bottom: 0.75rem;
    }
    .metric-box {
        background: #f8f9fa;
        border-radius: 8px;
        padding: 12px 16px;
        text-align: center;
    }
    .route-pill {
        display: inline-block;
        background: #1E88E5;
        color: white;
        border-radius: 20px;
        padding: 4px 14px;
        font-size: 0.85rem;
        font-weight: 600;
        margin: 2px;
    }
    .station-pill {
        display: inline-block;
        background: #FF6F00;
        color: white;
        border-radius: 20px;
        padding: 4px 14px;
        font-size: 0.85rem;
        font-weight: 600;
        margin: 2px;
    }
    .wait-badge {
        color: #d32f2f;
        font-weight: 600;
    }
    .ok-badge {
        color: #388e3c;
        font-weight: 600;
    }
    table { width: 100%; }
    thead th { background: #f0f2f6 !important; }
</style>
""", unsafe_allow_html=True)

st.title("🚌 Bus Charging Scheduler")
st.caption("Bengaluru ↔ Kochi — Electric Bus Fleet Charging Optimiser")

# ---------------------------------------------------------------------------
# Scenario selector
# ---------------------------------------------------------------------------

scenarios = get_scenarios()
scenario_names = list(scenarios.keys())

col_sel, col_info = st.columns([3, 5])
with col_sel:
    selected = st.selectbox(
        "**Select Scenario**",
        scenario_names,
        index=0,
        help="Each scenario represents a different departure pattern and weight configuration.",
    )

scenario, result = run_schedule(selected)

with col_info:
    st.markdown(f"<div style='padding-top:8px'><b>Description:</b> {scenario.meta.description}</div>", unsafe_allow_html=True)
    wcols = st.columns(3)
    wcols[0].metric("Individual weight", scenario.weights.individual)
    wcols[1].metric("Operator weight", scenario.weights.operator)
    wcols[2].metric("Overall weight", scenario.weights.overall)

st.divider()

# ---------------------------------------------------------------------------
# SECTION 1: Scenario Input View
# ---------------------------------------------------------------------------

st.markdown('<div class="section-header">📋 Scenario Input</div>', unsafe_allow_html=True)

tab_route, tab_buses = st.tabs(["Route & Stations", "Bus Departure Schedule"])

with tab_route:
    rc1, rc2, rc3 = st.columns([2, 2, 1])

    with rc1:
        st.markdown("**Route**")
        waypoints = scenario.route.waypoints
        route_html = " &rarr; ".join(
            f'<span class="{"route-pill" if wp in ["Bengaluru","Kochi"] else "station-pill"}">{wp}</span>'
            for wp in waypoints
        )
        st.markdown(route_html, unsafe_allow_html=True)
        st.markdown("")

        seg_rows = []
        for seg in scenario.route.segments:
            travel_min = (seg.distance_km / scenario.physics.speed_kmh) * 60
            seg_rows.append({
                "From": seg.from_stop,
                "To": seg.to_stop,
                "Distance (km)": seg.distance_km,
                "Travel time (min)": f"{travel_min:.0f}",
            })
        st.dataframe(pd.DataFrame(seg_rows), hide_index=True, use_container_width=True)

    with rc2:
        st.markdown("**Charging Stations**")
        stn_rows = []
        for s in scenario.stations:
            stn_rows.append({
                "Station": s.name,
                "Location": s.location,
                "Chargers": s.chargers,
            })
        st.dataframe(pd.DataFrame(stn_rows), hide_index=True, use_container_width=True)

    with rc3:
        st.markdown("**Physics**")
        p = scenario.physics
        st.markdown(f"- **Range:** {p.battery_range_km} km")
        st.markdown(f"- **Speed:** {p.speed_kmh} km/h")
        st.markdown(f"- **Charge time:** {p.charge_duration_min} min")
        st.markdown(f"- **Fills to full:** {'Yes' if p.charge_fills_to_full else 'No'}")

with tab_buses:
    bus_input_rows = []
    bus_map_ui = {b.id: b for b in scenario.buses}
    for bus in scenario.buses:
        bus_input_rows.append({
            "Bus ID": bus.id,
            "Operator": bus.operator.upper(),
            "Direction": DIRECTION_LABEL[bus.direction],
            "Departure": bus.departure,
        })
    df_buses = pd.DataFrame(bus_input_rows)
    st.dataframe(df_buses, hide_index=True, use_container_width=True, height=420)

st.divider()

# ---------------------------------------------------------------------------
# SECTION 2: Per-Bus Timetable
# ---------------------------------------------------------------------------

st.markdown('<div class="section-header">🗓️ Per-Bus Timetable</div>', unsafe_allow_html=True)

# Filter controls
fcol1, fcol2 = st.columns([2, 2])
with fcol1:
    dir_filter = st.selectbox(
        "Filter by direction",
        ["All", "Bengaluru → Kochi", "Kochi → Bengaluru"],
        key="dir_filter",
    )
with fcol2:
    ops = ["All"] + [op.upper() for op in scenario.operators]
    op_filter = st.selectbox("Filter by operator", ops, key="op_filter")

def passes_filters(bs: BusSchedule) -> bool:
    if dir_filter == "Bengaluru → Kochi" and bs.direction != "BK":
        return False
    if dir_filter == "Kochi → Bengaluru" and bs.direction != "KB":
        return False
    if op_filter != "All" and bs.operator.upper() != op_filter:
        return False
    return True

filtered_schedules = [bs for bs in result.bus_schedules if passes_filters(bs)]

for bs in filtered_schedules:
    bus = bus_map_ui[bs.bus_id]
    with st.expander(
        f"**{bs.bus_id}** — {bs.operator.upper()} — {DIRECTION_LABEL[bs.direction]} — "
        f"Dep {bus.departure} → Arr **{fmt_min(bs.arrival_min)}** "
        f"| Total wait: {'⚠️ ' if bs.total_wait_min > 30 else ''}{bs.total_wait_min} min",
        expanded=False,
    ):
        if not bs.charge_events:
            st.info("No charging stops scheduled (range sufficient — shouldn't happen for a full trip).")
            continue

        rows = []
        # Departure row
        rows.append({
            "Event": "🟢 Depart",
            "Location": bus.origin,
            "Time": bus.departure,
            "Wait (min)": "—",
            "Charge end": "—",
            "Range on arrival (km)": f"{scenario.physics.battery_range_km:.0f} (full)",
        })
        for ce in bs.charge_events:
            rows.append({
                "Event": "⚡ Charge",
                "Location": ce.station_id,
                "Time": fmt_min(ce.arrive_min),
                "Wait (min)": str(ce.wait_min) if ce.wait_min else "0 (no wait)",
                "Charge end": fmt_min(ce.charge_end_min),
                "Range on arrival (km)": f"{ce.range_on_arrival_km:.1f}",
            })
        # Arrival row
        rows.append({
            "Event": "🏁 Arrive",
            "Location": bus.destination,
            "Time": fmt_min(bs.arrival_min),
            "Wait (min)": "—",
            "Charge end": "—",
            "Range on arrival (km)": "—",
        })
        st.table(pd.DataFrame(rows))

st.divider()

# ---------------------------------------------------------------------------
# SECTION 3: Per-Station View
# ---------------------------------------------------------------------------

st.markdown('<div class="section-header">⚡ Per-Station Charging Order</div>', unsafe_allow_html=True)

scols = st.columns(len(scenario.stations))
for col, station in zip(scols, scenario.stations):
    with col:
        st.markdown(f"**Station {station.id}** ({station.chargers} charger{'s' if station.chargers > 1 else ''})")
        log = result.station_logs.get(station.id)
        if not log or not log.queue:
            st.info("No buses charged here.")
            continue

        q_rows = []
        for i, (bus_id, start, end) in enumerate(log.queue, 1):
            bs_ref = result.get_bus(bus_id)
            op = bs_ref.operator if bs_ref else "?"
            color = OPERATOR_COLORS.get(op, "#777")
            q_rows.append({
                "#": i,
                "Bus": bus_id,
                "Operator": op.upper(),
                "Start": fmt_min(start),
                "End": fmt_min(end),
                "Wait": f"{start - next((e.arrive_min for e in bs_ref.charge_events if e.station_id == station.id), start)} min" if bs_ref else "?",
            })
        st.dataframe(pd.DataFrame(q_rows), hide_index=True, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# SECTION 4: Summary Stats
# ---------------------------------------------------------------------------

st.markdown('<div class="section-header">📊 Summary</div>', unsafe_allow_html=True)

s1, s2, s3, s4 = st.columns(4)
all_waits = [bs.total_wait_min for bs in result.bus_schedules]
all_arrivals = [bs.arrival_min for bs in result.bus_schedules]
all_departures = [b.departure_min for b in scenario.buses]

s1.metric("Total buses", len(result.bus_schedules))
s2.metric("Avg wait per bus", f"{sum(all_waits)/len(all_waits):.1f} min")
s3.metric("Max wait (any bus)", f"{max(all_waits)} min")
s4.metric("Last arrival", fmt_min(max(all_arrivals)))

# Per-operator breakdown
st.markdown("**Per-Operator Wait Summary**")
op_data = {}
for bs in result.bus_schedules:
    op_data.setdefault(bs.operator, []).append(bs.total_wait_min)

op_rows = []
for op, waits in sorted(op_data.items()):
    op_rows.append({
        "Operator": op.upper(),
        "Buses": len(waits),
        "Total wait (min)": sum(waits),
        "Avg wait (min)": f"{sum(waits)/len(waits):.1f}",
        "Max wait (min)": max(waits),
    })
st.dataframe(pd.DataFrame(op_rows), hide_index=True, use_container_width=True)

st.caption(f"Scenario: {scenario.meta.id} | Version {scenario.meta.version} | Scheduler: Priority-Queue Greedy + Weighted Cost Rules")
