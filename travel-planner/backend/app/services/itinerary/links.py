"""Map and restaurant-guide links, always built in code (never by the LLM) — DESIGN §13.4.

* Google Maps URLs need no API key (``/maps/search/?api=1`` and ``/maps/dir/?api=1``). Transit
  directions are generated **per leg** (multi-stop transit routes are not supported by Maps).
* Tabelog (Japan) has no API and forbids scraping: links only, never fetched server-side.
  Verified pattern (docs/research/places-routing.md): ``https://tabelog.com/kr/tokyo/{A13xx}/
  rstLst/{genre}/?SrtT=rt`` (rating-sorted list for a Tokyo area and genre) — always preferred.
  Keyword parameters are ignored on ``/kr/``, so the fallback (and the extra ``tabelog_search``
  link of a named restaurant) is the Japanese site's keyword search
  ``https://tabelog.com/rstLst/?vs=1&sk=<name>`` (unverified; opens in a browser, Cloudflare
  blocks non-browser clients).
* Naver Map search for Korean destinations: ``https://map.naver.com/p/search/<query>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, urlencode

from app.services.itinerary.models import Candidate, normalize_key

if TYPE_CHECKING:
    from app.services.itinerary.routing import Coord

GOOGLE_MAPS_SEARCH_URL = "https://www.google.com/maps/search/"
GOOGLE_MAPS_DIR_URL = "https://www.google.com/maps/dir/"
TABELOG_KR_BASE = "https://tabelog.com/kr"
TABELOG_KEYWORD_URL = "https://tabelog.com/rstLst/"
NAVER_MAP_SEARCH_URL = "https://map.naver.com/p/search/"

TravelMode = Literal["transit", "walking", "driving", "bicycling"]
_MODE_ALIASES: dict[str, TravelMode] = {
    "walk": "walking",
    "walking": "walking",
    "transit": "transit",
    "train": "transit",
    "subway": "transit",
    "bus": "transit",
    "taxi": "driving",
    "car": "driving",
    "drive": "driving",
    "driving": "driving",
    "bike": "bicycling",
    "bicycling": "bicycling",
}

TABELOG_GENRES: dict[str, str] = {
    # canonical cuisine key (models.CUISINE_SYNONYMS) -> verified Tabelog genre slug
    "sushi": "sushi",
    "ramen": "ramen",
    "yakiniku": "yakiniku",
    "izakaya": "izakaya",
    "tonkatsu": "tonkatsu",
    "tempura": "tempura",
    "soba": "soba",
    "udon": "udon",
    "unagi": "unagi",
    "yakitori": "yakitori",
    "cafe": "cafe",
    "dessert": "sweets",
    "japanese": "japanese",
    "kaiseki": "japanese",
    "okonomiyaki": "okonomiyaki",
}

TOKYO_AREAS: dict[str, tuple[str, ...]] = {
    # verified Tabelog Tokyo area codes -> keywords (English / Korean / Japanese)
    "A1301": ("ginza", "shinbashi", "shimbashi", "yurakucho", "hibiya", "긴자", "신바시", "유라쿠초",
              "銀座", "新橋", "有楽町"),
    "A1302": ("tokyo station", "nihonbashi", "marunouchi", "yaesu", "kyobashi", "otemachi",
              "도쿄역", "니혼바시", "마루노우치", "東京駅", "日本橋", "丸の内"),
    "A1303": ("shibuya", "ebisu", "daikanyama", "nakameguro", "시부야", "에비스", "다이칸야마",
              "渋谷", "恵比寿", "代官山"),
    "A1304": ("shinjuku", "yoyogi", "okubo", "신주쿠", "요요기", "오쿠보", "新宿", "代々木", "大久保"),
    "A1305": ("ikebukuro", "takadanobaba", "waseda", "mejiro", "이케부쿠로", "池袋", "高田馬場"),
    "A1306": ("harajuku", "omotesando", "aoyama", "하라주쿠", "오모테산도", "아오야마", "原宿",
              "表参道", "青山"),
    "A1307": ("roppongi", "azabu", "hiroo", "롯폰기", "아자부", "六本木", "麻布", "広尾"),
    "A1308": ("akasaka", "nagatacho", "tameike", "아카사카", "赤坂", "永田町"),
    "A1309": ("yotsuya", "ichigaya", "iidabashi", "kagurazaka", "요쓰야", "이다바시", "四谷", "市ヶ谷",
              "飯田橋", "神楽坂"),
    "A1310": ("akihabara", "kanda", "ochanomizu", "suidobashi", "아키하바라", "간다", "秋葉原",
              "神田", "御茶ノ水", "水道橋"),
    "A1311": ("ueno", "asakusa", "nippori", "yanaka", "우에노", "아사쿠사", "닛포리", "야나카", "上野",
              "浅草", "日暮里", "谷中"),
    "A1312": ("ryogoku", "kinshicho", "oshiage", "skytree", "koiwa", "료고쿠", "긴시초", "오시아게",
              "両国", "錦糸町", "押上"),
    "A1313": ("tsukiji", "odaiba", "toyosu", "tsukishima", "bay area", "쓰키지", "츠키지", "오다이바",
              "도요스", "築地", "お台場", "豊洲", "月島"),
    "A1314": ("hamamatsucho", "tamachi", "shinagawa", "시나가와", "하마마쓰초", "品川", "浜松町", "田町"),
    "A1315": ("kamata", "omori", "가마타", "蒲田", "大森"),
    "A1316": ("meguro", "shirokane", "gotanda", "메구로", "고탄다", "目黒", "白金", "五反田"),
}
_TOKYO_NAMES = ("tokyo", "도쿄", "동경", "東京")


def _clean(parts: tuple[str | None, ...]) -> str:
    seen: list[str] = []
    for part in parts:
        text = (part or "").strip()
        if text and not any(normalize_key(text) in normalize_key(s) for s in seen):
            seen.append(text)
    return " ".join(seen)


def _coord_or_text(value: Coord | str) -> str:
    if isinstance(value, str):
        return value
    return f"{value[0]:.6f},{value[1]:.6f}"


def google_maps_search(name: str, area: str | None = None, city: str | None = None) -> str:
    """Google Maps search link for ``"name area city"`` (no API key needed)."""
    return GOOGLE_MAPS_SEARCH_URL + "?" + urlencode({"api": 1, "query": _clean((name, area, city))})


def google_maps_point(lat: float, lon: float) -> str:
    """Google Maps link to a coordinate."""
    return GOOGLE_MAPS_SEARCH_URL + "?" + urlencode({"api": 1, "query": f"{lat:.6f},{lon:.6f}"})


def google_maps_directions(origin: Coord | str, dest: Coord | str, mode: str = "transit") -> str:
    """Google Maps directions for ONE leg (``mode``: transit | walk(ing) | driving | bicycling)."""
    travelmode = _MODE_ALIASES.get(mode.lower(), "transit")
    params = {
        "api": 1,
        "origin": _coord_or_text(origin),
        "destination": _coord_or_text(dest),
        "travelmode": travelmode,
    }
    return GOOGLE_MAPS_DIR_URL + "?" + urlencode(params)


def naver_map_search(name: str, area: str | None = None) -> str:
    """Naver Map search link (Korean destinations)."""
    return NAVER_MAP_SEARCH_URL + quote(_clean((name, area)), safe="")


def is_tokyo(city: str | None) -> bool:
    """True when ``city`` names Tokyo (English / Korean / Japanese)."""
    folded = normalize_key(city)
    return any(name in folded for name in _TOKYO_NAMES)


def tokyo_area_code(*texts: str | None) -> str | None:
    """Tabelog ``A13xx`` code for the first text mentioning a known Tokyo area."""
    for text in texts:
        folded = normalize_key(text)
        if not folded:
            continue
        for code, keywords in TOKYO_AREAS.items():
            if any(keyword in folded for keyword in keywords):
                return code
    return None


def tabelog_keyword_url(keyword: str) -> str:
    """Tabelog keyword search (Japanese site; unverified, works in a browser, not for scripts).

    Only the keyword ``sk`` is sent: the area parameter ``sa`` expects Japanese place names
    and is not verified, so the area belongs in the keyword when needed."""
    return TABELOG_KEYWORD_URL + "?" + urlencode({"vs": 1, "sk": keyword})


def tabelog_list_url(
    city: str | None, area: str | None = None, cuisine: str | None = None
) -> str | None:
    """Rating-sorted Tabelog list for a Tokyo area and/or genre (verified ``/kr/`` pattern).

    ``cuisine`` is a canonical cuisine key (``"sushi"``). Returns ``None`` outside Tokyo or
    when neither an area code nor a genre is known.
    """
    if not is_tokyo(city):
        return None
    code = tokyo_area_code(area)
    genre = TABELOG_GENRES.get(cuisine or "")
    if code and genre:
        return f"{TABELOG_KR_BASE}/tokyo/{code}/rstLst/{genre}/?SrtT=rt"
    if code:
        return f"{TABELOG_KR_BASE}/tokyo/{code}/rstLst/?SrtT=rt"
    if genre:
        return f"{TABELOG_KR_BASE}/tokyo/rstLst/{genre}/?SrtT=rt"
    return None


def tabelog_url(
    city: str | None,
    *,
    name: str | None = None,
    area: str | None = None,
    cuisine: str | None = None,
) -> str:
    """Best Tabelog link: the verified ``/kr/`` area/genre list (:func:`tabelog_list_url`)
    when it resolves, else the (unverified) keyword search for ``name`` or, without a name,
    ``"<cuisine> <area>"``."""
    listing = tabelog_list_url(city, area, cuisine)
    if listing:
        return listing
    return tabelog_keyword_url(name or _clean((cuisine, area)) or (city or ""))


def place_links(candidate: Candidate, city: str, country: str) -> dict[str, str]:
    """All links for a candidate: ``google_maps`` always; for Japanese restaurants/cafes
    ``tabelog`` (verified area/genre list when it resolves, else keyword search) plus
    ``tabelog_search`` (keyword search for the restaurant's name) when the list is used;
    ``naver_map`` for Korean destinations."""
    links = {"google_maps": google_maps_search(candidate.name, candidate.area, city)}
    code = (country or "").upper()
    if code == "JP" and candidate.is_food:
        cuisines = candidate.cuisines
        name = candidate.name_local or candidate.name
        links["tabelog"] = tabelog_url(city, name=name, area=candidate.area, cuisine=cuisines[0] if cuisines else None)
        if links["tabelog"].startswith(TABELOG_KR_BASE):
            links["tabelog_search"] = tabelog_keyword_url(name)
    if code == "KR":
        links["naver_map"] = naver_map_search(candidate.name_local or candidate.name, candidate.area)
    return links


def meal_search_links(
    query: str, area: str | None, city: str, country: str, cuisine: str | None = None
) -> dict[str, str]:
    """Links for a generic meal suggestion ("sushi near Asakusa"): Google Maps search plus a
    Tabelog list (Japan) or Naver Map search (Korea)."""
    links = {"google_maps": google_maps_search(query, area, city)}
    code = (country or "").upper()
    if code == "JP":
        links["tabelog"] = tabelog_url(city, area=area, cuisine=cuisine)
    if code == "KR":
        links["naver_map"] = naver_map_search(query, area)
    return links
