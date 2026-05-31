"""
Data models for the Bus Charging Scheduler.
All domain objects are plain dataclasses — no business logic here.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# World / Route models
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    from_stop: str
    to_stop: str
    distance_km: float


@dataclass
class Route:
    id: str
    name: str
    waypoints: List[str]
    segments: List[Segment]

    def waypoint_index(self, name: str) -> int:
        return self.waypoints.index(name)

    def distance_between(self, a: str, b: str) -> float:
        """Total km from waypoint a to waypoint b (must be in order)."""
        idx_a = self.waypoint_index(a)
        idx_b = self.waypoint_index(b)
        if idx_b <= idx_a:
            raise ValueError(f"{b} must come after {a} on the route")
        total = 0.0
        for seg in self.segments[idx_a:idx_b]:
            total += seg.distance_km
        return total

    def stops_between(self, origin: str, destination: str) -> List[str]:
        """All waypoints strictly between origin and destination."""
        i = self.waypoint_index(origin)
        j = self.waypoint_index(destination)
        return self.waypoints[i + 1: j]


@dataclass
class Station:
    id: str
    name: str
    chargers: int          # number of parallel charger slots
    location: str          # waypoint name on the route


@dataclass
class Physics:
    battery_range_km: float
    charge_duration_min: float
    speed_kmh: float
    charge_fills_to_full: bool = True


# ---------------------------------------------------------------------------
# Weights — the one obvious place to change them
# ---------------------------------------------------------------------------

@dataclass
class Weights:
    individual: float = 1.0   # penalise long waits for a single bus
    operator: float = 1.0     # penalise long average waits within an operator's fleet
    overall: float = 1.0      # penalise total network time


# ---------------------------------------------------------------------------
# Bus / Scenario models
# ---------------------------------------------------------------------------

@dataclass
class Bus:
    id: str
    operator: str
    direction: str           # "BK" (Bengaluru→Kochi) or "KB" (Kochi→Bengaluru)
    departure: str           # "HH:MM"
    departure_min: int = 0   # minutes since midnight, computed on load

    @property
    def origin(self) -> str:
        return "Bengaluru" if self.direction == "BK" else "Kochi"

    @property
    def destination(self) -> str:
        return "Kochi" if self.direction == "BK" else "Bengaluru"


@dataclass
class ScenarioMeta:
    id: str
    name: str
    description: str
    version: str = "1.0"


@dataclass
class Scenario:
    meta: ScenarioMeta
    route: Route
    stations: List[Station]
    physics: Physics
    weights: Weights
    operators: List[str]
    buses: List[Bus]

    def station_by_id(self, sid: str) -> Optional[Station]:
        for s in self.stations:
            if s.id == sid:
                return s
        return None

    def stations_in_direction(self, direction: str) -> List[Station]:
        """Stations in the order a bus travelling in `direction` visits them."""
        route_order = self.route.waypoints
        relevant = [s for s in self.stations if s.location in route_order]
        if direction == "BK":
            return sorted(relevant, key=lambda s: route_order.index(s.location))
        else:  # KB
            return sorted(relevant, key=lambda s: route_order.index(s.location), reverse=True)


# ---------------------------------------------------------------------------
# Scheduler output models
# ---------------------------------------------------------------------------

@dataclass
class ChargeEvent:
    station_id: str
    arrive_min: int          # when the bus physically arrives at the station
    wait_min: int            # minutes spent waiting for the charger
    charge_start_min: int    # = arrive_min + wait_min
    charge_end_min: int      # = charge_start_min + charge_duration
    range_on_arrival_km: float  # remaining range when bus pulls in


@dataclass
class BusSchedule:
    bus_id: str
    operator: str
    direction: str
    departure_min: int
    charge_events: List[ChargeEvent]
    arrival_min: int         # at final destination
    total_wait_min: int
    valid: bool = True
    violation: str = ""      # description if invalid


@dataclass
class StationLog:
    station_id: str
    queue: List[Tuple[str, int, int]]  # (bus_id, charge_start_min, charge_end_min)


@dataclass
class ScheduleResult:
    scenario_id: str
    bus_schedules: List[BusSchedule]
    station_logs: Dict[str, StationLog]

    def get_bus(self, bus_id: str) -> Optional[BusSchedule]:
        for bs in self.bus_schedules:
            if bs.bus_id == bus_id:
                return bs
        return None
