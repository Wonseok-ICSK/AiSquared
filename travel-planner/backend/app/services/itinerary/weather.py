"""Daily weather for trip dates from Open-Meteo (no key; CC BY 4.0) — DESIGN §13.4.

* Every trip date within ``today + 15`` days (and not long past) → the **forecast** API.
* Otherwise → **climatology** from the ERA5 archive API: the same calendar window (±3 days)
  in each of the last 5 years (one small request per year), averaged per trip day; rain
  probability = share of days with ≥ 1 mm precipitation.

:func:`get_weather` never raises: failures are logged and yield ``{}`` (or the dates that
could be computed). Results are cached in memory (forecast 3 h, climatology for the process
lifetime). Show :data:`ATTRIBUTION` wherever the data is displayed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from datetime import date, timedelta
from statistics import fmean
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "AiSquaredTravel/0.1 (personal use)"
HTTP_TIMEOUT_S = 15.0
FORECAST_HORIZON_DAYS = 15
FORECAST_PAST_DAYS = 90
ARCHIVE_DELAY_DAYS = 6
CLIMATE_YEARS = 5
CLIMATE_WINDOW_DAYS = 3
RAIN_DAY_MM = 1.0
FORECAST_TTL_S = 3 * 3600
WET_PRECIP_PROB = 50
WET_PRECIP_MM = 3.0

_FORECAST_DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "precipitation_probability_max"
)
_ARCHIVE_DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum"

_WMO: dict[int, tuple[str, str]] = {
    0: ("Clear", "맑음"),
    1: ("Mostly clear", "대체로 맑음"),
    2: ("Partly cloudy", "구름 조금"),
    3: ("Overcast", "흐림"),
    45: ("Fog", "안개"),
    48: ("Rime fog", "짙은 안개"),
    51: ("Light drizzle", "약한 이슬비"),
    53: ("Drizzle", "이슬비"),
    55: ("Heavy drizzle", "강한 이슬비"),
    56: ("Freezing drizzle", "어는 이슬비"),
    57: ("Freezing drizzle", "어는 이슬비"),
    61: ("Light rain", "약한 비"),
    63: ("Rain", "비"),
    65: ("Heavy rain", "강한 비"),
    66: ("Freezing rain", "어는 비"),
    67: ("Freezing rain", "어는 비"),
    71: ("Light snow", "약한 눈"),
    73: ("Snow", "눈"),
    75: ("Heavy snow", "많은 눈"),
    77: ("Snow grains", "싸락눈"),
    80: ("Light showers", "약한 소나기"),
    81: ("Showers", "소나기"),
    82: ("Violent showers", "강한 소나기"),
    85: ("Snow showers", "눈 소나기"),
    86: ("Heavy snow showers", "강한 눈 소나기"),
    95: ("Thunderstorm", "뇌우"),
    96: ("Thunderstorm with hail", "우박을 동반한 뇌우"),
    99: ("Thunderstorm with hail", "우박을 동반한 뇌우"),
}

_cache: dict[tuple[Any, ...], tuple[float, dict[date, dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """Forget cached weather (tests)."""
    with _cache_lock:
        _cache.clear()


def describe_code(code: int | None, language: str = "en") -> str:
    """Human description of a WMO weather code."""
    if code is None:
        return ""
    en, ko = _WMO.get(int(code), ("Unknown", "알 수 없음"))
    return ko if language == "ko" else en


def is_wet(day_weather: dict[str, Any] | None) -> bool:
    """True when a day's weather suggests rain (probability ≥ 50 % or ≥ 3 mm)."""
    if not day_weather:
        return False
    prob = day_weather.get("precip_prob")
    mm = day_weather.get("precip_mm")
    return bool((prob is not None and prob >= WET_PRECIP_PROB) or (mm is not None and mm >= WET_PRECIP_MM))


