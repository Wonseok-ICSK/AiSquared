"""Travel-time model, balanced day clustering, stop ordering and route metrics (DESIGN §10.3).

Pure Python and deterministic (seeded); no network.

**Travel-time model** (straight-line haversine distance ``d``, door to door):

* ``d <= 1.2 km`` → **walk**: road distance ``1.3 × d`` at 4.5 km/h (≈ 17.3 min/km, ≤ 21 min).
* otherwise → **transit**: 8 min access/wait overhead + ``1.35 × d`` at a 22 km/h urban
  average (≈ 3.7 min/km). Long legs taper to rapid/express services: +1.5 min/km for the part
  between 10 and 30 km (≈ 40 km/h effective) and +1.0 min/km beyond 30 km (≈ 60 km/h), so
  airport transfers stay realistic. Examples: Shinjuku → Asakusa (≈ 9 km) ≈ 42 min,
  Haneda → Shinjuku (≈ 17 km) ≈ 56 min, Narita → Shinjuku (≈ 63 km) ≈ 108 min. Calibrated
  against the Tokyo rail spot checks in ``docs/research/places-routing.md`` (which suggests
  ``12 + 2.5·d``; the contract's 8 min + 1.35 detour at 22 km/h gives similar urban times).

**Clustering** (:func:`balanced_clusters`): points are projected to local kilometres, centres
are seeded with k-means++ (seeded RNG, several restarts), then each round assigns points with a
capacity-constrained *regret* heuristic (the point that would lose most by not getting its best
cluster goes first), recomputes centres, and finishes with pairwise swap / move refinement.
Clusters can be *anchored* (fixed centre, e.g. the hotel for arrival/departure days) and an
optional per-(point, cluster) extra cost (in km-equivalents) expresses closures, weather or
time-of-day fit.

**Ordering** (:func:`order_stops`): exact branch-and-bound over permutations for ≤ 8 stops,
otherwise nearest neighbour + 2-opt / relocation, minimising travel minutes plus soft penalties
for waiting, opening-window violations, best-time-of-day deviation, meal-break delays and
running past the end of the day; fixed start/end points (hotel, airport).
"""

from __future__ import annotations

import math
import random
from statistics import median
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.core.airports import haversine_km
from app.schemas import ItineraryPlan, RouteMetrics, TravelHop

Coord = tuple[float, float]
"""``(lat, lon)`` in degrees."""

WALK_MAX_KM = 1.2
WALK_KMH = 4.5
WALK_DETOUR = 1.3
TRANSIT_OVERHEAD_MIN = 8.0
TRANSIT_DETOUR = 1.35
TRANSIT_KMH = 22.0
EXPRESS_FROM_KM = 10.0
EXPRESS_MIN_PER_KM = 1.5
REGIONAL_FROM_KM = 30.0
REGIONAL_MIN_PER_KM = 1.0

BRUTE_FORCE_MAX_STOPS = 8
BREAK_TAKE_EARLY_MIN = 30
"""A meal break may start up to this long before its ``earliest`` time when the next stop
would otherwise push it past ``latest``."""
BREAK_AFTER_LAST_GRACE_MIN = 60

# soft-constraint weights (cost unit = travel minutes)
W_WAIT = 0.2
W_LATE = 12.0
W_PREF = 0.15
W_OVERRUN = 6.0
W_BREAK_DELAY = 1.0
CLOSED_LATENESS_MIN = 600
MAX_PREF_DEVIATION_MIN = 480

HOTEL_AREA_KM = 1.5
BACKTRACK_MIN_RADIUS_KM = 1.5
BACKTRACK_MAX_RADIUS_KM = 3.0
RETURN_RADIUS_KM = 1.0
LEFT_AREA_KM = 3.0
CITY_HOP_EXCLUDED_KINDS = frozenset({"arrival", "departure", "transit"})
AREA_KINDS = frozenset({"activity", "meal"})

# --------------------------------------------------------------------------- travel model


def distance_km(a: Coord, b: Coord) -> float:
    """Great-circle distance between two ``(lat, lon)`` points in km."""
    return haversine_km(a[0], a[1], b[0], b[1])


