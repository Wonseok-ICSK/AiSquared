"""Public holidays, closures and opening windows for candidates (DESIGN §13.4).

Holidays come from the ``holidays`` package by ISO country code. Closure rules:

* ``closed_on_holidays`` — closed on every public holiday.
* ``closed_weekdays`` — regular closing days (0 = Monday).
* ``holiday_shift`` — "closed Mondays (next weekday if Monday is a holiday)": when a regular
  closing day is a public holiday the place opens, and closes on the next non-holiday day
  instead (typical for Japanese museums, e.g. Monday 2026-11-23 Labour Thanksgiving Day ->
  closed Tuesday 2026-11-24).
* weekly ``opening_hours`` without a period on that weekday — closed.
"""

from __future__ import annotations

import logging
from collections.abc import Container, Iterable
from datetime import date, timedelta
from functools import lru_cache

import holidays as holidays_lib

from app.services.itinerary.models import Candidate, minutes_to_hhmm

logger = logging.getLogger(__name__)

HolidayDates = Container[date]
"""Anything supporting ``day in holidays`` (dict ``{date: name}``, set of dates, ...)."""

_MAX_SHIFT_DAYS = 7

_TIMEZONES: dict[str, str] = {
    "KR": "Asia/Seoul", "JP": "Asia/Tokyo", "CN": "Asia/Shanghai", "TW": "Asia/Taipei",
    "HK": "Asia/Hong_Kong", "MO": "Asia/Macau", "MN": "Asia/Ulaanbaatar", "TH": "Asia/Bangkok",
    "VN": "Asia/Ho_Chi_Minh", "PH": "Asia/Manila", "SG": "Asia/Singapore",
    "MY": "Asia/Kuala_Lumpur", "KH": "Asia/Phnom_Penh", "LA": "Asia/Vientiane",
    "MM": "Asia/Yangon", "BN": "Asia/Brunei", "IN": "Asia/Kolkata", "LK": "Asia/Colombo",
    "NP": "Asia/Kathmandu", "BD": "Asia/Dhaka", "PK": "Asia/Karachi", "MV": "Indian/Maldives",
    "UZ": "Asia/Tashkent", "KG": "Asia/Bishkek", "AE": "Asia/Dubai", "QA": "Asia/Qatar",
    "SA": "Asia/Riyadh", "KW": "Asia/Kuwait", "BH": "Asia/Bahrain", "OM": "Asia/Muscat",
    "JO": "Asia/Amman", "IL": "Asia/Jerusalem", "TR": "Europe/Istanbul", "NZ": "Pacific/Auckland",
    "FJ": "Pacific/Fiji", "PF": "Pacific/Tahiti", "NC": "Pacific/Noumea", "GU": "Pacific/Guam",
    "MP": "Pacific/Saipan", "PW": "Pacific/Palau", "PE": "America/Lima", "CO": "America/Bogota",
    "AR": "America/Argentina/Buenos_Aires", "CL": "America/Santiago", "GB": "Europe/London",
    "IE": "Europe/Dublin", "FR": "Europe/Paris", "DE": "Europe/Berlin", "IT": "Europe/Rome",
    "ES": "Europe/Madrid", "PT": "Europe/Lisbon", "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels", "AT": "Europe/Vienna", "CH": "Europe/Zurich", "CZ": "Europe/Prague",
    "HU": "Europe/Budapest", "PL": "Europe/Warsaw", "GR": "Europe/Athens", "HR": "Europe/Zagreb",
    "SI": "Europe/Ljubljana", "SK": "Europe/Bratislava", "DK": "Europe/Copenhagen",
    "SE": "Europe/Stockholm", "NO": "Europe/Oslo", "FI": "Europe/Helsinki",
    "IS": "Atlantic/Reykjavik", "EE": "Europe/Tallinn", "LV": "Europe/Riga",
    "LT": "Europe/Vilnius", "RO": "Europe/Bucharest", "BG": "Europe/Sofia",
    "RS": "Europe/Belgrade", "UA": "Europe/Kyiv", "GE": "Asia/Tbilisi", "CY": "Asia/Nicosia",
    "MT": "Europe/Malta", "LU": "Europe/Luxembourg", "MC": "Europe/Monaco",
    "ME": "Europe/Podgorica", "EG": "Africa/Cairo", "MA": "Africa/Casablanca",
    "ZA": "Africa/Johannesburg", "KE": "Africa/Nairobi", "TZ": "Africa/Dar_es_Salaam",
    "MU": "Indian/Mauritius",
}
# Multi-zone countries: (minimum longitude, zone) checked west -> east, first match wins.
_ZONE_BANDS: dict[str, tuple[tuple[float, str], ...]] = {
    "US": ((-180, "Pacific/Honolulu"), (-140, "America/Anchorage"), (-125, "America/Los_Angeles"),
           (-114, "America/Denver"), (-101, "America/Chicago"), (-87, "America/New_York")),
    "CA": ((-141, "America/Vancouver"), (-120, "America/Edmonton"), (-102, "America/Winnipeg"),
           (-90, "America/Toronto"), (-67, "America/Halifax")),
    "AU": ((-180, "Australia/Perth"), (129, "Australia/Adelaide"), (141, "Australia/Sydney")),
    "BR": ((-75, "America/Manaus"), (-55, "America/Sao_Paulo")),
    "MX": ((-118, "America/Tijuana"), (-108, "America/Mazatlan"), (-100, "America/Mexico_City"),
           (-88, "America/Cancun")),
    "ID": ((-180, "Asia/Jakarta"), (114.5, "Asia/Makassar"), (127, "Asia/Jayapura")),
    "RU": ((-180, "Europe/Kaliningrad"), (27, "Europe/Moscow"), (48, "Asia/Yekaterinburg"),
           (68, "Asia/Omsk"), (80, "Asia/Novosibirsk"), (95, "Asia/Irkutsk"),
           (118, "Asia/Yakutsk"), (130, "Asia/Vladivostok"), (150, "Asia/Kamchatka")),
    "KZ": ((-180, "Asia/Aqtobe"), (60, "Asia/Almaty")),
}


