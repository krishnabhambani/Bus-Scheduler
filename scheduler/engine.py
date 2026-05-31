"""
Bus Charging Scheduler — Core Engine
=====================================

Architecture: Two-Phase Event-Driven Simulation
-------------------------------------------------
Phase 1 — Station assignment: greedy range-first algorithm determines which
          stations each bus must stop at (minimum stops, maximum range used).

Phase 2 — Contention resolution: an event loop processes arrivals at each
          station. When multiple buses are waiting, the weighted cost function
          ranks them — lowest score charges first.

Contention resolution detail
-----------------------------
Buses arriving at the same station within a "contention window" (any bus
that has arrived but hasn't yet started charging) are ranked by:

    score = W_individual * individual_cost(bus)
          + W_operator   * operator_cost(bus)
          + W_overall    * overall_cost(bus)

The bus with the lowest score charges next. Wait times propagate forward
to that bus's next station.

Adding a new rule
-----------------
1. Define:   def rule_myname(bus, ctx) -> float
2. Register: RULE_REGISTRY["myname"] = (rule_myname, "myname")
3. Add key:  class Weights: myname: float = 1.0
The engine loop never changes.

Adding a new weight
--------------------
Add one field to the Weights dataclass and one entry in RULE_REGISTRY.
The engine reads weights dynamically via getattr — no engine code changes.
"""
from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from scheduler.models import (
    Bus, BusSchedule, ChargeEvent, Route, Scenario,
    ScheduleResult, Station, StationLog, Weights,
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

RuleContext = dict
RuleDef = Tuple[Callable[[Bus, RuleContext], float], str]


# ---------------------------------------------------------------------------
# Built-in soft rules (each is a pure function, no engine coupling)
# ---------------------------------------------------------------------------

def rule_individual(bus: Bus, ctx: RuleContext) -> float:
    """
    Priority for buses that have accumulated less waiting so far.
    Buses with less wait get lower score → go first.
    This rewards buses that have been penalised least, keeping individual
    wait times balanced across the fleet.
    """
    return ctx["accumulated_wait_min"].get(bus.id, 0.0)


def rule_operator(bus: Bus, ctx: RuleContext) -> float:
    """
    Priority for operators whose fleet is running furthest behind.
    Computes average wait across all buses of this operator so far.
    A high operator average → high score → that operator's buses
    get de-prioritised, producing equitable spread across operators.
    
    NOTE: When operator weight is HIGH, buses from high-average-wait
    operators are actually de-prioritised (they've already accumulated
    wait). This keeps operators balanced rather than stacking one.
    To *favour* a specific operator, use a negative weight (experimental).
    """
    op_waits = ctx["operator_total_wait"]
    op_counts = ctx["operator_bus_count"]
    op = bus.operator
    count = max(op_counts.get(op, 1), 1)
    avg = op_waits.get(op, 0.0) / count
    return avg


def rule_overall(bus: Bus, ctx: RuleContext) -> float:
    """
    Prefer buses whose naive earliest finish time is soonest.
    Minimises maximum makespan across the network.
    A bus already running late gets higher score (deprioritised slightly)
    to avoid compounding lateness into a cascade.
    """
    return ctx["earliest_finish_min"].get(bus.id, 0.0)


# Rule registry — the only place to add new rules
# Format: name → (function, weight_attribute_name_on_Weights)
RULE_REGISTRY: Dict[str, RuleDef] = {
    "individual": (rule_individual, "individual"),
    "operator":   (rule_operator,   "operator"),
    "overall":    (rule_overall,    "overall"),
}


# ---------------------------------------------------------------------------
# Physics helpers
# ---------------------------------------------------------------------------

def _travel_time_min(distance_km: float, speed_kmh: float) -> float:
    return (distance_km / speed_kmh) * 60.0


def _stations_for_bus(bus: Bus, scenario: Scenario) -> List[Station]:
    return scenario.stations_in_direction(bus.direction)


def _cumulative_distances(bus: Bus, scenario: Scenario) -> Dict[str, float]:
    """
    Returns a dict {waypoint_name: cumulative_km_from_origin} for this bus.
    Works for both directions by walking the route in travel order.
    """
    route = scenario.route
    all_wp = route.waypoints
    o_idx = all_wp.index(bus.origin)
    d_idx = all_wp.index(bus.destination)
    forward = o_idx < d_idx
    ordered = all_wp[o_idx: d_idx + 1] if forward else list(reversed(all_wp[d_idx: o_idx + 1]))

    cum: Dict[str, float] = {ordered[0]: 0.0}
    for i in range(1, len(ordered)):
        a, b = ordered[i - 1], ordered[i]
        seg = next(
            (s for s in route.segments if (s.from_stop == a and s.to_stop == b)
             or (not forward and s.from_stop == b and s.to_stop == a)),
            None
        )
        if seg is None:
            raise ValueError(f"No segment found between {a} and {b}")
        cum[ordered[i]] = cum[a] + seg.distance_km
    return cum


def _choose_charging_stops(bus: Bus, scenario: Scenario) -> List[str]:
    """
    Greedy range-first stop selection.
    Strategy: drive as far as possible before charging (charge as late as possible).
    This minimises total charging stops while guaranteeing the hard range constraint.
    
    Returns ordered list of station IDs this bus must charge at.
    """
    physics = scenario.physics
    cum = _cumulative_distances(bus, scenario)
    avail_stations = _stations_for_bus(bus, scenario)
    max_range = physics.battery_range_km

    chosen: List[str] = []
    last_charge_cum = 0.0  # cum distance of last charge point (origin = 0)
    remaining = list(avail_stations)

    while remaining:
        reachable = [
            s for s in remaining
            if cum[s.location] - last_charge_cum <= max_range
        ]
        if not reachable:
            break  # physically impossible — surface this elsewhere

        dist_to_dest = cum[bus.destination] - last_charge_cum
        if dist_to_dest <= max_range:
            break  # can reach destination from here — done

        # Must charge: pick farthest reachable (greedy)
        best = reachable[-1]
        chosen.append(best.id)
        last_charge_cum = cum[best.location]
        idx = remaining.index(best)
        remaining = remaining[idx + 1:]

    return chosen


# ---------------------------------------------------------------------------
# Weighted score
# ---------------------------------------------------------------------------

def _score(bus: Bus, ctx: RuleContext, weights: Weights) -> float:
    total = 0.0
    for rule_name, (rule_fn, weight_attr) in RULE_REGISTRY.items():
        w = getattr(weights, weight_attr, 1.0)
        total += w * rule_fn(bus, ctx)
    return total


# ---------------------------------------------------------------------------
# Station contention manager
# ---------------------------------------------------------------------------

class StationQueue:
    """
    Manages N parallel charger slots at one station.
    When a bus arrives, it either starts immediately or waits.
    The order among simultaneous/queued buses is determined by the caller
    (weighted scoring) — this class just tracks slot availability.
    """

    def __init__(self, station: Station):
        self.station = station
        self.chargers = station.chargers
        # min-heap of charger-free times
        self._free_at: List[int] = [0] * station.chargers
        heapq.heapify(self._free_at)
        self.log: List[Tuple[str, int, int]] = []  # (bus_id, start, end)

    def earliest_start(self, arrive_min: int) -> int:
        """When would a bus arriving now be able to start charging?"""
        return max(arrive_min, min(self._free_at))

    def commit(self, bus_id: str, arrive_min: int, charge_duration: int) -> Tuple[int, int]:
        """Reserve a charger slot. Returns (charge_start, charge_end)."""
        earliest_free = heapq.heappop(self._free_at)
        charge_start = max(arrive_min, earliest_free)
        charge_end = charge_start + charge_duration
        heapq.heappush(self._free_at, charge_end)
        self.log.append((bus_id, charge_start, charge_end))
        return charge_start, charge_end


# ---------------------------------------------------------------------------
# Main scheduler
# ---------------------------------------------------------------------------

def schedule_scenario(scenario: Scenario) -> ScheduleResult:
    """
    Simulate all buses, resolve contention with weighted scoring.

    Event loop:
    - Each pending charge is (predicted_arrival_min, bus_id, station_id).
    - We process in arrival order (min-heap).
    - When arrival times tie (within 1 min resolution), we score all tied
      buses and serve them in ascending score order.
    - After a bus charges, its departure from that station seeds its next
      event (if more stops remain).
    """
    physics = scenario.physics
    weights = scenario.weights

    # Precompute cumulative distances per bus
    bus_cum: Dict[str, Dict[str, float]] = {}
    bus_stops: Dict[str, List[str]] = {}
    for bus in scenario.buses:
        bus_cum[bus.id] = _cumulative_distances(bus, scenario)
        bus_stops[bus.id] = _choose_charging_stops(bus, scenario)

    # Station queues
    sq: Dict[str, StationQueue] = {s.id: StationQueue(s) for s in scenario.stations}

    # Per-bus mutable state
    bus_map: Dict[str, Bus] = {b.id: b for b in scenario.buses}
    bus_current_time: Dict[str, float] = {b.id: float(b.departure_min) for b in scenario.buses}
    bus_last_loc: Dict[str, str] = {b.id: b.origin for b in scenario.buses}
    bus_stop_idx: Dict[str, int] = {b.id: 0 for b in scenario.buses}
    bus_events: Dict[str, List[ChargeEvent]] = {b.id: [] for b in scenario.buses}

    # Scoring state
    accumulated_wait: Dict[str, float] = {b.id: 0.0 for b in scenario.buses}
    operator_total_wait: Dict[str, float] = defaultdict(float)
    operator_bus_count: Dict[str, int] = defaultdict(int)
    for b in scenario.buses:
        operator_bus_count[b.operator] += 1

    # Naive finish estimate (for overall rule) — no contention
    def _naive_finish(bus: Bus) -> float:
        dist = bus_cum[bus.id].get(bus.destination, 0.0)
        travel = _travel_time_min(dist, physics.speed_kmh)
        n_stops = len(bus_stops[bus.id])
        return bus.departure_min + travel + n_stops * physics.charge_duration_min

    earliest_finish: Dict[str, float] = {b.id: _naive_finish(b) for b in scenario.buses}

    # Event heap: (arrive_min_float, bus_id, station_id)
    event_heap: List[Tuple[float, str, str]] = []

    def _enqueue_next(bus_id: str):
        stops = bus_stops[bus_id]
        idx = bus_stop_idx[bus_id]
        if idx >= len(stops):
            return
        station_id = stops[idx]
        station = scenario.station_by_id(station_id)
        last_loc = bus_last_loc[bus_id]
        dist = bus_cum[bus_id][station.location] - bus_cum[bus_id][last_loc]
        arrive = bus_current_time[bus_id] + _travel_time_min(dist, physics.speed_kmh)
        heapq.heappush(event_heap, (arrive, bus_id, station_id))

    for bus in scenario.buses:
        _enqueue_next(bus.id)

    # -----------------------------------------------------------------------
    # Event loop
    # -----------------------------------------------------------------------
    while event_heap:
        # Peek the soonest arrival
        min_time = event_heap[0][0]

        # Collect all events arriving at the same station within 1 minute
        # (contention window) — score them and serve in order
        # Group by station_id to resolve per-station contention independently
        station_arrivals: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
        temp: List[Tuple[float, str, str]] = []

        while event_heap and event_heap[0][0] <= min_time + 0.99:
            arrive, bid, sid = heapq.heappop(event_heap)
            station_arrivals[sid].append((arrive, bid))

        # For each station with multiple simultaneous arrivals, score and order
        for sid, arrivals in station_arrivals.items():
            if len(arrivals) == 1:
                ordered = arrivals
            else:
                ctx = {
                    "accumulated_wait_min": accumulated_wait,
                    "operator_total_wait": dict(operator_total_wait),
                    "operator_bus_count": dict(operator_bus_count),
                    "earliest_finish_min": earliest_finish,
                }
                scored = [
                    (_score(bus_map[bid], ctx, weights), arrive, bid)
                    for arrive, bid in arrivals
                ]
                scored.sort()
                ordered = [(arr, bid) for (_, arr, bid) in scored]

            for arrive_float, bus_id in ordered:
                arrive_min = int(arrive_float)
                bus = bus_map[bus_id]
                station = scenario.station_by_id(sid)

                # Range on arrival
                last_loc = bus_last_loc[bus_id]
                dist_leg = bus_cum[bus_id][station.location] - bus_cum[bus_id][last_loc]
                range_on_arrival = physics.battery_range_km - dist_leg

                # Commit to charger
                charge_start, charge_end = sq[sid].commit(
                    bus_id, arrive_min, int(physics.charge_duration_min)
                )
                wait = charge_start - arrive_min

                accumulated_wait[bus_id] += wait
                operator_total_wait[bus.operator] += wait

                ce = ChargeEvent(
                    station_id=sid,
                    arrive_min=arrive_min,
                    wait_min=wait,
                    charge_start_min=charge_start,
                    charge_end_min=charge_end,
                    range_on_arrival_km=round(range_on_arrival, 1),
                )
                bus_events[bus_id].append(ce)

                # Advance bus state
                bus_current_time[bus_id] = float(charge_end)
                bus_last_loc[bus_id] = station.location
                bus_stop_idx[bus_id] += 1

                _enqueue_next(bus_id)

    # -----------------------------------------------------------------------
    # Build output
    # -----------------------------------------------------------------------
    bus_schedules: List[BusSchedule] = []
    for bus in scenario.buses:
        events = bus_events[bus.id]
        last_loc = bus_last_loc[bus.id]
        dist_to_dest = bus_cum[bus.id][bus.destination] - bus_cum[bus.id][last_loc]
        arrival_min = int(bus_current_time[bus.id] + _travel_time_min(dist_to_dest, physics.speed_kmh))
        total_wait = sum(e.wait_min for e in events)

        bs = BusSchedule(
            bus_id=bus.id,
            operator=bus.operator,
            direction=bus.direction,
            departure_min=bus.departure_min,
            charge_events=events,
            arrival_min=arrival_min,
            total_wait_min=total_wait,
        )
        bus_schedules.append(bs)

    station_logs: Dict[str, StationLog] = {}
    for sid, squeue in sq.items():
        sorted_log = sorted(squeue.log, key=lambda x: x[1])
        station_logs[sid] = StationLog(station_id=sid, queue=sorted_log)

    return ScheduleResult(
        scenario_id=scenario.meta.id,
        bus_schedules=bus_schedules,
        station_logs=station_logs,
    )
