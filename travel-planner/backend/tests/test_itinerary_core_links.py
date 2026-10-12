"""Google Maps / Tabelog / Naver Map links (built in code, never fetched)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.services.itinerary import links
from app.services.itinerary.models import Candidate


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_google_maps_search() -> None:
    url = links.google_maps_search("Senso-ji", "Asakusa", "Tokyo")
    assert url.startswith("https://www.google.com/maps/search/?api=1&query=")
    assert _query(url)["query"] == ["Senso-ji Asakusa Tokyo"]
    assert _query(links.google_maps_search("Ueno Park", "Ueno", "Tokyo"))["query"] == ["Ueno Park Tokyo"]
    assert _query(links.google_maps_point(35.7, 139.8))["query"] == ["35.700000,139.800000"]


def test_google_maps_directions_per_leg() -> None:
    url = links.google_maps_directions((35.69, 139.70), (35.7148, 139.7967))
    params = _query(url)
    assert url.startswith("https://www.google.com/maps/dir/?api=1")
    assert params["origin"] == ["35.690000,139.700000"]
    assert params["destination"] == ["35.714800,139.796700"]
    assert params["travelmode"] == ["transit"]
    assert _query(links.google_maps_directions("Shinjuku Station", "Senso-ji", "walk"))["travelmode"] == ["walking"]
    assert _query(links.google_maps_directions("a", "b", "spaceship"))["travelmode"] == ["transit"]


def test_tabelog_list_uses_verified_pattern() -> None:
    assert links.tabelog_list_url("Tokyo", "Ginza", "sushi") == "https://tabelog.com/kr/tokyo/A1301/rstLst/sushi/?SrtT=rt"
    assert links.tabelog_list_url("도쿄", "아사쿠사", "ramen") == "https://tabelog.com/kr/tokyo/A1311/rstLst/ramen/?SrtT=rt"
    assert links.tabelog_list_url("Tokyo", "Shibuya", None) == "https://tabelog.com/kr/tokyo/A1303/rstLst/?SrtT=rt"
    assert links.tabelog_list_url("Tokyo", "Nowhere", "dessert") == "https://tabelog.com/kr/tokyo/rstLst/sweets/?SrtT=rt"
    assert links.tabelog_list_url("Osaka", "Namba", "sushi") is None
    assert links.tokyo_area_code("Koiwa") == "A1312"


def test_tabelog_prefers_the_verified_list_and_falls_back_to_keywords() -> None:
    url = links.tabelog_url("Osaka", area="Namba", cuisine="takoyaki")
    assert url.startswith("https://tabelog.com/rstLst/?") and _query(url)["sk"] == ["takoyaki Namba"]
    named = links.tabelog_url("Tokyo", name="すしざんまい本店", area="Tsukiji", cuisine="sushi")
    assert named == "https://tabelog.com/kr/tokyo/A1313/rstLst/sushi/?SrtT=rt"  # verified pattern wins
    unknown_area = links.tabelog_url("Tokyo", name="すしざんまい本店", area="Nowhere")
    assert _query(unknown_area) == {"vs": ["1"], "sk": ["すしざんまい本店"]}  # no unverified "sa"
    assert "sa" not in _query(links.tabelog_keyword_url("寿司"))


def test_place_links_by_country() -> None:
    sushi = Candidate(name="Sushi Zanmai", name_local="すしざんまい本店", category="restaurant", cuisine="sushi",
                      area="Tsukiji")
    jp = links.place_links(sushi, "Tokyo", "JP")
    assert set(jp) == {"google_maps", "tabelog", "tabelog_search"}
    assert jp["tabelog"] == "https://tabelog.com/kr/tokyo/A1313/rstLst/sushi/?SrtT=rt"
    assert _query(jp["tabelog_search"]) == {"vs": ["1"], "sk": ["すしざんまい本店"]}
    osaka = links.place_links(sushi, "Osaka", "JP")  # no verified list outside Tokyo
    assert set(osaka) == {"google_maps", "tabelog"} and _query(osaka["tabelog"])["sk"] == ["すしざんまい本店"]
    sight = Candidate(name="Gyeongbokgung", name_local="경복궁", category="historic", area="Jongno")
    kr = links.place_links(sight, "Seoul", "KR")
    assert kr["naver_map"] == "https://map.naver.com/p/search/%EA%B2%BD%EB%B3%B5%EA%B6%81%20Jongno"
    assert "tabelog" not in links.place_links(sight, "Seoul", "KR")
    assert set(links.place_links(sight, "Paris", "FR")) == {"google_maps"}


def test_meal_search_links() -> None:
    jp = links.meal_search_links("sushi", "Ginza", "Tokyo", "JP", "sushi")
    assert jp["tabelog"] == "https://tabelog.com/kr/tokyo/A1301/rstLst/sushi/?SrtT=rt"
    assert _query(jp["google_maps"])["query"] == ["sushi Ginza Tokyo"]
    assert "naver_map" in links.meal_search_links("restaurant", "Hongdae", "Seoul", "KR")
