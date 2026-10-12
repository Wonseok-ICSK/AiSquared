from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from apscheduler.job import Job
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import session_scope
from app.models import SearchRun, Trip
from app.schemas import SchedulerJobOut
from app.services import monitoring
from app.services import scheduler as scheduler_module
from app.services.monitoring import create_run, run_trip_check
from app.services.scheduler import (
    CATCH_UP_DELAY_S,
    HOUSEKEEPING_JOB_ID,
    JOB_JITTER_S,
    MonitorScheduler,
    _describe_interval,
    get_scheduler,
    unschedule_trip,
)
from app.timeutil import now_local
from monitoring_fakes import add_trip, days, make_trip

SEOUL = ZoneInfo("Asia/Seoul")
TODAY = now_local().date()


def future_trip(db: Session, **overrides: Any) -> Trip:
    values: dict[str, Any] = {
        "depart_date": TODAY + days(30),
        "return_date": TODAY + days(34),
        "providers": ["mock"],
    }
    values.update(overrides)
    return add_trip(db, **values)


@pytest.fixture
def sched(settings: Settings) -> Iterator[MonitorScheduler]:
    instance = MonitorScheduler(settings)
    yield instance
    instance.shutdown()


def utc_to_seoul(naive_utc: datetime) -> datetime:
    return naive_utc.replace(tzinfo=UTC).astimezone(SEOUL)


def within_jitter(local: datetime, hour: int, minute: int) -> bool:
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start <= local <= start + timedelta(seconds=JOB_JITTER_S)


# --------------------------------------------------------------------------- trip jobs


def test_times_mode_creates_one_cron_job_per_time(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, schedule_times=["17:30", "08:30"])
    ids = sched.sync_trip(trip)
    assert ids == [f"trip-{trip.id}-at-0830", f"trip-{trip.id}-at-1730"]
    assert not sched.running  # nothing started
    for job in sched._scheduler.get_jobs():
        assert isinstance(job.trigger, CronTrigger)
        assert str(job.trigger.timezone) == "Asia/Seoul" and job.trigger.jitter == JOB_JITTER_S
        assert (job.coalesce, job.max_instances, job.misfire_grace_time) == (True, 1, 3600)
        assert job.func is run_trip_check and job.args == (trip.id, "schedule")
        assert job.name == trip.name


def test_next_run_times_in_seoul(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, schedule_times=["08:30", "17:30"])
    sched.sync_trip(trip)
    next_run = sched.next_run_for_trip(trip.id)
    assert next_run is not None and next_run.tzinfo is None  # naive UTC
    local = utc_to_seoul(next_run)
    assert within_jitter(local, 8, 30) or within_jitter(local, 17, 30)
    now = datetime.now(SEOUL)
    assert now < local <= now + timedelta(hours=24, seconds=JOB_JITTER_S)


def test_cron_trigger_fire_time_math(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, schedule_times=["08:30"])
    sched.sync_trip(trip)
    (job,) = sched._scheduler.get_jobs()
    before = datetime(2026, 10, 7, 7, 0, tzinfo=SEOUL)
    fire = job.trigger.get_next_fire_time(None, before)
    assert within_jitter(fire, 8, 30) and fire.date() == before.date()
    naive_utc = fire.astimezone(UTC).replace(tzinfo=None)
    base = datetime(2026, 10, 6, 23, 30)  # 08:30 KST = 23:30 UTC the day before
    assert base <= naive_utc <= base + timedelta(seconds=JOB_JITTER_S)
    after = datetime(2026, 10, 7, 9, 0, tzinfo=SEOUL)
    assert job.trigger.get_next_fire_time(None, after).date() == after.date() + timedelta(days=1)


def test_jobs_listing_matches_schema(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, name="Tokyo", schedule_times=["08:30", "17:30"])
    other = future_trip(db, name="Osaka", schedule_mode="interval", schedule_interval_hours=6)
    sched.sync_trip(trip)
    sched.sync_trip(other)
    entries = sched.jobs()
    assert sorted(SchedulerJobOut.model_validate(e).trip_id for e in entries) == sorted(
        [trip.id, other.id]
    )
    next_runs = [e["next_run_at"] for e in entries]
    assert next_runs == sorted(next_runs)  # soonest first
    by_trip = {e["trip_id"]: e for e in entries}
    assert by_trip[trip.id] == {
        "job_id": f"trip-{trip.id}",
        "trip_id": trip.id,
        "trip_name": "Tokyo",
        "next_run_at": by_trip[trip.id]["next_run_at"],
        "trigger": "daily at 08:30, 17:30 (Asia/Seoul)",
    }
    assert by_trip[other.id]["trigger"] == "every 6 hours"
    assert SchedulerJobOut.model_validate(by_trip[trip.id]).model_dump(mode="json")[
        "next_run_at"
    ].endswith("Z")


