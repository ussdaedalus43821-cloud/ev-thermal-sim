"""Streamlit entry point: single-run interactive explorer, with a vehicle
selector over the six real reference profiles.

Presentation only — imports `run_scenario` from `sim.py` and renders its
output. The only "logic" here is display-unit conversion (K<->C, m/s<->km/h,
percent-grade<->radians), which CLAUDE.md's architecture rules explicitly
scope to `dashboard/` the same way as °C/kWh/miles conversions; there are no
simulation equations, thresholds, or physics constants in this file.

The vehicle profile itself now comes from `profile_loader.load_profile`
against the real `configs/profiles/*.yaml` data (see
docs/research/profiles_v1.md) — no more in-memory placeholder profile. The
Scenario wrapped around it (climate/usage/charging profile) is still a
UI-only placeholder, since profile data and scenario data are independent
concerns; only the vehicle profile is user-selectable here.

Side-by-side multi-vehicle overlay views, Monte Carlo result plots, fork
comparison, and causal-trace exploration beyond the flat feed below are out
of scope for this milestone — this dashboard runs one selected vehicle at a
time.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from ev_thermal.profile_loader import PROFILES_DIR, load_profile
from ev_thermal.schema import (
    CausalEvent,
    ChargingProfile,
    Climate,
    Confidence,
    DTPolicy,
    ManufacturerProfile,
    RunResult,
    Scenario,
    Sourced,
    UsageProfile,
)
from ev_thermal.sim import coolant_node_name, run_scenario

CRUISE_SPEED_MPS = 13.4  # ~30 mph — UI demo drive-cycle default

VEHICLE_PROFILES = {
    "Nissan Leaf": "nissan_leaf",
    "Tesla Model 3": "tesla_model3",
    "Chevrolet Bolt": "chevrolet_bolt",
    "Audi e-tron": "audi_etron",
    "BMW i3": "bmw_i3",
    "Kia Niro EV": "kia_niro_ev",
}
"""Display name -> configs/profiles/<stem>.yaml, per docs/research/profiles_v1.md."""

SEVERITY_COLOR = {
    "critical": "#e74c3c",  # fault
    "warning": "#f39c12",  # derate
    "info": "#2ecc71",  # milestone
}
"""UI-only color mapping onto CausalEvent.severity, a field the physics layer
already sets (see causal.py/controller.py/motor.py/battery.py/inverter.py's
own severity= calls) — not a new classification invented in the UI."""


class _EventGroup(NamedTuple):
    event_type: str
    affected_component: str
    severity: str
    description: str
    first_sim_time_s: float
    last_sim_time_s: float
    fired_count: int


def _dedupe_consecutive_events(events: list[CausalEvent]) -> list[_EventGroup]:
    """Collapses immediately-consecutive CausalEvents with identical event_type,
    affected_component, and description (the same what/why/consequence, not just
    the same event_type) into one group with a fired-count and a time span, so a
    long run of an unchanging condition (e.g. a sustained derate lockout logging
    the same event every step) doesn't spam the feed with hundreds of duplicate
    lines. Events whose description text differs (even by a changing number) are
    NOT merged — that's a UI-only exact-match dedup, not a new similarity
    threshold invented on top of the physics layer's own event data.
    """
    groups: list[_EventGroup] = []
    for event in events:
        if groups:
            last = groups[-1]
            if (
                event.event_type == last.event_type
                and event.affected_component == last.affected_component
                and event.description == last.description
            ):
                groups[-1] = last._replace(last_sim_time_s=event.sim_time_s, fired_count=last.fired_count + 1)
                continue
        groups.append(
            _EventGroup(
                event_type=event.event_type,
                affected_component=event.affected_component,
                severity=event.severity,
                description=event.description,
                first_sim_time_s=event.sim_time_s,
                last_sim_time_s=event.sim_time_s,
                fired_count=1,
            )
        )
    return groups


COMPONENT_COLOR = {
    "motor": "#1f77b4",
    "inverter": "#ff7f0e",
    "battery": "#2ca02c",
    "coolant": "#9467bd",
}


def _sourced(value: float, source: str = "placeholder") -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source=source)


def build_scenario(profile: ManufacturerProfile, ambient_temp_k: float) -> Scenario:
    climate = Climate(
        location_name="Dashboard Run",
        mean_annual_temp_k=_sourced(ambient_temp_k),
        seasonal_amplitude_k=_sourced(10.0),
        daily_amplitude_k=_sourced(5.0),
        altitude_m=_sourced(100.0),
        solar_irradiance_w_per_m2=_sourced(800.0),
    )
    usage_profile = UsageProfile(
        annual_mileage_km=_sourced(15_000.0),
        mean_trip_km=_sourced(8.05),  # 5 miles
        trip_km_std=_sourced(3.0),
        driving_style="normal",
        garage_parked=True,
    )
    charging_profile = ChargingProfile(
        charger_type="Level 2",
        charge_power_kw=_sourced(7.2),
        typical_start_soc=_sourced(0.5),
        typical_end_soc=_sourced(0.8),
        dc_fast_charge_fraction=_sourced(0.05),
        preconditioning_enabled=False,
        dc_charge_power_kw=_sourced(50.0),
        charge_reset_hour=_sourced(22.0),
    )
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")

    return Scenario(
        name="dashboard-run",
        manufacturer_profile=profile,
        climate=climate,
        usage_profile=usage_profile,
        charging_profile=charging_profile,
        duration_years=1,
        dt_policy=dt_policy,
        random_seed=42,
    )


def target_speed_mps(t: float) -> float:
    """Same stop-and-go commute segment as examples/first_run.py: ramp to
    cruise, a red-light stop partway through (shows regen braking), then
    back to cruise. Not a real drive-cycle model (see CLAUDE.md).
    """
    if t < 30.0:
        return CRUISE_SPEED_MPS * (t / 30.0)
    if 200.0 <= t < 220.0:
        return max(0.0, CRUISE_SPEED_MPS * (1.0 - (t - 200.0) / 20.0))
    if 220.0 <= t < 240.0:
        return 0.0
    if 240.0 <= t < 260.0:
        return CRUISE_SPEED_MPS * ((t - 240.0) / 20.0)
    return CRUISE_SPEED_MPS


# --- display-unit conversions (K<->C, m/s<->km/h, percent-grade<->radians) ---


def k_to_c(temp_k: float) -> float:
    return temp_k - 273.15


def c_to_k(temp_c: float) -> float:
    return temp_c + 273.15


def mps_to_kmh(speed_mps: float) -> float:
    return speed_mps * 3.6


def percent_grade_to_rad(percent: float) -> float:
    return math.atan(percent / 100.0)


# --- page layout ---

st.set_page_config(page_title="EV Thermal Simulator", layout="wide")
st.title("EV Thermal Simulator")

left_col, center_col, right_col = st.columns([1, 2, 1])

with left_col:
    st.subheader("Controls")
    vehicle_name = st.selectbox("Vehicle", options=list(VEHICLE_PROFILES.keys()))
    ambient_temp_c = st.slider("Ambient temp (°C)", min_value=0, max_value=50, value=25)
    driver_aggression = st.slider("Driver aggression", min_value=0.7, max_value=1.3, value=1.0, step=0.05)
    trip_duration_s = st.select_slider("Trip duration (s)", options=[60, 300, 600, 1800], value=600)
    road_grade_percent = st.slider("Road grade (%)", min_value=-5.0, max_value=10.0, value=0.0, step=0.5)
    pump_aggressiveness = st.slider("Pump policy aggressiveness", min_value=0.5, max_value=2.0, value=1.0, step=0.05)
    st.slider(
        "Compute load (kW)",
        min_value=0.0,
        max_value=5.0,
        value=0.0,
        step=0.1,
        disabled=True,
        help="TODO(aux): aux_compute.py is still a stub (no HVAC/accessory physics implemented yet), "
        "so this control has no effect on the run.",
    )
    run_clicked = st.button("Run", type="primary", use_container_width=True)

run_output: tuple[RunResult, ManufacturerProfile] | None = None
if run_clicked:
    with st.spinner("Running simulation..."):
        run_profile = load_profile(PROFILES_DIR / f"{VEHICLE_PROFILES[vehicle_name]}.yaml")
        scenario = build_scenario(run_profile, ambient_temp_k=c_to_k(ambient_temp_c))
        run_result = run_scenario(
            run_profile,
            scenario,
            duration_s=float(trip_duration_s),
            target_speed_mps=target_speed_mps,
            driver_aggression=driver_aggression,
            grade_rad=percent_grade_to_rad(road_grade_percent),
            pump_aggressiveness=pump_aggressiveness,
        )
        run_output = (run_result, run_profile)

with center_col:
    if run_output is None:
        st.info("Set parameters on the left and click **Run**.")
    else:
        result, profile = run_output
        ts = result.time_series
        motor_name = profile.motors[0].thermal_node.name
        inverter_name = profile.inverter.thermal_node.name
        battery_name = profile.battery.thermal_node.name

        speed_torque_fig = make_subplots(specs=[[{"secondary_y": True}]])
        speed_torque_fig.add_trace(
            go.Scatter(x=ts.time_s, y=[mps_to_kmh(v) for v in ts.speed_mps], name="speed (km/h)"),
            secondary_y=False,
        )
        speed_torque_fig.add_trace(
            go.Scatter(
                x=ts.time_s, y=ts.requested_torque_nm, name="requested torque (Nm)", line=dict(dash="dot")
            ),
            secondary_y=True,
        )
        speed_torque_fig.add_trace(
            go.Scatter(x=ts.time_s, y=ts.commanded_torque_nm, name="commanded torque (Nm)"),
            secondary_y=True,
        )
        speed_torque_fig.update_layout(title="Speed & Torque (request vs. commanded)", height=280, margin=dict(t=40))
        speed_torque_fig.update_yaxes(title_text="speed (km/h)", secondary_y=False)
        speed_torque_fig.update_yaxes(title_text="torque (Nm)", secondary_y=True)
        st.plotly_chart(speed_torque_fig, use_container_width=True)

        soc_fig = go.Figure()
        soc_fig.add_trace(go.Scatter(x=ts.time_s, y=[soc * 100.0 for soc in ts.soc], name="SOC (%)"))
        soc_fig.update_layout(title="State of Charge", height=220, margin=dict(t=40), yaxis_title="SOC (%)")
        st.plotly_chart(soc_fig, use_container_width=True)

        temp_fig = go.Figure()
        for node_name, label in [
            (motor_name, "motor"),
            (inverter_name, "inverter"),
            (battery_name, "battery"),
        ]:
            temp_fig.add_trace(
                go.Scatter(
                    x=ts.time_s,
                    y=[k_to_c(t) for t in ts.node_temps_k[node_name]],
                    name=label,
                    line=dict(color=COMPONENT_COLOR[label]),
                )
            )
        cooling_loops = profile.thermal_architecture.cooling_loops
        for loop_index, loop in enumerate(cooling_loops):
            coolant_name = coolant_node_name(loop.name)
            trace_label = "coolant" if len(cooling_loops) == 1 else f"coolant ({loop.name})"
            temp_fig.add_trace(
                go.Scatter(
                    x=ts.time_s,
                    y=[k_to_c(t) for t in ts.node_temps_k[coolant_name]],
                    name=trace_label,
                    line=dict(color=COMPONENT_COLOR["coolant"], dash="solid" if loop_index == 0 else "dot"),
                )
            )
        derate_temps_k = {
            "motor": profile.motors[0].thermal_node.derate_temp_k.value,
            "inverter": profile.inverter.thermal_node.derate_temp_k.value,
            "battery": profile.battery.thermal_node.derate_temp_k.value,
        }
        for label, derate_temp_k in derate_temps_k.items():
            temp_fig.add_hline(
                y=k_to_c(derate_temp_k),
                line=dict(color=COMPONENT_COLOR[label], dash="dash", width=1),
                annotation_text=f"{label} derate",
                annotation_position="top left",
                annotation_font_size=10,
            )
        temp_fig.update_layout(title="Component Temperatures", height=320, margin=dict(t=40), yaxis_title="°C")
        st.plotly_chart(temp_fig, use_container_width=True)

        duty_fig = go.Figure()
        for loop in cooling_loops:
            pump_label = "pump duty" if len(cooling_loops) == 1 else f"pump duty ({loop.name})"
            fan_label = "fan duty" if len(cooling_loops) == 1 else f"fan duty ({loop.name})"
            duty_fig.add_trace(
                go.Scatter(x=ts.time_s, y=[step[loop.name] for step in ts.pump_duty], name=pump_label)
            )
            duty_fig.add_trace(go.Scatter(x=ts.time_s, y=[step[loop.name] for step in ts.fan_duty], name=fan_label))
        duty_fig.update_layout(title="Pump / Fan Duty", height=220, margin=dict(t=40), yaxis_title="duty (0-1)")
        st.plotly_chart(duty_fig, use_container_width=True)

with right_col:
    st.subheader("Causal Events")
    if run_output is None:
        st.caption("Run a scenario to see events.")
    else:
        result, _profile = run_output
        event_groups = _dedupe_consecutive_events(result.causal_events)
        with st.container(height=900):
            for group in reversed(event_groups):
                color = SEVERITY_COLOR.get(group.severity, "#888888")
                if group.fired_count > 1:
                    span_s = group.last_sim_time_s - group.first_sim_time_s
                    time_label = f"t={group.first_sim_time_s:.1f}-{group.last_sim_time_s:.1f}s"
                    fired_label = f" &middot; fired {group.fired_count} times over {span_s:.1f}s"
                else:
                    time_label = f"t={group.first_sim_time_s:.1f}s"
                    fired_label = ""
                st.markdown(
                    f"<div style='border-left: 4px solid {color}; padding: 4px 0 4px 10px; margin-bottom: 10px;'>"
                    f"<div style='font-size: 0.85em; color: {color}; font-weight: 600;'>"
                    f"{time_label} &middot; {group.event_type} &middot; {group.affected_component}"
                    f"{fired_label}"
                    f"</div>"
                    f"<div style='font-size: 0.9em;'>{group.description}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            if not event_groups:
                st.caption("No events this run.")