def travel_minutes(distance: float) -> tuple[str, float]:
    """``(mode, minutes)`` for a straight-line distance in km (see module docstring)."""
    if distance <= WALK_MAX_KM:
        return "walk", distance * WALK_DETOUR / WALK_KMH * 60.0
    minutes = TRANSIT_OVERHEAD_MIN + min(distance, EXPRESS_FROM_KM) * TRANSIT_DETOUR / TRANSIT_KMH * 60.0
    if distance > EXPRESS_FROM_KM:
        minutes += (min(distance, REGIONAL_FROM_KM) - EXPRESS_FROM_KM) * EXPRESS_MIN_PER_KM
    if distance > REGIONAL_FROM_KM:
        minutes += (distance - REGIONAL_FROM_KM) * REGIONAL_MIN_PER_KM
    return "transit", minutes


def travel_estimate(a: Coord, b: Coord) -> TravelHop:
    """Estimated door-to-door hop between two points (whole minutes, km to 2 decimals)."""
    dist = distance_km(a, b)
    mode, minutes = travel_minutes(dist)
    return TravelHop(mode=mode, minutes=float(round(minutes)), distance_km=round(dist, 2))


def _project(points: Sequence[Coord], origin: Coord) -> list[tuple[float, float]]:
    """Equirectangular projection to local km around ``origin`` (x east, y north)."""
    lat0, lon0 = origin
    kx = 111.32 * math.cos(math.radians(lat0))
    ky = 110.57
    out: list[tuple[float, float]] = []
    for lat, lon in points:
        dlon = (lon - lon0 + 180.0) % 360.0 - 180.0
        out.append((dlon * kx, (lat - lat0) * ky))
    return out


def centroid(points: Sequence[Coord]) -> Coord | None:
    """Mean ``(lat, lon)`` of a few nearby points (``None`` when empty)."""
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


# --------------------------------------------------------------------------- capacities


def proportional_capacities(
    n: int, weights: Sequence[float], maxima: Sequence[int] | None = None
) -> list[int]:
    """Split ``n`` items over clusters proportionally to ``weights`` (largest remainder),
    never exceeding ``maxima``. Raises ``ValueError`` when the maxima cannot hold ``n``."""
    k = len(weights)
    limits = list(maxima) if maxima is not None else [n] * k
    if len(limits) != k:
        raise ValueError("weights and maxima differ in length")
    if sum(limits[i] for i in range(k) if weights[i] > 0) < n:
        raise ValueError("capacity maxima cannot hold all items")
    caps = [0] * k
    remaining = n
    while remaining > 0:
        active = [i for i in range(k) if weights[i] > 0 and caps[i] < limits[i]]
        total = sum(weights[i] for i in active)
        shares = {i: remaining * weights[i] / total for i in active}
        given = 0
        for i in active:
            add = min(int(shares[i]), limits[i] - caps[i])
            caps[i] += add
            given += add
        left = remaining - given
        for i in sorted(active, key=lambda j: (-(shares[j] - int(shares[j])), j)):
            if left <= 0:
                break
            if caps[i] < limits[i]:
                caps[i] += 1
                left -= 1
                given += 1
        remaining -= given
        if given == 0:  # pragma: no cover - guarded by the maxima check above
            break
    return caps


# --------------------------------------------------------------------------- clustering