def test_interval_mode(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, schedule_mode="interval", schedule_interval_hours=6)
    assert sched.sync_trip(trip) == [f"trip-{trip.id}-every"]
    (job,) = sched._scheduler.get_jobs()
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(hours=6)
    next_run = sched.next_run_for_trip(trip.id)
    expected = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=6)
    assert abs((next_run - expected).total_seconds()) <= JOB_JITTER_S + 5


def test_interval_without_hours_is_not_scheduled(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, schedule_mode="interval", schedule_interval_hours=None)
    assert sched.sync_trip(trip) == []


def test_resync_replaces_jobs(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, schedule_times=["08:30", "17:30"])
    sched.sync_trip(trip)
    trip.schedule_times = ["12:00"]
    assert sched.sync_trip(trip) == [f"trip-{trip.id}-at-1200"]
    assert [j.id for j in sched._scheduler.get_jobs()] == [f"trip-{trip.id}-at-1200"]
    assert sched.jobs()[0]["trigger"] == "daily at 12:00 (Asia/Seoul)"


def test_inactive_and_past_trips_lose_their_jobs(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db)
    assert sched.sync_trip(trip)
    trip.active = False
    assert sched.sync_trip(trip) == [] and sched.jobs() == []
    past = future_trip(db, depart_date=TODAY - days(1), return_date=TODAY + days(2))
    assert sched.sync_trip(past) == []
    assert sched.next_run_for_trip(past.id) is None


def test_remove_trip_only_touches_that_trip(sched: MonitorScheduler) -> None:
    for trip_id in (1, 10, 11):
        sched.sync_trip(make_trip(id=trip_id, depart_date=TODAY + days(5),
                                  return_date=TODAY + days(9)))
    assert sched.remove_trip(1) == 2
    assert {e["trip_id"] for e in sched.jobs()} == {10, 11}
    assert sched.remove_trip(1) == 0


def test_sync_all(db: Session, sched: MonitorScheduler) -> None:
    active = future_trip(db)
    inactive = future_trip(db, active=False)
    past = future_trip(db, depart_date=TODAY - days(2), return_date=TODAY)
    sched.sync_trip(make_trip(id=999, depart_date=TODAY + days(5), return_date=TODAY + days(6)))
    assert sched.sync_all() == 1
    assert {e["trip_id"] for e in sched.jobs()} == {active.id}
    assert sched.next_run_for_trip(inactive.id) is None
    assert sched.next_run_for_trip(past.id) is None
    assert sched.next_run_for_trip(999) is None


def test_job_function_runs_a_scheduled_check(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db)
    sched.sync_trip(trip)
    job = sched._scheduler.get_jobs()[0]
    run_id = job.func(*job.args)
    with session_scope() as other:
        run = other.get(SearchRun, run_id)
        assert (run.trigger, run.status) == ("schedule", "ok")


# --------------------------------------------------------------------------- housekeeping


def test_housekeeping_deactivates_past_trips_and_recovers_runs(
    db: Session, sched: MonitorScheduler
) -> None:
    keep = future_trip(db)
    gone = future_trip(db, depart_date=TODAY + days(1), return_date=TODAY + days(3))
    sched.sync_trip(keep)
    sched.sync_trip(gone)
    stale = create_run(db, keep, "schedule")
    assert sched.run_housekeeping(today=TODAY + days(2)) == 1
    db.expire_all()
    assert db.get(Trip, gone.id).active is False and db.get(Trip, keep.id).active is True
    assert {e["trip_id"] for e in sched.jobs()} == {keep.id}
    assert db.get(SearchRun, stale.id).status == "error"


# --------------------------------------------------------------------------- lifecycle


def test_disabled_scheduler_does_nothing(db: Session, settings: Settings) -> None:
    disabled = MonitorScheduler(settings.model_copy(update={"scheduler_enabled": False}))
    stale = create_run(db, future_trip(db), "manual")
    assert disabled.start() is False and not disabled.running
    assert disabled.sync_trip(future_trip(db)) == [] and disabled.jobs() == []
    db.expire_all()
    assert db.get(SearchRun, stale.id).status == "error"  # startup recovery still happens