def guess_timezone(country: str, lon: float | None = None) -> str | None:
    """Best-guess IANA zone for a country (longitude picks the band in multi-zone countries)."""
    code = (country or "").upper()
    if code in _TIMEZONES:
        return _TIMEZONES[code]
    bands = _ZONE_BANDS.get(code)
    if not bands:
        return None
    zone = bands[0][1]
    for min_lon, name in bands:
        if lon is not None and lon >= min_lon:
            zone = name
    return zone


@lru_cache(maxsize=64)
def _holidays_cached(country: str, years: tuple[int, ...], language: str) -> tuple[tuple[date, str], ...]:
    try:
        calendar = holidays_lib.country_holidays(country, years=list(years))
        lang = _pick_language(calendar.supported_languages, language)
        if lang:
            calendar = holidays_lib.country_holidays(country, years=list(years), language=lang)
    except NotImplementedError:
        logger.info("No holiday calendar for country %s", country)
        return ()
    return tuple(sorted((day, str(name)) for day, name in calendar.items()))


def _pick_language(supported: Iterable[str], language: str) -> str | None:
    options = list(supported)
    for wanted in ((language,) if language != "en" else ()) + ("en_US", "en"):
        for option in options:
            if option == wanted or option.split("_")[0] == wanted:
                return option
    return None


def holidays_for(country: str, years: Iterable[int], language: str = "en") -> dict[date, str]:
    """Public holidays ``{date: name}`` of ``country`` (ISO alpha-2) for ``years``.

    Names are in ``language`` when the calendar supports it (else English, else native).
    Unknown countries yield ``{}``.
    """
    key = tuple(sorted(set(years)))
    if not country or not key:
        return {}
    return dict(_holidays_cached(country.upper(), key, language))


def holidays_for_dates(country: str, dates: Iterable[date], language: str = "en") -> dict[date, str]:
    """Holidays in the years spanned by ``dates`` plus one week margin (holiday shifts)."""
    days = list(dates)
    if not days:
        return {}
    years = {d.year for d in days} | {(max(days) + timedelta(days=_MAX_SHIFT_DAYS)).year}
    years |= {(min(days) - timedelta(days=_MAX_SHIFT_DAYS)).year}
    return holidays_for(country, years, language)


def is_holiday(day: date, holidays: HolidayDates | None) -> bool:
    """True when ``day`` is in ``holidays`` (``None`` = no holiday data)."""
    return bool(holidays) and day in holidays  # type: ignore[operator]


def _shifted_closure(candidate: Candidate, day: date, holidays: HolidayDates | None) -> bool:
    """Closed today because an earlier regular closing day fell on a holiday."""
    if not candidate.holiday_shift or is_holiday(day, holidays):
        return False
    previous = day - timedelta(days=1)
    for _ in range(_MAX_SHIFT_DAYS):
        if not is_holiday(previous, holidays):
            return False
        if previous.weekday() in candidate.closed_weekdays:
            return True
        previous -= timedelta(days=1)
    return False


def is_closed(candidate: Candidate, day: date, holidays: HolidayDates | None = None) -> bool:
    """Whether ``candidate`` is closed for the whole of ``day`` (see module docstring)."""
    holiday = is_holiday(day, holidays)
    if candidate.closed_on_holidays and holiday:
        return True
    if day.weekday() in candidate.closed_weekdays:
        return not (candidate.holiday_shift and holiday)
    if _shifted_closure(candidate, day, holidays):
        return True
    if candidate.opening_hours:
        weekdays = {p.weekday for p in candidate.opening_hours}
        if day.weekday() not in weekdays and not (candidate.holiday_shift and holiday):
            return True
    return False


def open_windows(
    candidate: Candidate, day: date, holidays: HolidayDates | None = None
) -> list[tuple[int, int]] | None:
    """Opening windows on ``day`` as ``[(open, close)]`` minutes after midnight.

    ``[]`` = closed all day; ``None`` = open but hours unknown (no structured hours). A
    holiday-shifted opening on a regular closing day uses the hours of the nearest weekday.
    """
    if is_closed(candidate, day, holidays):
        return []
    if not candidate.opening_hours:
        return None
    weekday = day.weekday()
    periods = [p for p in candidate.opening_hours if p.weekday == weekday]
    if not periods:  # holiday-shifted opening: borrow the next listed weekday's hours
        for offset in range(1, 7):
            periods = [p for p in candidate.opening_hours if p.weekday == (weekday + offset) % 7]
            if periods:
                break
    return sorted(p.minutes() for p in periods)


def hours_label(candidate: Candidate, day: date, holidays: HolidayDates | None = None) -> str | None:
    """Human ``"09:30–17:00"`` label for ``day`` (``None`` when unknown or closed)."""
    windows = open_windows(candidate, day, holidays)
    if not windows:
        return None
    return ", ".join(f"{minutes_to_hhmm(a)}–{minutes_to_hhmm(b)}" for a, b in windows)
