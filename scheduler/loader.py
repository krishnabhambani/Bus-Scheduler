"""
Scenario loader — translates JSON files into typed domain objects.
Adding new fields to the JSON is a data-only change; the loader
just reads whatever it finds in `world`, `weights`, and `buses`.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import List

from scheduler.models import (
    Bus, Physics, Route, Scenario, ScenarioMeta,
    Segment, Station, Weights,
)


def _parse_time(t: str) -> int:
    """'HH:MM' → minutes since midnight."""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def load_scenario(path: str | Path) -> Scenario:
    with open(path) as f:
        raw = json.load(f)

    meta = ScenarioMeta(**raw["meta"])

    world = raw["world"]

    # Route
    r = world["route"]
    segments = [Segment(s["from"], s["to"], s["distance_km"]) for s in r["segments"]]
    route = Route(
        id=r["id"],
        name=r["name"],
        waypoints=r["waypoints"],
        segments=segments,
    )

    # Stations — supports arbitrary number of chargers per station
    stations = [
        Station(
            id=s["id"],
            name=s["name"],
            chargers=s.get("chargers", 1),
            location=s["location"],
        )
        for s in world["stations"]
    ]

    # Physics
    p = world["physics"]
    physics = Physics(
        battery_range_km=p["battery_range_km"],
        charge_duration_min=p["charge_duration_min"],
        speed_kmh=p["speed_kmh"],
        charge_fills_to_full=p.get("charge_fills_to_full", True),
    )

    # Weights — each key is optional; falls back to 1.0
    w = raw.get("weights", {})
    weights = Weights(
        individual=w.get("individual", 1.0),
        operator=w.get("operator", 1.0),
        overall=w.get("overall", 1.0),
    )

    operators: List[str] = world.get("operators", [])

    # Buses
    buses = []
    for b in raw["buses"]:
        dep = b["departure"]
        buses.append(
            Bus(
                id=b["id"],
                operator=b["operator"],
                direction=b["direction"],
                departure=dep,
                departure_min=_parse_time(dep),
            )
        )

    return Scenario(
        meta=meta,
        route=route,
        stations=stations,
        physics=physics,
        weights=weights,
        operators=operators,
        buses=buses,
    )


def load_all_scenarios(directory: str | Path) -> dict[str, Scenario]:
    """Returns an ordered dict keyed by scenario name."""
    directory = Path(directory)
    scenarios = {}
    for path in sorted(directory.glob("scenario_*.json")):
        s = load_scenario(path)
        scenarios[s.meta.name] = s
    return scenarios
