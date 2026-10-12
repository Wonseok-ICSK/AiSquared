"""Open-Meteo forecast / climatology (respx-mocked) and the live check."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import httpx
import pytest
import respx

from app.services.itinerary import weather

TOKYO = (35.6812, 139.7671)
TODAY = date(2026, 10, 6)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    weather.clear_cache()
    yield
    weather.clear_cache()


def _forecast_body(days: list[date]) -> dict:
    return {
        "latitude": 35.68, "longitude": 139.77, "timezone": "Asia/Tokyo",
        "daily": {
            "time": [d.isoformat() for d in days],
            "weather_code": [61, 0, 3][: len(days)],
            "temperature_2m_max": [18.4, 21.0, 19.6][: len(days)],
            "temperature_2m_min": [12.1, 13.0, 14.2][: len(days)],
            "precipitation_sum": [6.2, 0.0, 0.4][: len(days)],
            "precipitation_probability_max": [80, 5, 30][: len(days)],
        },
    }


def _archive_handler(request: httpx.Request) -> httpx.Response:
    start = date.fromisoformat(request.url.params["start_date"])
    end = date.fromisoformat(request.url.params["end_date"])
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return httpx.Response(200, json={"daily": {
        "time": [d.isoformat() for d in days],
        "temperature_2m_max": [16.0 for _ in days],
        "temperature_2m_min": [8.0 for _ in days],
        "precipitation_sum": [3.0 if d.day % 4 == 0 else 0.0 for d in days],
    }})


def test_forecast_within_horizon_and_cache() -> None:
    days = [date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)]
    with respx.mock as router:
        route = router.get(weather.FORECAST_URL).mock(return_value=httpx.Response(200, json=_forecast_body(days)))
        result = weather.get_weather(*TOKYO, days, "Asia/Tokyo", today=TODAY)
        again = weather.get_weather(*TOKYO, list(reversed(days)), "Asia/Tokyo", today=TODAY)
    assert route.call_count == 1 and again == result
    params = route.calls.last.request.url.params
    assert params["start_date"] == "2026-10-10" and params["end_date"] == "2026-10-12"
    assert params["timezone"] == "Asia/Tokyo" and "precipitation_probability_max" in params["daily"]
    assert route.calls.last.request.headers["User-Agent"] == weather.USER_AGENT
    first = result[date(2026, 10, 10)]
    assert first == {
        "temp_min_c": 12.1, "temp_max_c": 18.4, "temp_min": 12.1, "temp_max": 18.4, "precip_mm": 6.2,
        "precip_prob": 80, "weather_code": 61, "source": "forecast", "summary": "Light rain, 80% rain",
    }
    assert weather.is_wet(first) and not weather.is_wet(result[date(2026, 10, 11)])


def test_forecast_summary_in_korean_and_auto_timezone() -> None:
    days = [date(2026, 10, 10)]
    with respx.mock as router:
        route = router.get(weather.FORECAST_URL).mock(return_value=httpx.Response(200, json=_forecast_body(days)))
        result = weather.get_weather(*TOKYO, days, language="ko", today=TODAY)
    assert route.calls.last.request.url.params["timezone"] == "auto"
    assert result[days[0]]["summary"] == "약한 비, 강수확률 80%"
    assert result[days[0]]["temp_min_c"] == 12.1 and result[days[0]]["temp_max_c"] == 18.4


def test_climatology_beyond_horizon() -> None:
    trip = [date(2026, 11, 20) + timedelta(days=i) for i in range(4)]
    with respx.mock as router:
        route = router.get(weather.ARCHIVE_URL).mock(side_effect=_archive_handler)
        result = weather.get_weather(*TOKYO, trip, "Asia/Tokyo", today=TODAY)
        cached = weather.get_weather(*TOKYO, trip, "Asia/Tokyo", today=TODAY)
    assert route.call_count == weather.CLIMATE_YEARS
    years = sorted(int(call.request.url.params["start_date"][:4]) for call in route.calls)
    assert years == [2021, 2022, 2023, 2024, 2025]
    first_call = next(c for c in route.calls if c.request.url.params["start_date"].startswith("2025"))
    assert first_call.request.url.params["start_date"] == "2025-11-17"
    assert first_call.request.url.params["end_date"] == "2025-11-26"
    assert set(result) == set(trip) and cached == result
    day = result[trip[0]]
    assert day["source"] == "climatology" and day["temp_max_c"] == 16.0 and day["temp_min_c"] == 8.0
    assert day["temp_max"] == 16.0 and day["temp_min"] == 8.0
    assert 0 < day["precip_prob"] < 50 and day["weather_code"] is None
    assert day["summary"] == f"Climate average, rain on ~{day['precip_prob']}% of days"


def test_partial_archive_failure_still_returns_data() -> None:
    trip = [date(2026, 12, 30), date(2027, 1, 2)]
    calls: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["start_date"])
        return httpx.Response(500) if len(calls) == 1 else _archive_handler(request)

    with respx.mock as router:
        router.get(weather.ARCHIVE_URL).mock(side_effect=flaky)
        result = weather.get_weather(*TOKYO, trip, today=TODAY)
    assert set(result) == set(trip)
    assert len(calls) == weather.CLIMATE_YEARS and calls[0] == "2025-12-27"  # window spans New Year


def test_failures_return_empty() -> None:
    with respx.mock as router:
        router.get(weather.FORECAST_URL).mock(
            return_value=httpx.Response(400, json={"error": True, "reason": "Forecast days is invalid"})
        )
        assert weather.get_weather(*TOKYO, [date(2026, 10, 10)], today=TODAY) == {}
    with respx.mock as router:
        router.get(weather.FORECAST_URL).mock(side_effect=httpx.ConnectError("offline"))
        assert weather.get_weather(*TOKYO, [date(2026, 10, 10)], today=TODAY) == {}
    with respx.mock as router:
        router.get(weather.ARCHIVE_URL).mock(side_effect=httpx.ConnectError("offline"))
        assert weather.get_weather(*TOKYO, [date(2026, 12, 1)], today=TODAY) == {}
    with respx.mock as router:
        router.get(weather.FORECAST_URL).mock(return_value=httpx.Response(200, json={"oops": 1}))
        assert weather.get_weather(*TOKYO, [date(2026, 10, 10)], today=TODAY) == {}
    assert weather.get_weather(*TOKYO, [], today=TODAY) == {}


def test_helpers() -> None:
    assert weather.describe_code(95) == "Thunderstorm" and weather.describe_code(0, "ko") == "맑음"
    assert weather.describe_code(None) == ""
    assert weather.rain_factor({"precip_prob": 40}) == 0.4
    assert weather.rain_factor({"precip_prob": 10, "precip_mm": 8}) == 1.0
    assert weather.rain_factor(None) == 0.0 and not weather.is_wet(None)
    assert "CC BY 4.0" in weather.ATTRIBUTION


@pytest.mark.live
def test_live_open_meteo() -> None:
    today = date.today()
    soon = [today + timedelta(days=2), today + timedelta(days=3)]
    forecast = weather.get_weather(*TOKYO, soon, "Asia/Tokyo")
    assert set(forecast) == set(soon) and forecast[soon[0]]["source"] == "forecast"
    trip = [date(2026, 11, 20), date(2026, 11, 21)]
    climate = weather.get_weather(*TOKYO, trip, "Asia/Tokyo")
    assert set(climate) == set(trip)
    assert 5 < climate[trip[0]]["temp_max"] < 25 and climate[trip[0]]["source"] == "climatology"