def rain_factor(day_weather: dict[str, Any] | None) -> float:
    """0..1 rain likelihood for planning (probability, or 1.0 for ≥ 3 mm)."""
    if not day_weather:
        return 0.0
    prob = day_weather.get("precip_prob")
    factor = (prob or 0) / 100.0
    if (day_weather.get("precip_mm") or 0) >= WET_PRECIP_MM:
        factor = max(factor, 1.0)
    return min(1.0, factor)


def _summary(day: dict[str, Any], language: str) -> str:
    """Condition + rain chance. Temperatures are separate keys (``temp_min_c``/``temp_max_c``)
    that the UI renders next to the summary, so they are not repeated here."""
    prob = day.get("precip_prob")
    if day["source"] == "forecast":
        label = describe_code(day.get("weather_code"), language)
        rain = "" if prob is None else (f"강수확률 {prob}%" if language == "ko" else f"{prob}% rain")
        return ", ".join(part for part in (label, rain) if part)
    if prob is None:
        return "평년 기준" if language == "ko" else "Climate average"
    if language == "ko":
        return f"평년 기준, 비 온 날 약 {prob}%"
    return f"Climate average, rain on ~{prob}% of days"


def _entry(temp_min: float | None, temp_max: float | None, **fields: Any) -> dict[str, Any]:
    """Day entry with both key styles: ``temp_min_c``/``temp_max_c`` (shared ``PlanDay.weather``
    contract, read by the UI) and ``temp_min``/``temp_max`` (same values, module API)."""
    return {"temp_min": temp_min, "temp_max": temp_max, "temp_min_c": temp_min, "temp_max_c": temp_max,
            **fields}


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=HTTP_TIMEOUT_S, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url, params=params)
    if response.status_code != 200:
        raise httpx.HTTPStatusError(
            f"Open-Meteo HTTP {response.status_code}: {response.text[:200]}",
            request=response.request, response=response,
        )
    data = response.json()
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError(f"Open-Meteo error: {data.get('reason') if isinstance(data, dict) else data}")
    return data


def _daily_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    daily = data.get("daily") or {}
    days = daily.get("time") or []
    rows = []
    for i, day in enumerate(days):
        rows.append({key: (values[i] if i < len(values) else None)
                     for key, values in daily.items() if isinstance(values, list)} | {"time": day})
    return rows


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _forecast(lat: float, lon: float, days: list[date], tz: str, language: str) -> dict[date, dict[str, Any]]:
    data = _get_json(FORECAST_URL, {
        "latitude": round(lat, 4), "longitude": round(lon, 4), "daily": _FORECAST_DAILY,
        "timezone": tz, "start_date": days[0].isoformat(), "end_date": days[-1].isoformat(),
    })
    wanted = set(days)
    out: dict[date, dict[str, Any]] = {}
    for row in _daily_rows(data):
        day = date.fromisoformat(row["time"])
        if day not in wanted:
            continue
        prob = row.get("precipitation_probability_max")
        entry = _entry(
            _round(row.get("temperature_2m_min")),
            _round(row.get("temperature_2m_max")),
            precip_mm=_round(row.get("precipitation_sum")),
            precip_prob=None if prob is None else int(round(prob)),
            weather_code=row.get("weather_code"),
            source="forecast",
        )
        entry["summary"] = _summary(entry, language)
        out[day] = entry
    return out


def _shift_year(day: date, year: int) -> date:
    try:
        return day.replace(year=year)
    except ValueError:  # 29 Feb -> 28 Feb
        return day.replace(year=year, day=28)


def _climate_years(first: date, last: date, today: date) -> list[int]:
    """The last :data:`CLIMATE_YEARS` years whose shifted window is already in the archive."""
    years: list[int] = []
    year = first.year - 1 if first > today else today.year
    while len(years) < CLIMATE_YEARS and year > first.year - 30:
        offset = year - first.year
        window_end = _shift_year(last, last.year + offset) + timedelta(days=CLIMATE_WINDOW_DAYS)
        if window_end <= today - timedelta(days=ARCHIVE_DELAY_DAYS):
            years.append(year)
        year -= 1
    return years