def _euclid(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class _ClusterRun:
    labels: list[int]
    centers: list[tuple[float, float]]
    cost: float


def _seed_centers(
    xy: list[tuple[float, float]],
    anchors_xy: list[tuple[float, float] | None],
    rng: random.Random,
) -> list[tuple[float, float]]:
    """k-means++ seeding for unanchored clusters (anchored ones keep their anchor)."""
    centers: list[tuple[float, float] | None] = list(anchors_xy)
    chosen = [c for c in centers if c is not None]
    for j, center in enumerate(centers):
        if center is not None:
            continue
        if not chosen:
            pick = xy[rng.randrange(len(xy))]
        else:
            d2 = [min(_euclid(p, c) ** 2 for c in chosen) for p in xy]
            total = sum(d2)
            if total <= 0:
                pick = xy[rng.randrange(len(xy))]
            else:
                r = rng.random() * total
                acc = 0.0
                pick = xy[-1]
                for p, w in zip(xy, d2, strict=True):
                    acc += w
                    if acc >= r:
                        pick = p
                        break
        centers[j] = pick
        chosen.append(pick)
    return [c for c in centers if c is not None]


def _cost_matrix(
    xy: list[tuple[float, float]],
    centers: list[tuple[float, float]],
    extra: Sequence[Sequence[float]] | None,
) -> list[list[float]]:
    return [
        [_euclid(p, c) + (extra[i][j] if extra is not None else 0.0) for j, c in enumerate(centers)]
        for i, p in enumerate(xy)
    ]


def _regret_assign(cost: list[list[float]], capacities: Sequence[int]) -> list[int]:
    """Capacity-constrained greedy assignment, highest regret first."""
    n, k = len(cost), len(capacities)
    remaining = list(capacities)
    labels = [-1] * n
    unassigned = set(range(n))
    while unassigned:
        best: tuple[float, float, int, int] | None = None  # (-regret, best_cost, i, j)
        for i in sorted(unassigned):
            options = sorted((cost[i][j], j) for j in range(k) if remaining[j] > 0)
            regret = options[1][0] - options[0][0] if len(options) > 1 else math.inf
            key = (-regret, options[0][0], i, options[0][1])
            if best is None or key < best:
                best = key
        assert best is not None
        _, _, i, j = best
        labels[i] = j
        remaining[j] -= 1
        unassigned.discard(i)
    return labels


def _recenter(
    xy: list[tuple[float, float]],
    labels: list[int],
    centers: list[tuple[float, float]],
    anchored: list[bool],
) -> list[tuple[float, float]]:
    out = list(centers)
    for j in range(len(centers)):
        if anchored[j]:
            continue
        members = [xy[i] for i, lab in enumerate(labels) if lab == j]
        if members:
            out[j] = (sum(p[0] for p in members) / len(members), sum(p[1] for p in members) / len(members))
    return out


def _refine(
    xy: list[tuple[float, float]],
    labels: list[int],
    centers: list[tuple[float, float]],
    capacities: Sequence[int],
    anchored: list[bool],
    extra: Sequence[Sequence[float]] | None,
    passes: int = 4,
) -> tuple[list[int], list[tuple[float, float]]]:
    """Pairwise swaps and moves into spare capacity while they reduce the total cost."""
    labels = list(labels)
    n, k = len(labels), len(capacities)
    for _ in range(passes):
        cost = _cost_matrix(xy, centers, extra)
        improved = False
        for i in range(n):
            for j in range(i + 1, n):
                a, b = labels[i], labels[j]
                if a == b:
                    continue
                delta = cost[i][b] + cost[j][a] - cost[i][a] - cost[j][b]
                if delta < -1e-9:
                    labels[i], labels[j] = b, a
                    improved = True
        sizes = [labels.count(c) for c in range(k)]
        for i in range(n):
            a = labels[i]
            for c in range(k):
                if c != a and sizes[c] < capacities[c] and cost[i][c] - cost[i][a] < -1e-9:
                    sizes[a] -= 1
                    sizes[c] += 1
                    labels[i] = a = c
                    improved = True
        centers = _recenter(xy, labels, centers, anchored)
        if not improved:
            break
    return labels, centers


def _total_cost(cost: list[list[float]], labels: list[int]) -> float:
    return sum(cost[i][lab] for i, lab in enumerate(labels))


def balanced_clusters(
    points: Sequence[Coord],
    capacities: Sequence[int],
    *,
    anchors: Sequence[Coord | None] | None = None,
    extra_cost: Sequence[Sequence[float]] | None = None,
    seed: int = 0,
    restarts: int = 6,
    max_iter: int = 12,
) -> list[int]:
    """Assign each point to one of ``len(capacities)`` clusters (one cluster per day).

    Cluster ``j`` receives at most ``capacities[j]`` points. ``anchors[j]`` fixes cluster
    ``j``'s centre (e.g. the hotel on short arrival/departure days); ``extra_cost[i][j]`` adds a
    km-equivalent penalty for putting point ``i`` in cluster ``j``. Deterministic for a given
    input order and ``seed``. Returns the cluster label of every point.

    Raises ``ValueError`` when capacities cannot hold all points.
    """
    n, k = len(points), len(capacities)
    if n == 0:
        return []
    if k == 0 or sum(capacities) < n:
        raise ValueError("cluster capacities cannot hold all points")
    anchor_list = list(anchors) if anchors is not None else [None] * k
    if len(anchor_list) != k:
        raise ValueError("anchors and capacities differ in length")
    if extra_cost is not None and (len(extra_cost) != n or any(len(r) != k for r in extra_cost)):
        raise ValueError("extra_cost must be an n x k matrix")
    origin = centroid(list(points)) or points[0]
    xy = _project(points, origin)
    anchor_xy = [
        _project([a], origin)[0] if a is not None else None for a in anchor_list
    ]
    anchored = [a is not None for a in anchor_xy]

    best: _ClusterRun | None = None
    for restart in range(max(1, restarts)):
        rng = random.Random(seed * 1009 + restart)
        centers = _seed_centers(xy, anchor_xy, rng)
        labels: list[int] = []
        for _ in range(max_iter):
            new_labels = _regret_assign(_cost_matrix(xy, centers, extra_cost), capacities)
            centers = _recenter(xy, new_labels, centers, anchored)
            if new_labels == labels:
                break
            labels = new_labels
        labels, centers = _refine(xy, labels, centers, capacities, anchored, extra_cost)
        run = _ClusterRun(labels, centers, _total_cost(_cost_matrix(xy, centers, extra_cost), labels))
        if best is None or run.cost < best.cost - 1e-9:
            best = run
    assert best is not None
    return best.labels


# --------------------------------------------------------------------------- stop ordering


@dataclass(frozen=True)
class Stop:
    """A place to visit within one day.

    ``windows``: opening windows in minutes after midnight (``None`` = always open / unknown,
    ``()`` = closed that day). ``preferred``: soft best-time-of-day window.
    """

    key: str
    point: Coord
    duration: int
    windows: tuple[tuple[int, int], ...] | None = None
    preferred: tuple[int, int] | None = None


@dataclass(frozen=True)
class Break:
    """A meal pause taken wherever the traveller is once the clock reaches ``earliest``
    (``duration`` should include a short walk to a nearby restaurant)."""

    key: str
    earliest: int
    latest: int
    duration: int


@dataclass(frozen=True)
class Visit:
    """One scheduled stop or break (minutes after midnight)."""

    kind: Literal["stop", "break"]
    index: int
    """Index into the ``stops`` or ``breaks`` sequence."""
    start: int
    end: int
    travel: float
    """Minutes travelled to reach a stop (0 for breaks)."""
    wait: int
    late: int
    """Minutes past closing (stops) or past ``latest`` (breaks)."""


@dataclass(frozen=True)
class DaySchedule:
    """Result of :func:`order_stops` / :func:`schedule_stops`."""

    order: tuple[int, ...]
    visits: tuple[Visit, ...]
    finish: int
    """Minute the day ends (back at the end point, or after the last visit)."""
    travel_minutes: float
    lateness: int
    """Total minutes of opening-window violations."""
    overrun: int
    """Minutes past ``end_minute``."""
    cost: float
    skipped_breaks: tuple[int, ...] = ()


def fit_window(
    arrive: int, duration: int, windows: tuple[tuple[int, int], ...] | None
) -> tuple[int, int, int]:
    """``(start, wait, late)`` for a visit arriving at ``arrive`` given opening windows."""
    if windows is None:
        return arrive, 0, 0
    if not windows:
        return arrive, 0, CLOSED_LATENESS_MIN
    best: tuple[int, int, int] | None = None
    for open_at, close_at in windows:
        start = max(arrive, open_at)
        late = max(0, start + duration - close_at)
        key = (late, start - arrive, start)
        if best is None or key < best:
            best = key
    assert best is not None
    late, wait, start = best
    return start, wait, late


def _pref_deviation(start: int, end: int, preferred: tuple[int, int] | None) -> int:
    if preferred is None:
        return 0
    lo, hi = preferred
    return min(MAX_PREF_DEVIATION_MIN, max(0, lo - start) + max(0, end - hi))


@dataclass(frozen=True)
class _State:
    t: int
    pos: int
    pending: tuple[int, ...]
    cost: float
    travel: float
    late: int
    visits: tuple[Visit, ...]
    order: tuple[int, ...]


@dataclass
class _Problem:
    stops: Sequence[Stop]
    breaks: Sequence[Break]
    matrix: list[list[float]]  # node 0 = start, 1..n = stops, n+1 = end
    end_minute: int | None
    has_end: bool
    _memo: dict[tuple[int, ...], DaySchedule] = field(default_factory=dict)

    def _break_visit(self, state: _State, bi: int, start: int, wait_cost: bool) -> _State:
        brk = self.breaks[bi]
        delay = max(0, start - brk.latest)
        cost = W_BREAK_DELAY * delay + (W_WAIT * (start - state.t) if wait_cost else 0.0)
        visit = Visit("break", bi, start, start + brk.duration, 0.0, start - state.t, delay)
        return _State(
            start + brk.duration, state.pos, tuple(b for b in state.pending if b != bi),
            state.cost + cost, state.travel, state.late, state.visits + (visit,), state.order,
        )

    def step(self, state: _State, k: int) -> _State:
        """Visit stop ``k`` next (taking any due meal break first)."""
        stop = self.stops[k]
        for bi in state.pending:
            brk = self.breaks[bi]
            hop = self.matrix[state.pos][k + 1]
            start, _, _ = fit_window(state.t + int(round(hop)), stop.duration, stop.windows)
            if state.t >= brk.earliest or (
                state.t >= brk.earliest - BREAK_TAKE_EARLY_MIN
                and start + stop.duration > brk.latest + BREAK_TAKE_EARLY_MIN
            ):
                state = self._break_visit(state, bi, max(state.t, brk.earliest - BREAK_TAKE_EARLY_MIN), True)
        hop = self.matrix[state.pos][k + 1]
        arrive = state.t + int(round(hop))
        start, wait, late = fit_window(arrive, stop.duration, stop.windows)
        end = start + stop.duration
        cost = hop + W_WAIT * wait + W_LATE * late + W_PREF * _pref_deviation(start, end, stop.preferred)
        visit = Visit("stop", k, start, end, hop, wait, late)
        return _State(
            end, k + 1, state.pending, state.cost + cost, state.travel + hop, state.late + late,
            state.visits + (visit,), state.order + (k,),
        )

    def finish(self, state: _State) -> DaySchedule:
        """Take remaining breaks if still sensible, travel to the end point, total the cost."""
        skipped: list[int] = []
        for bi in state.pending:
            brk = self.breaks[bi]
            start = max(state.t, brk.earliest)
            fits_day = self.end_minute is None or start + brk.duration <= self.end_minute
            if state.t <= brk.latest + BREAK_AFTER_LAST_GRACE_MIN and fits_day:
                state = self._break_visit(state, bi, start, False)
            else:
                skipped.append(bi)
                state = _State(
                    state.t, state.pos, tuple(b for b in state.pending if b != bi), state.cost,
                    state.travel, state.late, state.visits, state.order,
                )
        t, cost, travel = state.t, state.cost, state.travel
        if self.has_end:
            hop = self.matrix[state.pos][len(self.stops) + 1]
            t += int(round(hop))
            cost += hop
            travel += hop
        overrun = max(0, t - self.end_minute) if self.end_minute is not None else 0
        cost += W_OVERRUN * overrun
        return DaySchedule(
            order=state.order, visits=state.visits, finish=t, travel_minutes=travel,
            lateness=state.late, overrun=overrun, cost=cost, skipped_breaks=tuple(skipped),
        )

    def evaluate(self, order: Sequence[int], start_state: _State) -> DaySchedule:
        key = tuple(order)
        cached = self._memo.get(key)
        if cached is None:
            state = start_state
            for k in order:
                state = self.step(state, k)
            cached = self._memo[key] = self.finish(state)
        return cached


def _matrix(start: Coord, stops: Sequence[Stop], end: Coord | None) -> list[list[float]]:
    nodes = [start, *(s.point for s in stops), end if end is not None else start]
    return [[travel_minutes(distance_km(a, b))[1] for b in nodes] for a in nodes]


def _initial_state(start_minute: int, breaks: Sequence[Break]) -> _State:
    """Start state; pending breaks are kept in chronological order (indices = caller's list)."""
    pending = tuple(sorted(range(len(breaks)), key=lambda i: (breaks[i].earliest, i)))
    return _State(start_minute, 0, pending, 0.0, 0.0, 0, (), ())


def _branch_and_bound(problem: _Problem, start: _State, n: int) -> DaySchedule:
    best: list[DaySchedule | None] = [None]

    def dfs(state: _State, remaining: tuple[int, ...]) -> None:
        current = best[0]
        if current is not None and state.cost >= current.cost:
            return
        if not remaining:
            schedule = problem.finish(state)
            if current is None or schedule.cost < current.cost - 1e-9:
                best[0] = schedule
            return
        for k in remaining:
            dfs(problem.step(state, k), tuple(r for r in remaining if r != k))

    dfs(start, tuple(range(n)))
    assert best[0] is not None
    return best[0]


def _nearest_neighbour(problem: _Problem, start: _State, n: int) -> list[int]:
    state, remaining, order = start, list(range(n)), []
    while remaining:
        k = min(remaining, key=lambda r: (problem.step(state, r).cost, r))
        state = problem.step(state, k)
        order.append(k)
        remaining.remove(k)
    return order


def _improve(problem: _Problem, start: _State, order: list[int], max_rounds: int = 40) -> list[int]:
    """2-opt reversals and single-stop relocations, first improvement, until no gain."""
    best = problem.evaluate(order, start)
    n = len(order)
    for _ in range(max_rounds):
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                candidate = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                schedule = problem.evaluate(candidate, start)
                if schedule.cost < best.cost - 1e-9:
                    order, best, improved = candidate, schedule, True
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                candidate = order[:i] + order[i + 1 :]
                candidate.insert(j, order[i])
                schedule = problem.evaluate(candidate, start)
                if schedule.cost < best.cost - 1e-9:
                    order, best, improved = candidate, schedule, True
        if not improved:
            break
    return order


def schedule_stops(
    stops: Sequence[Stop],
    order: Sequence[int],
    start: Coord,
    end: Coord | None = None,
    *,
    start_minute: int,
    end_minute: int | None = None,
    breaks: Sequence[Break] = (),
) -> DaySchedule:
    """Simulate a fixed visiting ``order`` (times, waits, breaks, lateness, cost)."""
    problem = _Problem(stops, list(breaks), _matrix(start, stops, end), end_minute, end is not None)
    return problem.evaluate(list(order), _initial_state(start_minute, breaks))


def order_stops(
    stops: Sequence[Stop],
    start: Coord,
    end: Coord | None = None,
    *,
    start_minute: int,
    end_minute: int | None = None,
    breaks: Sequence[Break] = (),
) -> DaySchedule:
    """Best visiting order for one day from ``start`` (e.g. hotel) to ``end`` (hotel/airport).

    Exact branch-and-bound for ≤ :data:`BRUTE_FORCE_MAX_STOPS` stops, else nearest neighbour +
    2-opt/relocation. Opening windows, best-time preferences, meal breaks and ``end_minute``
    are soft constraints in the cost (see module docstring). Deterministic.
    """
    problem = _Problem(stops, list(breaks), _matrix(start, stops, end), end_minute, end is not None)
    initial = _initial_state(start_minute, breaks)
    n = len(stops)
    if n <= BRUTE_FORCE_MAX_STOPS:
        return _branch_and_bound(problem, initial, n)
    order = _improve(problem, initial, _nearest_neighbour(problem, initial, n))
    return problem.evaluate(order, initial)


# --------------------------------------------------------------------------- route metrics


def _item_point(item: object) -> Coord | None:
    lat, lng = getattr(item, "lat", None), getattr(item, "lng", None)
    return (lat, lng) if lat is not None and lng is not None else None


def _day_stop_points(plan: ItineraryPlan) -> list[list[Coord]]:
    return [
        [p for item in day.items if item.kind in AREA_KINDS and (p := _item_point(item)) is not None]
        for day in plan.days
    ]


def backtrack_hops(plan: ItineraryPlan) -> int:
    """Hops that return to an area already covered and left.

    Counts (a) stops lying inside an *earlier day's* area (within its radius = median stop
    distance to the day's centre clamped to [:data:`BACKTRACK_MIN_RADIUS_KM`,
    :data:`BACKTRACK_MAX_RADIUS_KM`], and closer to that day's centre than to the own day's) and
    (b) within a day, stops within :data:`RETURN_RADIUS_KM` of an earlier stop after having
    been more than :data:`LEFT_AREA_KM` away in between (A → B → A). Only activity and meal
    items with coordinates are considered (hotel/airport legs are anchors, not areas), and
    stops within :data:`HOTEL_AREA_KM` of the plan's ``hotel`` (extra field, when present) are
    ignored in both checks — the hotel area is naturally revisited every day.
    """
    per_day = _day_stop_points(plan)
    hotel_info = getattr(plan, "hotel", None)
    hotel: Coord | None = None
    if isinstance(hotel_info, dict) and hotel_info.get("lat") is not None and hotel_info.get("lng") is not None:
        hotel = (float(hotel_info["lat"]), float(hotel_info["lng"]))
    centres = [centroid(points) for points in per_day]
    radii = [
        min(BACKTRACK_MAX_RADIUS_KM, max(BACKTRACK_MIN_RADIUS_KM, median(distance_km(p, c) for p in pts)))
        if c is not None else 0.0
        for pts, c in zip(per_day, centres, strict=True)
    ]
    count = 0
    for d, points in enumerate(per_day):
        own = centres[d]
        for j, point in enumerate(points):
            own_dist = distance_km(point, own) if own is not None else math.inf
            if hotel is not None and distance_km(point, hotel) <= HOTEL_AREA_KM:
                continue
            earlier_area = any(
                centres[e] is not None
                and (dist := distance_km(point, centres[e])) <= radii[e]  # type: ignore[arg-type]
                and dist < own_dist
                for e in range(d)
            )
            returned = any(
                distance_km(point, points[i]) <= RETURN_RADIUS_KM
                and (hotel is None or distance_km(points[i], hotel) > HOTEL_AREA_KM)
                and any(distance_km(points[m], points[i]) > LEFT_AREA_KM for m in range(i + 1, j))
                for i in range(j - 1)
            )
            if earlier_area or returned:
                count += 1
    return count


def route_metrics(plan: ItineraryPlan) -> RouteMetrics:
    """Route quality of a plan: total travel minutes (all hops incl. airport transfers),
    average / maximum in-city hop distance (airport transfers excluded) and backtrack hops."""
    total = 0.0
    city_hops: list[float] = []
    for day in plan.days:
        for item in day.items:
            hop = item.travel_from_prev
            if hop is None:
                continue
            total += hop.minutes
            if item.kind not in CITY_HOP_EXCLUDED_KINDS:
                city_hops.append(hop.distance_km)
    return RouteMetrics(
        total_travel_minutes=round(total, 1),
        avg_hop_km=round(sum(city_hops) / len(city_hops), 2) if city_hops else 0.0,
        max_hop_km=round(max(city_hops), 2) if city_hops else 0.0,
        backtrack_hops=backtrack_hops(plan),
    )
