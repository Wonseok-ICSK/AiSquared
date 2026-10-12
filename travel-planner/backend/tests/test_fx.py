from __future__ import annotations

import httpx
import pytest
import respx

from app.core import fx


def test_identity_needs_no_network() -> None:
    with respx.mock(assert_all_called=False) as router:
        assert fx.get_rate("krw", "KRW") == (1.0, "live")
        assert fx.convert(12345, "JPY", "JPY") == 12345
        assert router.calls.call_count == 0


def test_live_rate_and_cache() -> None:
    with respx.mock as router:
        route = router.get(fx.FRANKFURTER_V2_URL).mock(
            return_value=httpx.Response(200, json=[{"base": "JPY", "quote": "KRW", "rate": 8.5}])
        )
        assert fx.get_rate("JPY", "KRW") == (8.5, "live")
        assert fx.convert(1000, "jpy", "krw") == pytest.approx(8500)
        assert route.call_count == 1
        assert dict(route.calls.last.request.url.params) == {"base": "JPY", "quotes": "KRW"}


def test_legacy_endpoint_when_v2_fails() -> None:
    with respx.mock as router:
        router.get(fx.FRANKFURTER_V2_URL).mock(return_value=httpx.Response(500))
        router.get(fx.FRANKFURTER_LEGACY_URL).mock(
            return_value=httpx.Response(200, json={"base": "USD", "rates": {"KRW": 1350.0}})
        )
        assert fx.get_rate("USD", "KRW") == (1350.0, "live")


def test_static_fallback_when_offline() -> None:
    with respx.mock as router:
        router.get(fx.FRANKFURTER_V2_URL).mock(side_effect=httpx.ConnectError("offline"))
        router.get(fx.FRANKFURTER_LEGACY_URL).mock(side_effect=httpx.ConnectError("offline"))
        rate, source = fx.get_rate("TWD", "KRW")
        assert source == "static"
        assert rate == pytest.approx(fx.STATIC_USD_RATES["KRW"] / fx.STATIC_USD_RATES["TWD"])
        assert 30 < rate < 60
        assert fx.convert(100, "TWD", "KRW") == pytest.approx(100 * rate)
        calls = router.calls.call_count
        fx.get_rate("TWD", "KRW")  # cached (short TTL) -> no new requests
        assert router.calls.call_count == calls


def test_unknown_currency_raises() -> None:
    with respx.mock as router:
        router.get(fx.FRANKFURTER_V2_URL).mock(
            return_value=httpx.Response(422, json={"message": "invalid currency: XXX"})
        )
        router.get(fx.FRANKFURTER_LEGACY_URL).mock(return_value=httpx.Response(404))
        with pytest.raises(fx.FxError):
            fx.convert(1, "XXX", "KRW")


def test_static_rate_table() -> None:
    assert fx.static_rate("KRW", "KRW") == 1.0
    assert fx.static_rate("USD", "XXX") is None
    assert 1000 < fx.static_rate("USD", "KRW") < 2000


@pytest.mark.live
def test_live_frankfurter() -> None:
    rate, source = fx.get_rate("JPY", "KRW")
    assert source == "live" and 3 < rate < 20