def test_start_is_idempotent_and_schedules_everything(
    db: Session, sched: MonitorScheduler
) -> None:
    trip = future_trip(db, schedule_mode="interval", schedule_interval_hours=24)
    stale = create_run(db, trip, "schedule")
    assert sched.start() is True and sched.running
    assert sched.start() is True
    entries = {e["job_id"]: e for e in sched.jobs()}
    assert set(entries) == {f"trip-{trip.id}", HOUSEKEEPING_JOB_ID}
    housekeeping = entries[HOUSEKEEPING_JOB_ID]
    assert housekeeping["trip_id"] is None
    assert housekeeping["trigger"].startswith("daily at 03:10 (Asia/Seoul)")
    assert utc_to_seoul(housekeeping["next_run_at"]).strftime("%H:%M") == "03:10"
    assert list(entries)[-1] == HOUSEKEEPING_JOB_ID  # trips first
    trip_job = sched._scheduler.get_job(f"trip-{trip.id}-every")
    assert trip_job.next_run_time.tzinfo is not None  # real scheduler-computed time
    # a trip added while running is scheduled immediately
    added = future_trip(db, schedule_times=["09:00"])
    sched.sync_trip(added)
    assert sched.next_run_for_trip(added.id) is not None
    with session_scope() as other:
        assert other.get(SearchRun, stale.id).status == "error"
    sched.shutdown()
    assert not sched.running
    sched.shutdown()  # idempotent


def test_get_scheduler_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "_instance", None)
    first = get_scheduler()
    assert first is get_scheduler() and isinstance(first, MonitorScheduler)
    assert first.timezone_name == "Asia/Seoul" and not first.running


def test_describe_interval() -> None:
    assert _describe_interval(1) == "every hour"
    assert _describe_interval(1.5) == "every 1.5 hours"
    assert _describe_interval(12) == "every 12 hours"


def test_no_trip_rows_touched_by_listing(db: Session, sched: MonitorScheduler) -> None:
    future_trip(db)
    sched.sync_all()
    sched.jobs()
    assert len(db.scalars(select(SearchRun)).all()) == 0


# --------------------------------------------------------------------------- anchoring & catch-up


def naive_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def seoul_to_naive_utc(local: datetime) -> datetime:
    return local.astimezone(UTC).replace(tzinfo=None)


def assert_catch_up(when: datetime | None, now: datetime) -> None:
    """``when`` (naive UTC) is a catch-up shortly after ``now`` (naive UTC)."""
    assert when is not None
    delay = (when - now).total_seconds()
    assert CATCH_UP_DELAY_S - 1 <= delay <= CATCH_UP_DELAY_S + JOB_JITTER_S + 5, delay


def job_by_id(sched: MonitorScheduler, job_id: str) -> Job:
    (job,) = [job for job in sched._scheduler.get_jobs() if job.id == job_id]
    return job


def test_interval_counts_from_the_last_check_across_resyncs_and_restarts(
    db: Session, sched: MonitorScheduler, settings: Settings
) -> None:
    trip = future_trip(db, schedule_mode="interval", schedule_interval_hours=6)
    trip.last_checked_at = naive_utc_now() - timedelta(hours=5, minutes=50)
    db.commit()
    due = trip.last_checked_at + timedelta(hours=6)  # in about 10 minutes

    def assert_due(next_run: datetime | None) -> None:
        assert next_run is not None
        assert due - timedelta(seconds=1) <= next_run <= due + timedelta(seconds=JOB_JITTER_S + 1)

    sched.sync_trip(trip)
    assert_due(sched.next_run_for_trip(trip.id))
    trip.name = "renamed"  # PUT /trips/{id} -> sync_trip again: no postponement
    sched.sync_trip(trip)
    assert_due(sched.next_run_for_trip(trip.id))
    restarted = MonitorScheduler(settings)  # app restart
    try:
        assert restarted.start() is True
        assert_due(restarted.next_run_for_trip(trip.id))
    finally:
        restarted.shutdown()


def test_never_checked_interval_trip_counts_from_its_creation(
    db: Session, sched: MonitorScheduler
) -> None:
    trip = future_trip(db, schedule_mode="interval", schedule_interval_hours=24)
    trip.created_at = naive_utc_now() - timedelta(hours=23)
    db.commit()
    sched.sync_trip(trip)
    expected = trip.created_at + timedelta(hours=24)
    assert abs((sched.next_run_for_trip(trip.id) - expected).total_seconds()) <= JOB_JITTER_S + 1


def test_overdue_interval_trip_is_caught_up_then_keeps_its_rhythm(
    db: Session, sched: MonitorScheduler
) -> None:
    trip = future_trip(db, schedule_mode="interval", schedule_interval_hours=6)
    trip.last_checked_at = naive_utc_now() - timedelta(hours=7)
    db.commit()
    sched.sync_trip(trip)
    catch_up = sched.next_run_for_trip(trip.id)
    assert_catch_up(catch_up, naive_utc_now())
    job = job_by_id(sched, f"trip-{trip.id}-every")
    after = job.trigger.get_next_fire_time(job.next_run_time, job.next_run_time)
    following = seoul_to_naive_utc(after) - catch_up
    assert timedelta(hours=6) <= following <= timedelta(hours=6, seconds=JOB_JITTER_S)


