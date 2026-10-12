"""In-process monitoring scheduler (DESIGN §1, §11) around APScheduler's BackgroundScheduler.

Each active trip gets jobs calling ``monitoring.run_trip_check(trip_id, "schedule")``:
``schedule_mode == "times"`` -> one daily ``CronTrigger`` per ``HH:MM`` (local timezone,
up to :data:`JOB_JITTER_S` seconds of jitter); ``"interval"`` -> one ``IntervalTrigger``
every ``schedule_interval_hours``, counted from the trip's last check. Inactive trips and
trips with no searchable departure date left have no jobs.

Jobs live in memory and are rebuilt from the DB on every start, so schedules are anchored
to ``trip.last_checked_at`` (else ``created_at``): re-syncing a trip (app start, trip edit)
never postpones a check, and a trip whose interval is overdue, or whose most recent
scheduled time passed after its last check (PC off or asleep at that time), gets one
catch-up check :data:`CATCH_UP_DELAY_S` (+ jitter) seconds later.

A daily housekeeping job (03:10, and once at every start) deactivates departed trips and
recovers stale runs. Jobs coalesce missed runs (e.g. after sleep), never overlap, and may
start up to an hour late.

Usage (app lifespan): ``get_scheduler().start()`` / ``get_scheduler().shutdown()`` (the
instance can be started again after a shutdown); the trips API calls ``sync_trip(trip)``
after create/update and ``remove_trip(id)`` on delete.
"""

from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.job import Job
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import Trip
from app.services.monitoring import (
    cancel_running_checks,
    is_trip_running,
    recover_stale_runs,
    run_trip_check,
)
from app.services.search import trip_departed
from app.timeutil import parse_hhmm

logger = logging.getLogger(__name__)

JOB_JITTER_S = 120
"""Random delay (seconds) added to every trip job so checks don't fire on the exact minute."""
CATCH_UP_DELAY_S = 60
"""Minimum delay (seconds) of a catch-up check after (re)scheduling; plus up to
:data:`JOB_JITTER_S` so several overdue trips don't all start at once."""
MISFIRE_GRACE_S = 3600
HOUSEKEEPING_JOB_ID = "housekeeping"
HOUSEKEEPING_TIME = "03:10"
EXECUTOR_THREADS = 2
_TRIP_PREFIX = "trip-"
_TIMES_MARK = "-at-"
_INTERVAL_SUFFIX = "-every"
_MISSING = object()


def trip_job_prefix(trip_id: int) -> str:
    """Id prefix of every job belonging to ``trip_id`` (``"trip-7-"``)."""
    return f"{_TRIP_PREFIX}{trip_id}-"


def _trip_id_of(job_id: str) -> int | None:
    if not job_id.startswith(_TRIP_PREFIX):
        return None
    head = job_id[len(_TRIP_PREFIX) :].split("-", 1)[0]
    return int(head) if head.isdigit() else None


@dataclass(frozen=True)
class _JobSpec:
    """One trip job to add: id, trigger and (catch-up only) an explicit first run time."""

    job_id: str
    trigger: BaseTrigger
    first_run: datetime | None = None


