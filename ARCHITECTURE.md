# Architecture

## Scheduler Approach: Event-Driven Greedy Simulation with Weighted Cost Rules

### Why this approach?

The problem is a **constrained resource scheduling problem**: buses are jobs, chargers are machines, range constraints are deadlines, and we have multiple competing objectives. Classic approaches:

| Approach | Why rejected |
|----------|-------------|
| ILP / MIP | Correct and optimal, but rewriting the constraint matrix for every new rule is painful; solver setup adds heavyweight dependencies; hard to explain to non-technical ops staff |
| Genetic / simulated annealing | Slow convergence on large instances; not deterministic; weights are baked into fitness function in opaque ways |
| Pure FIFO queue | Ignores all soft objectives; no weight tunability at all |
| **Event-Driven Greedy + Pluggable Rules** | Fast, deterministic, explainable, trivially extensible — rules are just functions |

The chosen approach:

1. **Phase 1 — Stop selection:** For each bus, compute which charging stations it must use. Uses a greedy "drive as far as possible" algorithm — charge at the *latest* feasible station before running out. This minimises total stops while guaranteeing the hard range constraint.

2. **Phase 2 — Contention resolution:** A global event loop processes arrivals in chronological order. When buses arrive at the same station within the same tick, they are scored using a weighted sum of soft-rule functions. Lowest score charges first.

3. **Propagation:** A bus's wait at one station delays its arrival at the next. Waits cascade forward correctly because each bus's departure time is updated before enqueuing its next event.

**Scalability:** O(B × S × log B) where B = buses, S = stations. Tested with 20 buses; handles hundreds with no architectural change.

---

## Data Structure Design

Each scenario is a self-contained JSON file. The schema is intentionally over-specified relative to today's requirements — every field that *will* be needed is already present.

```
scenario_N.json
├── meta           — scenario identity and version
├── world
│   ├── route      — waypoints + segments with distances
│   ├── stations   — list of stations with charger count and location
│   ├── physics    — battery range, speed, charge time
│   └── operators  — list of operator IDs
├── weights        — one float per soft rule
└── buses          — list of buses with operator, direction, departure
```

Key design decisions:

- **`chargers: int` on each station** — not hardcoded to 1. Adding a second charger anywhere is a data change.
- **`segments` as a list** — not derived from waypoints. Supports non-linear routes, partial routes, or bidirectional segments with different distances.
- **`waypoints` as an ordered list** — direction is computed by comparing `origin` index vs `destination` index. Works for any number of waypoints.
- **`weights` as named floats** — not an array. Adding a new weight is adding one key to the JSON; the engine reads it via `getattr`.
- **`direction` as a string code** — `"BK"` or `"KB"` today, but the model supports any pair of endpoint names. Bus origin/destination are derived from the route, not hardcoded.

---

## Future Changes I Designed For

These are the specific changes I anticipated and how the data structure handles each — **without code changes to the engine**.

### 1. More chargers at a station
**Change:** Set `"chargers": 2` (or any N) in the station entry.  
**Engine handles it:** `StationQueue` uses a min-heap of N slots. Adding chargers = increasing N. Zero engine changes.

### 2. New stations added to the route
**Change:** Add a waypoint to `waypoints`, add a segment to `segments`, add a station entry.  
**Engine handles it:** Stop-selection and simulation are driven entirely by the route data. The engine never mentions station names.

### 3. Segment distances change
**Change:** Update `distance_km` in the relevant segment.  
**Engine handles it:** All travel times and range calculations are derived from segment distances at runtime.

### 4. New operator added
**Change:** Add operator name to `"operators"` list and assign it to buses.  
**Engine handles it:** Operator scoring uses `defaultdict` — unknown operators get 0 accumulated wait (neutral, fair). No registry to update.

### 5. Speed changes or varies by segment
**Change:** Move `speed_kmh` from `physics` to individual `segments` (already designed with segments as objects).  
**Minimal change:** Add `speed_kmh` to `Segment` dataclass and update `_travel_time_min`. One function change, no engine restructuring.

### 6. Multiple routes sharing stations
**Change:** Add a second route object; buses reference a `route_id`.  
**Engine handles it:** Each scenario already references a single `route` object. Multi-route scenarios = multiple `route` entries + bus gets a `route_id` field. The engine's per-bus cum-distance computation uses `bus.origin`/`bus.destination` + the bus's route — already parameterised.

### 7. New weight added (e.g. `priority`)
**Change:** Add one field to `Weights` dataclass, one entry in `RULE_REGISTRY`, one key in scenario JSON.  
**Engine handles it:** `_score()` iterates `RULE_REGISTRY` dynamically via `getattr(weights, weight_attr)`.

### 8. Priority bus class
**Change:** Add `"priority": true` to a bus entry in JSON, define `rule_priority`, register it.  
**Engine handles it:** Rule registry pattern. Bus dataclass already accepts arbitrary extra fields via the loader's `**kwargs`-style approach.

### 9. Time-of-day electricity costs
**Change:** Add a `tariff_schedule` list to the station entry (e.g. `[{start: "22:00", end: "06:00", cost_multiplier: 0.5}]`).  
**New rule:** `rule_cheapest_slot(bus, ctx) -> float` scores buses by expected charging cost at their predicted arrival time.  
**Engine handles it:** Rule registry. Tariff data flows through `ctx`.