def _climatology(lat: float, lon: float, days: list[date], tz: str, today: date,
                 language: str) -> dict[date, dict[str, Any]]:
    first, last = days[0], days[-1]
    samples: dict[date, list[dict[str, Any]]] = {d: [] for d in days}
    for year in _climate_years(first, last, today):
        offset = year - first.year
        start = _shift_year(first, first.year + offset) - timedelta(days=CLIMATE_WINDOW_DAYS)
        end = _shift_year(last, last.year + offset) + timedelta(days=CLIMATE_WINDOW_DAYS)
        try:
            data = _get_json(ARCHIVE_URL, {
                "latitude": round(lat, 4), "longitude": round(lon, 4), "daily": _ARCHIVE_DAILY,
                "timezone": tz, "start_date": start.isoformat(), "end_date": end.isoformat(),
            })
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Open-Meteo archive %s failed: %s", year, exc)
            continue
        rows = {date.fromisoformat(r["time"]): r for r in _daily_rows(data)}
        for day in days:
            centre = _shift_year(day, day.year + offset)
            for delta in range(-CLIMATE_WINDOW_DAYS, CLIMATE_WINDOW_DAYS + 1):
                row = rows.get(centre + timedelta(days=delta))
                if row is not None:
                    samples[day].append(row)
    out: dict[date, dict[str, Any]] = {}
    for day, rows in samples.items():
        tmax = [r["temperature_2m_max"] for r in rows if r.get("temperature_2m_max") is not None]
        tmin = [r["temperature_2m_min"] for r in rows if r.get("temperature_2m_min") is not None]
        rain = [r["precipitation_sum"] for r in rows if r.get("precipitation_sum") is not None]
        if not (tmax and tmin):
            continue
        entry = _entry(
            round(fmean(tmin), 1),
            round(fmean(tmax), 1),
            precip_mm=round(fmean(rain), 1) if rain else None,
            precip_prob=int(round(100 * sum(1 for v in rain if v >= RAIN_DAY_MM) / len(rain))) if rain else None,
            weather_code=None,
            source="climatology",
        )
        entry["summary"] = _summary(entry, language)
        out[day] = entry
    return out


def get_weather(
    lat: float,
    lon: float,
    dates: Iterable[date],
    timezone: str | None = None,
    *,
    language: str = "en",
    today: date | None = None,
) -> dict[date, dict[str, Any]]:
    """Daily weather ``{date: {summary, temp_min_c, temp_max_c, temp_min, temp_max, precip_mm,
    precip_prob, source, weather_code}}`` (°C, mm, %) — ``source`` is ``"forecast"`` or
    ``"climatology"``; ``temp_min``/``temp_max`` repeat the ``_c`` values (the ``_c`` keys are
    the shared ``PlanDay.weather`` contract); ``summary`` = condition + rain chance.

    ``timezone`` is an IANA zone (``None`` → Open-Meteo ``auto``). Never raises; returns ``{}``
    on failure. ``today`` is injectable for tests (default: local today).
    """
    days = sorted(set(dates))
    if not days:
        return {}
    today = today or date.today()
    tz = timezone or "auto"
    use_forecast = (
        days[-1] <= today + timedelta(days=FORECAST_HORIZON_DAYS)
        and days[0] >= today - timedelta(days=FORECAST_PAST_DAYS)
    )
    mode = "forecast" if use_forecast else "climatology"
    key = (mode, round(lat, 2), round(lon, 2), tz, tuple(days), language, today if use_forecast else None)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
    if cached and cached[0] > now:
        return dict(cached[1])
    try:
        if use_forecast:
            result = _forecast(lat, lon, days, tz, language)
        else:
            result = _climatology(lat, lon, days, tz, today, language)
    except Exception as exc:  # noqa: BLE001 - weather is optional, never fail the plan
        logger.warning("Weather lookup failed (%s): %s", mode, exc)
        return {}
    if result:
        ttl = FORECAST_TTL_S if use_forecast else float("inf")
        with _cache_lock:
            _cache[key] = (now + ttl, result)
    return dict(result)
