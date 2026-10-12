from __future__ import annotations

from datetime import date

from sqlalchemy import Engine, delete, func, inspect, select, text
from sqlalchemy.orm import Session

from app.models import (
    AlertRule,
    AppSetting,
    FlightOffer,
    Itinerary,
    Notification,
    ProviderSession,
    SearchRun,
    TravelPreference,
    Trip,
)

CHILD_MODELS = (SearchRun, FlightOffer, AlertRule, Notification, TravelPreference, Itinerary)


def _full_trip(db: Session) -> Trip:
    trip = Trip(
        name="Tokyo autumn",
        origin="SEL",
        destination="TYO",
        depart_date=date(2026, 11, 20),
        return_date=date(2026, 11, 24),
        schedule_times=["08:30", "17:30"],
    )
    run = SearchRun(trip=trip, trigger="manual")
    offer = FlightOffer(
        trip=trip,
        run=run,
        provider="mock",
        offer_key="ICN-NRT-2026-11-20T09:05-KE703|-|ECONOMY",
        origin="ICN",
        destination="NRT",
        depart_date=date(2026, 11, 20),
        departure_airport="ICN",
        arrival_airport="NRT",
        departure_time="2026-11-20T09:05",
        arrival_time="2026-11-20T11:25",
        outbound={"departure_airport": "ICN"},
        price_total=300000,
        price_per_person=300000,
        currency="KRW",
    )
    rule = AlertRule(trip=trip, kind="below_target", threshold=250000)
    db.add_all(
        [
            trip,
            Notification(trip=trip, kind="price_alert", title="Price drop", body="..."),
            TravelPreference(trip=trip, budget_total=1_500_000, budget_flight=600_000),
            Itinerary(trip=trip),
            run,
            offer,
            rule,
        ]
    )
    db.commit()
    return trip


def test_create_full_trip_with_defaults(db: Session) -> None:
    trip = _full_trip(db)
    db.expire_all()
    loaded = db.get(Trip, trip.id)
    assert loaded is not None
    assert loaded.trip_type == "round_trip" and loaded.cabin == "economy"
    assert loaded.alt_destinations == [] and loaded.providers == []
    assert loaded.active is True and loaded.price_basis == "per_person"
    assert loaded.created_at is not None and loaded.updated_at is not None
    assert loaded.preferences is not None and loaded.preferences.style_weights == {}
    assert len(loaded.runs) == 1 and loaded.runs[0].status == "running"
    assert loaded.runs[0].provider_stats == {} and loaded.runs[0].log == []
    assert loaded.offers[0].run is loaded.runs[0]
    assert loaded.alert_rules[0].armed is True
    assert loaded.notifications[0].read_at is None
    assert loaded.itineraries[0].status == "pending"


def test_json_defaults_are_not_shared(db: Session) -> None:
    first = Trip(name="a", origin="ICN", destination="NRT", depart_date=date(2026, 1, 1))
    second = Trip(name="b", origin="ICN", destination="KIX", depart_date=date(2026, 1, 1))
    db.add_all([first, second])
    db.commit()
    first.alt_destinations.append("HND")
    db.commit()
    db.expire_all()
    assert db.get(Trip, first.id).alt_destinations == ["HND"]
    assert db.get(Trip, second.id).alt_destinations == []


def test_in_place_json_mutation_is_persisted(db: Session) -> None:
    trip = _full_trip(db)
    run = trip.runs[0]
    run.log.append("capped 3 combinations")
    run.provider_stats["mock"] = {"status": "ok", "offers": 5}
    db.commit()
    db.expire_all()
    reloaded = db.get(SearchRun, run.id)
    assert reloaded.log == ["capped 3 combinations"]
    assert reloaded.provider_stats["mock"]["offers"] == 5


def test_updated_at_changes_on_update(db: Session) -> None:
    trip = _full_trip(db)
    before = trip.updated_at
    trip.name = "renamed"
    db.commit()
    assert trip.updated_at >= before


def test_orm_cascade_delete(db: Session) -> None:
    trip = _full_trip(db)
    db.delete(trip)
    db.commit()
    for model in (Trip, *CHILD_MODELS):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_db_level_cascade_with_foreign_keys_on(db: Session) -> None:
    _full_trip(db)
    db.execute(delete(Trip))
    db.commit()
    for model in CHILD_MODELS:
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_indexes_exist(engine: Engine) -> None:
    inspector = inspect(engine)

    def index_columns(table: str) -> list[list[str]]:
        return [ix["column_names"] for ix in inspector.get_indexes(table)]

    assert ["trip_id", "offer_key"] in index_columns("flight_offers")
    assert ["run_id"] in index_columns("flight_offers")
    assert ["trip_id", "started_at"] in index_columns("search_runs")
    assert ["read_at"] in index_columns("notifications")


def test_string_primary_key_tables(db: Session) -> None:
    db.add_all(
        [
            ProviderSession(provider="google_flights"),
            AppSetting(key="runtime", value={"browser_headless": False}),
        ]
    )
    db.commit()
    assert db.get(ProviderSession, "google_flights").status == "none"
    assert db.get(AppSetting, "runtime").value == {"browser_headless": False}


def test_json_containers_exist_before_flush() -> None:
    trip = Trip(name="x", origin="ICN", destination="NRT", depart_date=date(2026, 11, 20))
    run = SearchRun(trip=trip)
    run.log.append("capped 2 combinations")  # before any flush
    run.provider_stats["mock"] = {"status": "skipped"}
    assert trip.alt_destinations == [] and trip.providers == [] and trip.schedule_times == []
    assert TravelPreference(budget_total=1, budget_flight=1).style_weights == {}
    assert Trip(name="y", origin="ICN", destination="KIX", depart_date=date(2026, 1, 1),
                providers=["mock"]).providers == ["mock"]  # explicit values win
    assert Trip(name="z", origin="ICN", destination="KIX",
                depart_date=date(2026, 1, 1)).alt_destinations is not trip.alt_destinations


def test_containers_mutated_before_flush_are_persisted(db: Session) -> None:
    trip = Trip(name="x", origin="ICN", destination="NRT", depart_date=date(2026, 11, 20))
    run = SearchRun(trip=trip)
    run.log.append("skipped KIX")
    db.add(trip)
    db.commit()
    db.expire_all()
    assert db.get(SearchRun, run.id).log == ["skipped KIX"]


def test_nullable_json_none_is_sql_null(db: Session) -> None:
    trip = _full_trip(db)
    offer = trip.offers[0]
    offer.inbound = None
    db.add(Itinerary(trip=trip, plan=None))
    db.commit()
    raw = db.execute(text("SELECT inbound IS NULL FROM flight_offers")).scalar_one()
    assert raw == 1
    assert db.scalar(select(func.count()).where(FlightOffer.inbound.is_(None))) == 1
    assert db.scalar(select(func.count()).where(Itinerary.plan.is_(None))) == 2
    offer.inbound = {"departure_airport": "NRT"}
    db.commit()
    db.expire_all()
    assert db.get(FlightOffer, offer.id).inbound == {"departure_airport": "NRT"}