### 10. Driver shift constraints
**Change:** Add a `shift_end` field to each bus.  
**New hard rule:** After stop selection, validate that `arrival_min <= shift_end_min`; if not, prefer earlier charging stations (shorter total trip time).  
**Engine handles it:** Hard rules are expressed as constraints on stop selection (Phase 1), not on the contention resolver (Phase 2).

### 11. Charging to a partial level (not always full)
**Change:** Add `charge_target_pct` to physics or per-bus.  
**Engine handles it:** `ChargeEvent.range_on_arrival_km` + `physics.battery_range_km * charge_target_pct` determines post-charge range. The range check in stop selection uses this. One arithmetic change in `_choose_charging_stops`.

### 12. Growing bus fleet (hundreds of buses)
**Change:** Add more bus entries to JSON.  
**Engine handles it:** Event loop is O(B × S × log B). 200 buses × 4 stations is trivial. No rewrite needed.

### 13. New scenario without code deployment
**Change:** Drop a new `scenario_N.json` file.  
**Engine handles it:** `load_all_scenarios` globs `scenario_*.json`. New file = new dropdown entry. App restarts pick it up automatically.

### 14. Bidirectional simultaneous station sharing (buses from both directions competing at same station)
**Engine already handles this:** Stations are direction-agnostic. `bus-BK-01` and `bus-KB-01` can both want Station B at the same time — the contention resolver scores them both and picks one. The queue has no concept of direction.

### 15. Real-time re-scheduling (a bus breaks down, free up its slot)
**Change:** Expose `schedule_scenario` as a function that accepts a mutable state dict (partially-executed schedule).  
**Engine handles it:** The simulation state is already isolated in local dicts per `schedule_scenario` call. Re-running with updated departure times is a fresh call.

---

## Assumptions Made

1. **All buses depart full.** The spec says endpoints have slow chargers. We model this as 240km range at departure, no departure delay.

2. **Speed is uniform across all segments and buses.** 60 km/h used throughout. This is configurable per scenario via `physics.speed_kmh`.

3. **Greedy stop selection (latest feasible charge) is correct for Phase 1.** An optimal stop set (minimising total wait) would require solving contention first — circular dependency. The greedy choice (B+D for BK buses, C+A for KB buses) is provably optimal for range alone and gives the scheduler the most flexibility in Phase 2.

4. **Contention window = 1 minute.** Buses arriving within 1 minute of each other at the same station are considered simultaneous and scored before ordering. This is a configurable constant.

5. **All times are in integer minutes.** Sub-minute precision is not meaningful for operational scheduling at this scale.

6. **No pre-emption.** A bus that starts charging always finishes. Interrupting a charge is not modelled.

7. **Charging always fills to full.** The spec states this. `physics.charge_fills_to_full = true` is a reminder field for when partial charging is added.

8. **Operator weight interpretation:** Higher operator weight → more emphasis on balancing average wait across operators (not on favouring one). If you want to *favour* a specific operator, define a new `rule_operator_priority` that returns 0 for the favoured operator and 1 for others.

---

## How the Weights Work (with an example)

**Scenario 4 has `operator = 2.0`** (KPN dominates BK fleet).

With balanced weights (1/1/1), buses charge in strict arrival order regardless of operator. KPN buses arrive in a dense cluster at Station B and queue up sequentially.

With `operator = 2.0`, the cost of operator imbalance is penalised more heavily. When two buses arrive simultaneously at a station — one KPN (whose fleet is already running late) and one from another operator — the scoring system sees:

```
score(kpn_bus)   = 1.0 * individual_wait + 2.0 * avg_kpn_wait  + 1.0 * finish_estimate
score(other_bus) = 1.0 * individual_wait + 2.0 * avg_other_wait + 1.0 * finish_estimate
```

Since KPN's fleet has accumulated more total wait (8 buses queuing), `avg_kpn_wait` is higher → KPN score is higher → other operator charges first, balancing fleet-level fairness. This is the intended "operator weight up → operators get balanced" behaviour.

---

## Code Example: Adding a New Rule

```python
# 1. In scheduler/engine.py — define the function
def rule_peak_hours(bus: Bus, ctx: RuleContext) -> float:
    """
    De-prioritise buses charging during peak hours (07:00–10:00).
    Encourages off-peak charging to reduce grid load.
    """
    # ctx carries anything we inject — here, current station arrival time
    arrive = ctx["current_arrive_min"].get(bus.id, 0)
    hour = (arrive // 60) % 24
    return 1.0 if 7 <= hour <= 10 else 0.0

# 2. Register it — one line
RULE_REGISTRY["peak_hours"] = (rule_peak_hours, "peak_hours")

# 3. In scheduler/models.py — add the weight field
@dataclass
class Weights:
    individual: float = 1.0
    operator:   float = 1.0
    overall:    float = 1.0
    peak_hours: float = 0.0  # off by default; set to 1.5 to activate

# 4. In scenario JSON — optionally override the default
{ "weights": { "peak_hours": 1.5 } }
```

The engine loop is not touched.
