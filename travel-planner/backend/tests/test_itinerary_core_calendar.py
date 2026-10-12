"""Holidays, closures and opening windows."""

from __future__ import annotations

from datetime import date

from app.services.itinerary import calendar_util
from app.services.itinerary.models import Candidate, OpeningPeriod

MON_HOLIDAY = date(2026, 11, 23)  # 勤労感謝の日 (Labour Thanksgiving Day), a Monday
TUE_AFTER = date(2026, 11, 24)
NORMAL_MON = date(2026, 11, 30)


def museum(**overrides: object) -> Candidate:
    data: dict[str, object] = {
        "name": "Tokyo National Museum",
        "category": "museum",
        "closed_weekdays": [0],
        "holiday_shift": True,
        "opening_hours": [{"weekday": d, "open": "09:30", "close": "17:00"} for d in range(1, 7)],
    }
    data.update(overrides)
    return Candidate(**data)


def test_japanese_holidays_2026() -> None:
    hols = calendar_util.holidays_for("JP", [2026])
    assert MON_HOLIDAY in hols and date(2026, 10, 12) in hols  # Sports Day
    assert hols[MON_HOLIDAY] == "Labor Thanksgiving Day"  # English names when supported
    assert calendar_util.holidays_for("JP", [2026], language="ko")[MON_HOLIDAY] == "Labor Thanksgiving Day"
    assert calendar_util.holidays_for("KR", [2026], language="ko")[date(2026, 10, 3)] == "개천절"
    assert calendar_util.holidays_for("XX", [2026]) == {}
    assert calendar_util.holidays_for("JP", []) == {}


def test_holidays_for_dates_covers_year_boundary() -> None:
    hols = calendar_util.holidays_for_dates("JP", [date(2026, 12, 30), date(2027, 1, 2)])
    assert date(2027, 1, 1) in hols


def test_closed_monday_shifts_to_next_day_when_monday_is_a_holiday() -> None:
    hols = calendar_util.holidays_for("JP", [2026])
    tnm = museum()
    assert calendar_util.is_closed(tnm, NORMAL_MON, hols)
    assert not calendar_util.is_closed(tnm, MON_HOLIDAY, hols)
    assert calendar_util.is_closed(tnm, TUE_AFTER, hols)
    assert not calendar_util.is_closed(tnm, date(2026, 11, 25), hols)
    assert calendar_util.open_windows(tnm, MON_HOLIDAY, hols) == [(570, 1020)]  # borrows Tuesday's hours
    assert calendar_util.open_windows(tnm, TUE_AFTER, hols) == []


def test_without_shift_or_holiday_data() -> None:
    hols = calendar_util.holidays_for("JP", [2026])
    plain = museum(holiday_shift=False)
    assert calendar_util.is_closed(plain, MON_HOLIDAY, hols)
    assert not calendar_util.is_closed(plain, TUE_AFTER, hols)
    assert calendar_util.is_closed(museum(), MON_HOLIDAY, None)  # no holiday data: plain rule


def test_closed_on_holidays_and_weekly_hours() -> None:
    hols = {MON_HOLIDAY: "holiday"}
    market = Candidate(name="Market", category="market", closed_on_holidays=True, closed_weekdays=[6])
    assert calendar_util.is_closed(market, MON_HOLIDAY, hols)
    assert calendar_util.is_closed(market, date(2026, 11, 22), hols)  # Sunday
    assert not calendar_util.is_closed(market, date(2026, 11, 21), hols)
    assert calendar_util.open_windows(market, date(2026, 11, 21), hols) is None  # open, hours unknown
    weekdays_only = Candidate(
        name="Office cafe", category="cafe",
        opening_hours=[OpeningPeriod(weekday=d, open="08:00", close="18:00") for d in range(5)],
    )
    assert calendar_util.is_closed(weekdays_only, date(2026, 11, 21))
    bar = Candidate(name="Bar", category="nightlife",
                    opening_hours=[{"weekday": 4, "open": "19:00", "close": "02:00"}])
    assert calendar_util.open_windows(bar, date(2026, 11, 20)) == [(1140, 1560)]
    assert calendar_util.hours_label(bar, date(2026, 11, 20)) == "19:00–02:00"
    assert calendar_util.hours_label(market, date(2026, 11, 21)) is None


def test_guess_timezone() -> None:
    assert calendar_util.guess_timezone("JP") == "Asia/Tokyo"
    assert calendar_util.guess_timezone("kr") == "Asia/Seoul"
    assert calendar_util.guess_timezone("US", -118.2) == "America/Los_Angeles"
    assert calendar_util.guess_timezone("US", -74.0) == "America/New_York"
    assert calendar_util.guess_timezone("US", -157.8) == "Pacific/Honolulu"
    assert calendar_util.guess_timezone("AU", 151.2) == "Australia/Sydney"
    assert calendar_util.guess_timezone("ZZ") is None