class MonitorScheduler:
    """Schedules trip checks; safe to call from API threads (internally locked)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._tz = ZoneInfo(self._settings.timezone)
        self._scheduler = self._build()
        self._lock = threading.RLock()

    def _build(self) -> BackgroundScheduler:
        """A fresh (not started) APScheduler with its own thread pool."""
        return BackgroundScheduler(
            timezone=self._tz,
            executors={"default": ThreadPoolExecutor(max_workers=EXECUTOR_THREADS)},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": MISFIRE_GRACE_S,
            },
        )

    # ----------------------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        """``SCHEDULER_ENABLED`` (when False no jobs are created and nothing starts)."""
        return bool(self._settings.scheduler_enabled)

    @property
    def running(self) -> bool:
        """True while the background scheduler thread runs."""
        return bool(self._scheduler.running)

    @property
    def timezone_name(self) -> str:
        """Timezone the schedules are interpreted in (``TIMEZONE``)."""
        return self._settings.timezone

    def start(self) -> bool:
        """Start scheduling (idempotent; also after :meth:`shutdown`). Returns True if the
        scheduler is running.

        Every start marks runs left ``running`` by a previous process as interrupted (even
        when the scheduler is disabled). When enabled it also runs housekeeping once (so
        departed trips are deactivated even if the PC is never on at 03:10) and schedules
        every active trip, including catch-up checks for missed or overdue ones.
        """
        with self._lock:
            if self._scheduler.running:
                return True
            if not self.enabled:
                self._safe_recover()
                logger.info("Scheduler disabled (SCHEDULER_ENABLED=false); no automatic checks")
                return False
            self._safe_housekeeping()
            self._add_housekeeping_job()
            self.sync_all()
            self._scheduler.start()
            logger.info(
                "Scheduler started (%s): %d trip(s) scheduled",
                self.timezone_name, len(self._trip_ids()),
            )
            return True

    def shutdown(self, cancel_running: bool = True) -> None:
        """Stop scheduling without waiting for running checks (idempotent).

        With ``cancel_running`` (default; this is the app-shutdown hook) checks in progress
        are asked to stop too: they skip their remaining queries and end as interrupted, so
        the process can exit promptly. The instance can be started again afterwards (a
        fresh APScheduler is built; :meth:`start` re-creates the jobs).
        """
        with self._lock:
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
                self._scheduler = self._build()  # a shut-down APScheduler can't run jobs again
                logger.info("Scheduler stopped")
        if cancel_running:
            cancel_running_checks()

    # ----------------------------------------------------------------------- trip jobs

    def sync_trip(
        self, trip: Trip, today: date | None = None, now: datetime | None = None
    ) -> list[str]:
        """(Re)create the jobs of ``trip`` from its schedule; returns the job ids.

        Inactive trips, trips without a searchable departure date left (local ``today``)
        and a disabled scheduler get no jobs (existing ones are removed). Schedules are
        anchored to the trip's last check, so calling this again (app start, trip edit)
        never postpones a check; a missed time slot or overdue interval yields one catch-up
        check shortly after ``now``.
        """
        with self._lock:
            self.remove_trip(trip.id)
            if not self.enabled:
                return []
            now = (now or datetime.now(self._tz)).astimezone(self._tz)
            today = today or now.date()
            if not trip.active or trip_departed(trip, today):
                return []
            specs = self._job_specs(trip, now)
            for spec in specs:
                first_run = {} if spec.first_run is None else {"next_run_time": spec.first_run}
                self._scheduler.add_job(
                    run_trip_check,
                    trigger=spec.trigger,
                    args=[trip.id, "schedule"],
                    id=spec.job_id,
                    name=trip.name,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=MISFIRE_GRACE_S,
                    **first_run,
                )
                if spec.first_run is not None:
                    logger.info(
                        "Trip %s: scheduled check missed or overdue; catch-up at %s",
                        trip.id, spec.first_run.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    )
            job_ids = [spec.job_id for spec in specs]
            logger.debug("Trip %s scheduled: %s", trip.id, ", ".join(job_ids) or "no jobs")
            return job_ids

    def remove_trip(self, trip_id: int) -> int:
        """Remove every job of ``trip_id``; returns how many were removed."""
        with self._lock:
            prefix = trip_job_prefix(trip_id)
            removed = 0
            for job in self._scheduler.get_jobs():
                if job.id.startswith(prefix):
                    try:
                        self._scheduler.remove_job(job.id)
                        removed += 1
                    except JobLookupError:
                        pass
            return removed

    def sync_all(self, today: date | None = None) -> int:
        """Re-sync every trip from the DB (active ones scheduled, others and deleted ones
        removed); returns the number of trips with jobs."""
        with self._lock, session_scope() as db:
            trips = list(db.scalars(select(Trip)))
            scheduled = sum(1 for trip in trips if self.sync_trip(trip, today))
            known = {trip.id for trip in trips}
            for trip_id in self._trip_ids() - known:
                self.remove_trip(trip_id)
            return scheduled

    def _job_specs(self, trip: Trip, now: datetime) -> list[_JobSpec]:
        if trip.schedule_mode == "interval":
            return self._interval_specs(trip, now)
        return self._times_specs(trip, now)

    def _interval_specs(self, trip: Trip, now: datetime) -> list[_JobSpec]:
        """One interval job whose first run is ``interval`` after the last check (catch-up
        soon when that moment has already passed)."""
        hours = trip.schedule_interval_hours
        if not hours or hours <= 0:
            logger.warning("Trip %s: no valid schedule_interval_hours; not scheduled", trip.id)
            return []
        first = (self._last_check(trip, now) or now) + timedelta(hours=hours)
        trigger = IntervalTrigger(
            hours=hours, start_date=first, timezone=self._tz, jitter=JOB_JITTER_S
        )
        catch_up = self._catch_up_time(now) if first <= now else None
        job_id = trip_job_prefix(trip.id).rstrip("-") + _INTERVAL_SUFFIX
        return [_JobSpec(job_id, trigger, catch_up)]

    def _times_specs(self, trip: Trip, now: datetime) -> list[_JobSpec]:
        """One daily cron job per time; the job of the most recent slot gets a catch-up run
        when that slot passed after the trip's last check."""
        times: dict[time, str] = {}
        for value in trip.schedule_times or []:
            try:
                at = parse_hhmm(value)
            except ValueError:
                logger.warning("Trip %s: invalid schedule time %r ignored", trip.id, value)
                continue
            times[at] = f"{trip_job_prefix(trip.id).rstrip('-')}{_TIMES_MARK}{at:%H%M}"
        if not times:
            return []
        last_check = self._last_check(trip, now)
        latest_at = max(times, key=lambda at: self._previous_slot(at, now))
        missed = last_check is not None and self._previous_slot(latest_at, now) > last_check
        catch_up = self._catch_up_time(now) if missed else None
        return [
            _JobSpec(
                job_id,
                CronTrigger(hour=at.hour, minute=at.minute, timezone=self._tz, jitter=JOB_JITTER_S),
                catch_up if at == latest_at else None,
            )
            for at, job_id in sorted(times.items())
        ]

    def _last_check(self, trip: Trip, now: datetime) -> datetime | None:
        """When ``trip`` was last checked (else created), aware and not after ``now``;
        ``now`` while a check of it is running (that check is the latest one)."""
        if trip.id is not None and is_trip_running(trip.id):
            return now
        stamp = trip.last_checked_at or trip.created_at
        if stamp is None:
            return None
        aware = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
        return min(aware.astimezone(self._tz), now)

    def _previous_slot(self, at: time, now: datetime) -> datetime:
        """The latest local occurrence of ``at`` not after ``now``."""
        slot = datetime.combine(now.date(), at, tzinfo=self._tz)
        return slot if slot <= now else slot - timedelta(days=1)

    @staticmethod
    def _catch_up_time(now: datetime) -> datetime:
        return now + timedelta(seconds=CATCH_UP_DELAY_S + random.uniform(0, JOB_JITTER_S))

    # ----------------------------------------------------------------------- housekeeping

    def _add_housekeeping_job(self) -> None:
        at = parse_hhmm(HOUSEKEEPING_TIME)
        self._scheduler.add_job(
            self.run_housekeeping,
            trigger=CronTrigger(hour=at.hour, minute=at.minute, timezone=self._tz),
            id=HOUSEKEEPING_JOB_ID,
            name="Housekeeping",
            replace_existing=True,
        )

    def run_housekeeping(self, today: date | None = None) -> int:
        """Deactivate trips with no searchable departure date left before ``today``
        (local), drop their jobs and recover stale runs; returns the number deactivated."""
        today = today or datetime.now(self._tz).date()
        with session_scope() as db:
            candidates = db.scalars(
                select(Trip).where(Trip.active.is_(True), Trip.depart_date < today)
            )
            past = [trip for trip in candidates if trip_departed(trip, today)]
            for trip in past:
                trip.active = False
                self.remove_trip(trip.id)
                logger.info(
                    "Trip %s departed on %s; monitoring deactivated", trip.id, trip.depart_date
                )
        self._safe_recover()
        return len(past)

    def _safe_housekeeping(self) -> None:
        try:
            self.run_housekeeping()
        except Exception:  # noqa: BLE001 - startup must not fail on housekeeping
            logger.exception("Housekeeping failed")

    @staticmethod
    def _safe_recover() -> None:
        try:
            recover_stale_runs()
        except Exception:  # noqa: BLE001 - housekeeping must not crash the scheduler
            logger.exception("Recovering stale runs failed")

    # ----------------------------------------------------------------------- introspection

    def jobs(self) -> list[dict[str, Any]]:
        """Scheduled work as ``SchedulerJobOut``-shaped dicts: one entry per trip (its
        earliest next run, all its times in ``trigger``) plus housekeeping; soonest first."""
        with self._lock:
            by_trip: dict[int, list[Job]] = {}
            others: list[Job] = []
            for job in self._scheduler.get_jobs():
                trip_id = _trip_id_of(job.id)
                if trip_id is None:
                    others.append(job)
                else:
                    by_trip.setdefault(trip_id, []).append(job)
            entries = [self._trip_entry(trip_id, jobs) for trip_id, jobs in by_trip.items()]
            entries.extend(self._other_entry(job) for job in others)
        far_future = datetime.max
        return sorted(
            entries, key=lambda e: (e["trip_id"] is None, e["next_run_at"] or far_future)
        )

    def next_run_for_trip(self, trip_id: int) -> datetime | None:
        """Next scheduled check of ``trip_id`` as naive UTC, or ``None``."""
        with self._lock:
            prefix = trip_job_prefix(trip_id)
            times = [
                when
                for job in self._scheduler.get_jobs()
                if job.id.startswith(prefix) and (when := self._next_run(job)) is not None
            ]
        return min(times) if times else None

    def _trip_ids(self) -> set[int]:
        return {
            trip_id
            for job in self._scheduler.get_jobs()
            if (trip_id := _trip_id_of(job.id)) is not None
        }

    def _trip_entry(self, trip_id: int, jobs: list[Job]) -> dict[str, Any]:
        times = [when for job in jobs if (when := self._next_run(job)) is not None]
        return {
            "job_id": f"{_TRIP_PREFIX}{trip_id}",
            "trip_id": trip_id,
            "trip_name": jobs[0].name,
            "next_run_at": min(times) if times else None,
            "trigger": self._describe(jobs),
        }

    def _other_entry(self, job: Job) -> dict[str, Any]:
        text = (
            f"daily at {HOUSEKEEPING_TIME} ({self.timezone_name}) — deactivates past trips"
            if job.id == HOUSEKEEPING_JOB_ID
            else str(job.trigger)
        )
        return {
            "job_id": job.id,
            "trip_id": None,
            "trip_name": None,
            "next_run_at": self._next_run(job),
            "trigger": text,
        }

    def _describe(self, jobs: list[Job]) -> str:
        """``"daily at 08:30, 17:30 (Asia/Seoul)"`` or ``"every 6 hours"``."""
        times = sorted(
            f"{job.id[-4:-2]}:{job.id[-2:]}" for job in jobs if _TIMES_MARK in job.id
        )
        parts: list[str] = []
        if times:
            parts.append(f"daily at {', '.join(times)} ({self.timezone_name})")
        for job in jobs:
            if isinstance(job.trigger, IntervalTrigger):
                parts.append(_describe_interval(job.trigger.interval.total_seconds() / 3600))
        return "; ".join(parts)

    def _next_run(self, job: Job) -> datetime | None:
        """Next fire time as naive UTC (computed from the trigger before the scheduler starts)."""
        when = getattr(job, "next_run_time", _MISSING)
        if when is _MISSING:  # pending job: scheduler not started yet
            when = job.trigger.get_next_fire_time(None, datetime.now(self._tz))
        if not isinstance(when, datetime):
            return None
        return when.astimezone(UTC).replace(tzinfo=None)


def _describe_interval(hours: float) -> str:
    if hours == 1:
        return "every hour"
    text = f"{hours:g}"
    return f"every {text} hours"


_instance: MonitorScheduler | None = None
_instance_lock = threading.Lock()


def get_scheduler() -> MonitorScheduler:
    """The process-wide :class:`MonitorScheduler` (created on first use, not started)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MonitorScheduler()
        return _instance


def unschedule_trip(trip_id: int) -> int:
    """Remove ``trip_id``'s jobs from the process-wide scheduler if it exists (else no-op);
    returns how many jobs were removed."""
    instance = _instance
    return instance.remove_trip(trip_id) if instance is not None else 0
