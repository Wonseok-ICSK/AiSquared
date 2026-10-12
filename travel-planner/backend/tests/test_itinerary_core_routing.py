"""Travel-time model, balanced clustering, stop ordering and route metrics."""

from __future__ import annotations

import itertools
import json
import random
from datetime import date
from pathlib import Path

import pytest

from app.schemas import ItineraryPlan, PlanDay, PlanItem, TravelHop
from app.services.itinerary import routing
from app.services.itinerary.routing import Break, Stop

FIXTURE = Path(__file__).parent / "fixtures" / "itinerary" / "tokyo_candidates.json"
SHINJUKU = (35.6905, 139.7000)
ASAKUSA = (35.71476, 139.79666)
NARITA = (35.7720, 140.3929)
HANEDA = (35.5494, 139.7798)


def tokyo_points() -> list[tuple[str, tuple[float, float]]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [(row["name"], (row["lat"], row["lon"])) for row in data["pois"]]


# --------------------------------------------------------------------------- travel model


def test_walk_for_short_hops() -> None:
    hop = routing.travel_estimate(SHINJUKU, (35.6930, 139.6995))
    assert hop.mode == "walk"
    assert hop.distance_km == pytest.approx(0.28, abs=0.03)
    assert hop.minutes == round(hop.distance_km * 1.3 / 4.5 * 60)
    assert routing.travel_estimate(SHINJUKU, SHINJUKU) == TravelHop(mode="walk", minutes=0, distance_km=0)


def test_transit_model_matches_documented_examples() -> None:
    mode, minutes = routing.travel_minutes(1.21)
    assert mode == "transit" and minutes == pytest.approx(8 + 1.21 * 1.35 / 22 * 60)
    assert routing.travel_minutes(1.2)[0] == "walk"
    across = routing.travel_estimate(SHINJUKU, ASAKUSA)
    assert across.mode == "transit" and 38 <= across.minutes <= 46
    narita = routing.travel_estimate(NARITA, SHINJUKU)
    assert 95 <= narita.minutes <= 120
    haneda = routing.travel_estimate(HANEDA, SHINJUKU)
    assert 45 <= haneda.minutes <= 65


def test_transit_minutes_increase_with_distance() -> None:
    values = [routing.travel_minutes(d)[1] for d in (1.3, 3, 9.9, 10.1, 20, 29.9, 30.1, 60, 120)]
    assert values == sorted(values)


# --------------------------------------------------------------------------- capacities & clustering


def test_proportional_capacities() -> None:
    assert routing.proportional_capacities(11, [105, 595, 595, 272], [4, 4, 4, 4]) == [1, 4, 4, 2]
    assert sum(routing.proportional_capacities(7, [1, 2, 3])) == 7
    assert routing.proportional_capacities(3, [1, 1, 0]) == [2, 1, 0]
    assert routing.proportional_capacities(5, [1, 9], [3, 3]) == [2, 3]
    with pytest.raises(ValueError):
        routing.proportional_capacities(10, [1, 1], [3, 3])


def test_balanced_clusters_respects_capacities_and_is_deterministic() -> None:
    points = [p for _, p in tokyo_points()]
    capacities = [7, 7, 6, 6]
    labels = routing.balanced_clusters(points, capacities, seed=3)
    assert len(labels) == len(points)
    for cluster, cap in enumerate(capacities):
        assert labels.count(cluster) <= cap
    assert routing.balanced_clusters(points, capacities, seed=3) == labels


def test_balanced_clusters_groups_neighbourhoods() -> None:
    named = dict(tokyo_points())
    names = list(named)
    labels = routing.balanced_clusters([named[n] for n in names], [7, 7, 6, 6])
    label = dict(zip(names, labels, strict=True))
    assert label["Senso-ji"] == label["Nakamise Shopping Street"] == label["Tokyo Skytree"]
    assert label["Shibuya Sky"] == label["Shibuya Scramble Crossing"]
    assert label["Tokyo National Museum"] == label["Ueno Park"]
    assert label["Shibuya Sky"] != label["Senso-ji"]


def test_anchor_and_extra_cost() -> None:
    points = [SHINJUKU, (35.6930, 139.6995), ASAKUSA, (35.7101, 139.8107)]
    labels = routing.balanced_clusters(points, [2, 2], anchors=[ASAKUSA, None])
    assert labels[2] == labels[3] == 0
    forbidden = [[0.0, 0.0], [0.0, 0.0], [0.0, 50.0], [0.0, 0.0]]
    labels = routing.balanced_clusters(points, [3, 3], extra_cost=forbidden, seed=1)
    assert labels[2] == 0
    with pytest.raises(ValueError):
        routing.balanced_clusters(points, [1, 1])


def _route_minutes(points: list[tuple[float, float]]) -> float:
    stops = [Stop(key=str(i), point=p, duration=60) for i, p in enumerate(points)]
    schedule = routing.order_stops(stops, SHINJUKU, SHINJUKU, start_minute=9 * 60)
    return schedule.travel_minutes


def _total_minutes(points: list[tuple[float, float]], labels: list[int], k: int) -> float:
    return sum(_route_minutes([p for p, lab in zip(points, labels, strict=True) if lab == c]) for c in range(k))


def test_clustering_beats_random_assignment() -> None:
    points = [p for _, p in tokyo_points()][:24]
    capacities = [6, 6, 6, 6]
    clustered = _total_minutes(points, routing.balanced_clusters(points, capacities), 4)
    rng = random.Random(42)
    randoms = []
    for _ in range(12):
        labels = [c for c, cap in enumerate(capacities) for _ in range(cap)]
        rng.shuffle(labels)
        randoms.append(_total_minutes(points, labels, 4))
    assert clustered < min(randoms)
    assert clustered < 0.9 * (sum(randoms) / len(randoms))


# --------------------------------------------------------------------------- ordering


def test_brute_force_finds_the_optimal_order() -> None:
    pts = [p for _, p in tokyo_points()][:7]
    stops = [Stop(key=str(i), point=p, duration=45) for i, p in enumerate(pts)]
    best = routing.order_stops(stops, SHINJUKU, SHINJUKU, start_minute=540)
    exhaustive = min(
        routing.schedule_stops(stops, perm, SHINJUKU, SHINJUKU, start_minute=540).cost
        for perm in itertools.permutations(range(len(stops)))
    )
    assert best.cost == pytest.approx(exhaustive)
    assert sorted(best.order) == list(range(7))


def test_opening_windows_and_best_time_are_respected() -> None:
    stops = [
        Stop("evening-view", (35.6586, 139.7454), 60, windows=((540, 1350),), preferred=(1020, 1260)),
        Stop("morning-market", (35.6654, 139.7707), 60, windows=((360, 840),), preferred=(480, 720)),
        Stop("museum", (35.7188, 139.7765), 90, windows=((570, 1020),)),
    ]
    schedule = routing.order_stops(stops, SHINJUKU, SHINJUKU, start_minute=540, end_minute=1290)
    keys = [stops[v.index].key for v in schedule.visits if v.kind == "stop"]
    assert keys[-1] == "evening-view"
    market = next(v for v in schedule.visits if v.kind == "stop" and stops[v.index].key == "morning-market")
    assert market.end <= 780  # finished soon after its preferred morning window (until 12:00)
    assert schedule.lateness == 0 and schedule.overrun == 0
    for visit in schedule.visits:
        opening, closing = stops[visit.index].windows[0]  # type: ignore[index]
        assert opening <= visit.start and visit.end <= closing


def test_waits_for_opening_and_flags_closed() -> None:
    late_opening = Stop("bar", (35.6939, 139.7046), 60, windows=((1140, 1560),))
    schedule = routing.order_stops([late_opening], SHINJUKU, None, start_minute=600)
    assert schedule.visits[0].start == 1140 and schedule.visits[0].wait > 0
    closed = Stop("closed", (35.6939, 139.7046), 60, windows=())
    assert routing.order_stops([closed], SHINJUKU, None, start_minute=600).lateness > 0


def test_meal_breaks_are_inserted() -> None:
    pts = [p for _, p in tokyo_points()][:4]
    stops = [Stop(str(i), p, 90) for i, p in enumerate(pts)]
    breaks = [Break("lunch", 690, 810, 75), Break("dinner", 1080, 1200, 90)]
    schedule = routing.order_stops(stops, SHINJUKU, SHINJUKU, start_minute=540, end_minute=1290, breaks=breaks)
    taken = [(breaks[v.index].key, v.start) for v in schedule.visits if v.kind == "break"]
    assert [k for k, _ in taken] == ["lunch", "dinner"]
    assert 660 <= taken[0][1] <= 840 and 1080 <= taken[1][1] <= 1230
    assert schedule.skipped_breaks == ()


def test_many_stops_use_heuristic_and_beat_nearest_neighbour_order() -> None:
    pts = [p for _, p in tokyo_points()][:12]
    stops = [Stop(str(i), p, 30) for i, p in enumerate(pts)]
    schedule = routing.order_stops(stops, SHINJUKU, SHINJUKU, start_minute=480)
    assert sorted(schedule.order) == list(range(12))
    as_listed = routing.schedule_stops(stops, list(range(12)), SHINJUKU, SHINJUKU, start_minute=480)
    assert schedule.cost <= as_listed.cost
    assert routing.order_stops(stops, SHINJUKU, SHINJUKU, start_minute=480) == schedule


def test_overrun_is_reported() -> None:
    stops = [Stop(str(i), p, 180) for i, (_, p) in enumerate(tokyo_points()[:4])]
    schedule = routing.order_stops(stops, SHINJUKU, SHINJUKU, start_minute=540, end_minute=900)
    assert schedule.overrun > 0


# --------------------------------------------------------------------------- metrics


def _plan(days: list[list[tuple[str, tuple[float, float]]]], hotel: tuple[float, float] | None = None) -> ItineraryPlan:
    plan_days = []
    for offset, stops in enumerate(days):
        items = []
        previous = None
        for name, point in stops:
            hop = routing.travel_estimate(previous, point) if previous else None
            items.append(PlanItem(start="10:00", end="11:00", kind="activity", title=name,
                                  lat=point[0], lng=point[1], travel_from_prev=hop))
            previous = point
        plan_days.append(PlanDay(date=date(2026, 11, 20 + offset), items=items))
    extra = {"hotel": {"lat": hotel[0], "lng": hotel[1]}} if hotel else {}
    return ItineraryPlan(title="t", destination="Tokyo", currency_local="JPY", days=plan_days, **extra)


def test_route_metrics_totals_and_backtracking() -> None:
    p = dict(tokyo_points())
    compact = _plan([
        [("Senso-ji", p["Senso-ji"]), ("Nakamise", p["Nakamise Shopping Street"]), ("Skytree", p["Tokyo Skytree"])],
        [("Shibuya Sky", p["Shibuya Sky"]), ("Crossing", p["Shibuya Scramble Crossing"]), ("Takeshita", p["Takeshita Street"])],
    ])
    metrics = routing.route_metrics(compact)
    assert metrics.backtrack_hops == 0
    assert metrics.max_hop_km < 3
    assert metrics.total_travel_minutes == sum(
        i.travel_from_prev.minutes for d in compact.days for i in d.items if i.travel_from_prev
    )
    zigzag = _plan([[  # the spec's bad example: Shinjuku -> Asakusa -> Shibuya -> Ueno -> Shinjuku
        ("Shinjuku", p["Tokyo Metropolitan Government Building Observatory"]), ("Asakusa", p["Senso-ji"]),
        ("Shibuya", p["Shibuya Sky"]), ("Ueno", p["Ueno Park"]), ("Shinjuku again", p["Omoide Yokocho"]),
    ]])
    assert routing.route_metrics(zigzag).backtrack_hops >= 1
    assert routing.route_metrics(zigzag).max_hop_km > 8
    revisit = _plan([
        [("Senso-ji", p["Senso-ji"]), ("Skytree", p["Tokyo Skytree"])],
        [("Shibuya Sky", p["Shibuya Sky"]), ("Crossing", p["Shibuya Scramble Crossing"])],
        [("Nakamise", p["Nakamise Shopping Street"]), ("Ueno", p["Ueno Park"])],
    ])
    assert routing.route_metrics(revisit).backtrack_hops == 1


def test_hotel_area_is_not_a_backtrack() -> None:
    p = dict(tokyo_points())
    near_hotel = _plan([
        [("TMG", p["Tokyo Metropolitan Government Building Observatory"]), ("Omoide", p["Omoide Yokocho"])],
        [("Senso-ji", p["Senso-ji"]), ("Golden Gai", p["Shinjuku Golden Gai"])],
    ], hotel=SHINJUKU)
    assert routing.route_metrics(near_hotel).backtrack_hops == 0
