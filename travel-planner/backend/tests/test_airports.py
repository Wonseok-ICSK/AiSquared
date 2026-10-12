from __future__ import annotations

import pytest

from app.core import airports as ap
from app.schemas import AirportOut


def test_metro_expansion() -> None:
    assert ap.airports_for("SEL") == ["ICN", "GMP"]
    assert ap.airports_for("tyo") == ["NRT", "HND"]
    assert ap.airports_for("ICN") == ["ICN"]
    assert ap.airports_for("BKK") == ["BKK", "DMK"]
    assert ap.airports_for("SPK") == ["CTS"]


def test_is_metro() -> None:
    assert ap.is_metro("SEL") and ap.is_metro("BKK") and ap.is_metro("SPK")
    assert not ap.is_metro("ICN") and not ap.is_metro("HKG") and not ap.is_metro("XXX")


def test_all_metro_airports_exist_in_dataset() -> None:
    missing = [c for m in ap.METRO_CODES.values() for c in m.airports if ap.get_airport(c) is None]
    assert missing == []
    assert len(ap.METRO_CODES) >= 34


def test_get_airport_and_metro_lookup() -> None:
    icn = ap.get_airport("icn")
    assert icn is not None and "Incheon" in icn.name and icn.country == "KR"
    assert ap.get_airport("ZZZ") is None
    assert ap.metro_for_airport("HND") == "TYO"
    assert ap.metro_for_airport("PVG") == "SHA"
    assert ap.metro_for_airport("CDG") == "PAR"
    assert ap.metro_for_airport("DXB") is None


def test_nearby_airports() -> None:
    near_nrt = ap.nearby_airports("NRT")
    assert "HND" in near_nrt and "NRT" not in near_nrt
    assert "NRT" in ap.nearby_airports("HND")
    assert "GMP" in ap.nearby_airports("ICN")
    near_osa = ap.nearby_airports("OSA", radius_km=50)
    assert not {"KIX", "ITM", "UKB"} & set(near_osa)
    assert ap.nearby_airports("ZZZ") == []


def test_haversine() -> None:
    icn, nrt = ap.get_airport("ICN"), ap.get_airport("NRT")
    assert 1200 < ap.haversine_km(icn.lat, icn.lon, nrt.lat, nrt.lon) < 1320
    assert ap.haversine_km(1.0, 2.0, 1.0, 2.0) == 0


def test_city_info() -> None:
    nrt = ap.city_info("NRT")
    assert nrt is not None
    assert (nrt["city"], nrt["country"], nrt["currency"]) == ("Tokyo", "JP", "JPY")
    assert ap.city_info("SEL")["currency"] == "KRW"
    assert ap.city_info("TPE")["currency"] == "TWD"
    assert ap.city_info("CDG")["currency"] == "EUR"
    assert ap.city_info("ZZZ") is None


def test_country_currency_coverage() -> None:
    assert len(ap.COUNTRY_CURRENCY) >= 60
    for country, currency in {"KR": "KRW", "JP": "JPY", "VN": "VND", "US": "USD", "DE": "EUR"}.items():
        assert ap.COUNTRY_CURRENCY[country] == currency


def test_search_tokyo() -> None:
    results = ap.search("tokyo")
    codes = [r["code"] for r in results]
    assert codes[0] == "TYO" and results[0]["is_metro"] is True
    assert {"NRT", "HND"} <= set(codes)
    for result in results:
        AirportOut.model_validate(result)


@pytest.mark.parametrize(
    ("query", "first"),
    [("SEL", "SEL"), ("icn", "ICN"), ("도쿄", "TYO"), ("seoul", "SEL"), ("sao paulo", "SAO"), ("다낭", "DAD")],
)
def test_search_ranking(query: str, first: str) -> None:
    assert ap.search(query)[0]["code"] == first


def test_search_limits_and_dedupes() -> None:
    assert ap.search("") == []
    assert len(ap.search("a", limit=5)) == 5
    codes = [r["code"] for r in ap.search("bangkok")]
    assert codes.count("BKK") == 1 and "DMK" in codes
    hnd = next(r for r in ap.search("HND") if r["code"] == "HND")
    assert hnd["city"] == "Tokyo" and hnd["city_code"] == "TYO"


def test_all_alias_keys_exist_in_dataset() -> None:
    assert [code for code in ap.AIRPORT_ALIASES if ap.get_airport(code) is None] == []
    assert ap.search("울란바토르")[0]["code"] == "UBN"