def test_no_catch_up_while_the_trip_is_being_checked(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, schedule_mode="interval", schedule_interval_hours=6)
    trip.last_checked_at = naive_utc_now() - timedelta(hours=7)
    db.commit()
    assert monitoring._guard.try_claim(trip.id)
    try:
        sched.sync_trip(trip)
    finally:
        monitoring._guard.release(trip.id)
    expected = naive_utc_now() + timedelta(hours=6)
    assert abs((sched.next_run_for_trip(trip.id) - expected).total_seconds()) <= JOB_JITTER_S + 5


NOW_0840 = datetime(2026, 10, 7, 8, 40, tzinfo=SEOUL)


@pytest.mark.parametrize(
    ("last_checked_local", "created_local", "caught_up"),
    [
        pytest.param(datetime(2026, 10, 6, 17, 35), None, True, id="08:30 slot missed"),
        pytest.param(datetime(2026, 10, 7, 8, 31), None, False, id="08:30 slot ran"),
        pytest.param(None, datetime(2026, 10, 6, 12, 0), True, id="never checked, created before"),
        pytest.param(None, datetime(2026, 10, 7, 8, 35), False, id="created after the slot"),
    ],
)
def test_times_mode_catches_up_the_most_recent_missed_slot(
    db: Session,
    sched: MonitorScheduler,
    last_checked_local: datetime | None,
    created_local: datetime | None,
    caught_up: bool,
) -> None:
    trip = future_trip(db, schedule_times=["08:30", "17:30"])
    if last_checked_local is not None:
        trip.last_checked_at = seoul_to_naive_utc(last_checked_local.replace(tzinfo=SEOUL))
    if created_local is not None:
        trip.created_at = seoul_to_naive_utc(created_local.replace(tzinfo=SEOUL))
    db.commit()
    sched.sync_trip(trip, now=NOW_0840)
    morning = job_by_id(sched, f"trip-{trip.id}-at-0830")
    evening = job_by_id(sched, f"trip-{trip.id}-at-1730")
    assert not hasattr(evening, "next_run_time")  # follows its cron trigger
    if caught_up:
        catch_up = seoul_to_naive_utc(morning.next_run_time)
        assert_catch_up(catch_up, seoul_to_naive_utc(NOW_0840))
    else:
        assert not hasattr(morning, "next_run_time")


def test_catch_up_survives_scheduler_start(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db, schedule_mode="interval", schedule_interval_hours=6)
    trip.last_checked_at = naive_utc_now() - timedelta(days=2)
    db.commit()
    assert sched.start() is True
    assert_catch_up(sched.next_run_for_trip(trip.id), naive_utc_now())
    assert sched._scheduler.get_job(f"trip-{trip.id}-every").next_run_time is not None


# --------------------------------------------------------------------------- restart & shutdown


def test_restart_after_shutdown_runs_jobs(db: Session, sched: MonitorScheduler) -> None:
    trip = future_trip(db)
    assert sched.start() is True
    sched.shutdown()
    assert sched.jobs() == []  # a fresh APScheduler ...
    assert sched.start() is True and sched.running  # ... that start() fills again
    assert {e["job_id"] for e in sched.jobs()} == {f"trip-{trip.id}", HOUSEKEEPING_JOB_ID}
    fired = threading.Event()
    sched._scheduler.add_job(fired.set, "date", run_date=datetime.now(SEOUL), id="probe")
    assert fired.wait(5), "jobs must execute after a restart"


def test_shutdown_cancels_running_checks(
    sched: MonitorScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(scheduler_module, "cancel_running_checks", lambda: calls.append("x") or 0)
    sched.shutdown(cancel_running=False)
    assert calls == []
    sched.shutdown()
    assert calls == ["x"]


def test_start_deactivates_departed_trips(db: Session, sched: MonitorScheduler) -> None:
    gone = future_trip(db, depart_date=TODAY - days(1), return_date=TODAY + days(2))
    flexible = future_trip(
        db, depart_date=TODAY - days(1), return_date=TODAY + days(2), flex_depart_days=3
    )
    assert sched.start() is True  # housekeeping runs at start, not only at 03:10
    db.expire_all()
    assert db.get(Trip, gone.id).active is False
    assert db.get(Trip, flexible.id).active is True  # later flexible dates are still searchable
    assert {e["trip_id"] for e in sched.jobs()} == {flexible.id, None}


def test_unschedule_trip_uses_the_process_scheduler(
    db: Session, sched: MonitorScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "_instance", None)
    assert unschedule_trip(1) == 0  # no scheduler yet: nothing to do (and none created)
    assert scheduler_module._instance is None
    trip = future_trip(db)
    sched.sync_trip(trip)
    monkeypatch.setattr(scheduler_module, "_instance", sched)
    assert unschedule_trip(trip.id) == 2 and sched.jobs() == []
