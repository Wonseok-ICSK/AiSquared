"""Airport / metro-code lookup over ``core/data/airports.json`` (OurAirports, public domain).

Codes are IATA. A *metro* (city) code groups several airports (``TYO`` -> NRT, HND). Some
city codes equal one of their airports (``BKK`` -> BKK, DMK); such a code is treated as the
metro everywhere (``airports_for("BKK") == ["BKK", "DMK"]``). Single-airport entries in
:data:`METRO_CODES` only provide city names / Korean aliases and are not metros.
"""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent / "data" / "airports.json"
EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class Airport:
    """An airport with scheduled service."""

    code: str
    name: str
    city: str
    country: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Metro:
    """A city with one or more airports; ``lat``/``lon`` is the city centre."""

    code: str
    name: str
    country: str
    airports: tuple[str, ...]
    lat: float
    lon: float
    aliases: tuple[str, ...] = ()


_METROS: tuple[Metro, ...] = (
    Metro("SEL", "Seoul", "KR", ("ICN", "GMP"), 37.5665, 126.9780, ("서울",)),
    Metro("TYO", "Tokyo", "JP", ("NRT", "HND"), 35.6762, 139.6503, ("도쿄", "동경")),
    Metro("OSA", "Osaka", "JP", ("KIX", "ITM", "UKB"), 34.6937, 135.5023, ("오사카",)),
    Metro("NGO", "Nagoya", "JP", ("NGO",), 35.1815, 136.9066, ("나고야",)),
    Metro("SPK", "Sapporo", "JP", ("CTS",), 43.0618, 141.3545, ("삿포로",)),
    Metro("FUK", "Fukuoka", "JP", ("FUK",), 33.5904, 130.4017, ("후쿠오카",)),
    Metro("OKA", "Okinawa", "JP", ("OKA",), 26.2124, 127.6809, ("오키나와", "나하")),
    Metro("CJU", "Jeju", "KR", ("CJU",), 33.4996, 126.5312, ("제주",)),
    Metro("PUS", "Busan", "KR", ("PUS",), 35.1796, 129.0756, ("부산",)),
    Metro("BKK", "Bangkok", "TH", ("BKK", "DMK"), 13.7563, 100.5018, ("방콕",)),
    Metro("TPE", "Taipei", "TW", ("TPE", "TSA"), 25.0330, 121.5654, ("타이베이", "타이페이")),
    Metro("SHA", "Shanghai", "CN", ("PVG", "SHA"), 31.2304, 121.4737, ("상하이",)),
    Metro("BJS", "Beijing", "CN", ("PEK", "PKX"), 39.9042, 116.4074, ("베이징", "북경")),
    Metro("HKG", "Hong Kong", "HK", ("HKG",), 22.3193, 114.1694, ("홍콩",)),
    Metro("MNL", "Manila", "PH", ("MNL",), 14.5995, 120.9842, ("마닐라",)),
    Metro("CEB", "Cebu", "PH", ("CEB",), 10.3157, 123.8854, ("세부",)),
    Metro("KUL", "Kuala Lumpur", "MY", ("KUL", "SZB"), 3.1390, 101.6869, ("쿠알라룸푸르",)),
    Metro("JKT", "Jakarta", "ID", ("CGK", "HLP"), -6.2088, 106.8456, ("자카르타",)),
    Metro("DPS", "Bali", "ID", ("DPS",), -8.6705, 115.2126, ("발리", "덴파사르")),
    Metro("SGN", "Ho Chi Minh City", "VN", ("SGN",), 10.8231, 106.6297, ("호치민", "호찌민")),
    Metro("HAN", "Hanoi", "VN", ("HAN",), 21.0278, 105.8342, ("하노이",)),
    Metro("GUM", "Guam", "GU", ("GUM",), 13.4443, 144.7937, ("괌",)),
    Metro("SPN", "Saipan", "MP", ("SPN",), 15.1850, 145.7467, ("사이판",)),
    Metro("LON", "London", "GB", ("LHR", "LGW", "STN", "LTN", "LCY"), 51.5072, -0.1276, ("런던",)),
    Metro("PAR", "Paris", "FR", ("CDG", "ORY"), 48.8566, 2.3522, ("파리",)),
    Metro("MIL", "Milan", "IT", ("MXP", "LIN", "BGY"), 45.4642, 9.1900, ("밀라노",)),
    Metro("ROM", "Rome", "IT", ("FCO", "CIA"), 41.9028, 12.4964, ("로마",)),
    Metro("MOW", "Moscow", "RU", ("SVO", "DME", "VKO"), 55.7558, 37.6173, ("모스크바",)),
    Metro("NYC", "New York", "US", ("JFK", "EWR", "LGA"), 40.7128, -74.0060, ("뉴욕",)),
    Metro("CHI", "Chicago", "US", ("ORD", "MDW"), 41.8781, -87.6298, ("시카고",)),
    Metro("WAS", "Washington", "US", ("IAD", "DCA", "BWI"), 38.9072, -77.0369, ("워싱턴",)),
    Metro("YTO", "Toronto", "CA", ("YYZ", "YTZ"), 43.6532, -79.3832, ("토론토",)),
    Metro("SAO", "São Paulo", "BR", ("GRU", "CGH", "VCP"), -23.5505, -46.6333, ("상파울루",)),
    Metro("BUE", "Buenos Aires", "AR", ("EZE", "AEP"), -34.6037, -58.3816, ("부에노스아이레스",)),
)
METRO_CODES: dict[str, Metro] = {metro.code: metro for metro in _METROS}

# Korean names for popular single airports (searchable in addition to English names).
AIRPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "SIN": ("싱가포르",),
    "DAD": ("다낭",),
    "CXR": ("나트랑", "냐짱"),
    "PQC": ("푸꾸옥",),
    "BKI": ("코타키나발루",),
    "CNX": ("치앙마이",),
    "HKT": ("푸켓",),
    "MFM": ("마카오",),
    "VVO": ("블라디보스토크",),
    "UBN": ("울란바토르",),  # Chinggis Khaan Intl (ULN closed to scheduled service in 2021)
    "HNL": ("호놀룰루", "하와이"),
    "LAX": ("로스앤젤레스", "LA"),
    "SFO": ("샌프란시스코",),
    "SYD": ("시드니",),
    "KOJ": ("가고시마",),
    "TAE": ("대구",),
    "CJJ": ("청주",),
    "KWJ": ("광주",),
    "MWX": ("무안",),
    "RSU": ("여수",),
    "USN": ("울산",),
    "YNY": ("양양",),
}

COUNTRY_CURRENCY: dict[str, str] = {
    # East Asia
    "KR": "KRW", "JP": "JPY", "CN": "CNY", "TW": "TWD", "HK": "HKD", "MO": "MOP", "MN": "MNT",
    # South-east Asia
    "TH": "THB", "VN": "VND", "PH": "PHP", "SG": "SGD", "MY": "MYR", "ID": "IDR", "KH": "KHR",
    "LA": "LAK", "MM": "MMK", "BN": "BND",
    # South / Central Asia
    "IN": "INR", "LK": "LKR", "NP": "NPR", "BD": "BDT", "PK": "PKR", "MV": "MVR",
    "KZ": "KZT", "UZ": "UZS", "KG": "KGS",
    # Middle East
    "AE": "AED", "QA": "QAR", "SA": "SAR", "KW": "KWD", "BH": "BHD", "OM": "OMR", "JO": "JOD",
    "IL": "ILS", "TR": "TRY",
    # Oceania / Pacific (US territories use USD)
    "AU": "AUD", "NZ": "NZD", "FJ": "FJD", "PF": "XPF", "NC": "XPF", "GU": "USD", "MP": "USD",
    "PW": "USD",
    # Americas
    "US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL", "AR": "ARS", "CL": "CLP", "PE": "PEN",
    "CO": "COP",
    # Europe — eurozone
    "AT": "EUR", "BE": "EUR", "HR": "EUR", "CY": "EUR", "EE": "EUR", "FI": "EUR", "FR": "EUR",
    "DE": "EUR", "GR": "EUR", "IE": "EUR", "IT": "EUR", "LV": "EUR", "LT": "EUR", "LU": "EUR",
    "MT": "EUR", "NL": "EUR", "PT": "EUR", "SK": "EUR", "SI": "EUR", "ES": "EUR", "BG": "EUR",
    "MC": "EUR", "ME": "EUR",
    # Europe — other
    "GB": "GBP", "CH": "CHF", "NO": "NOK", "SE": "SEK", "DK": "DKK", "IS": "ISK", "PL": "PLN",
    "CZ": "CZK", "HU": "HUF", "RO": "RON", "RS": "RSD", "RU": "RUB", "UA": "UAH", "GE": "GEL",
    # Africa
    "EG": "EGP", "MA": "MAD", "ZA": "ZAR", "KE": "KES", "TZ": "TZS", "MU": "MUR",
}


# --------------------------------------------------------------------------- data access


@lru_cache(maxsize=1)
def _airports() -> dict[str, Airport]:
    rows: list[dict[str, Any]] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {row["code"]: Airport(**row) for row in rows}


@lru_cache(maxsize=1)
def _metro_by_airport() -> dict[str, Metro]:
    return {code: metro for metro in _METROS for code in metro.airports}


def _norm(code: str) -> str:
    return code.strip().upper()


def _fold(text: str) -> str:
    """Case- and accent-insensitive form for matching (``São`` -> ``sao``)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _clean_city(city: str) -> str:
    return city.split(" (")[0].strip()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def get_airport(code: str) -> Airport | None:
    """The airport with IATA ``code``, or ``None``."""
    return _airports().get(_norm(code))


def is_metro(code: str) -> bool:
    """True for a multi-airport city code (``SEL``, ``TYO``, ``BKK``, ``SPK``)."""
    metro = METRO_CODES.get(_norm(code))
    return metro is not None and metro.airports != (metro.code,)


def airports_for(code: str) -> list[str]:
    """Airports to search for ``code``: a metro's airports, else ``[code]`` itself."""
    normalized = _norm(code)
    metro = METRO_CODES.get(normalized)
    return list(metro.airports) if metro else [normalized]


def metro_for_airport(code: str) -> str | None:
    """City code an airport belongs to (``NRT`` -> ``TYO``), or ``None``."""
    metro = _metro_by_airport().get(_norm(code))
    return metro.code if metro else None


def nearby_airports(code: str, radius_km: float = 100.0) -> list[str]:
    """Other airports near ``code`` (airport or metro), nearest first.

    Excludes ``airports_for(code)`` itself; for an airport, same-metro siblings are always
    included regardless of distance (``nearby_airports("NRT")`` contains ``HND``).
    """
    normalized = _norm(code)
    own = airports_for(normalized)
    anchors = [a for a in (get_airport(c) for c in own) if a is not None]
    if not anchors:
        return []
    siblings: set[str] = set()
    metro_code = metro_for_airport(normalized)
    if not is_metro(normalized) and metro_code:
        siblings = set(METRO_CODES[metro_code].airports) - set(own)

    found: list[tuple[float, str]] = []
    for airport in _airports().values():
        if airport.code in own:
            continue
        distance = min(haversine_km(a.lat, a.lon, airport.lat, airport.lon) for a in anchors)
        if distance <= radius_km or airport.code in siblings:
            found.append((distance, airport.code))
    return [c for _, c in sorted(found)]


def city_info(code: str) -> dict[str, Any] | None:
    """``{city, country, currency, lat, lon}`` for an airport or metro code (metro = city centre)."""
    normalized = _norm(code)
    metro = METRO_CODES.get(normalized) or _metro_by_airport().get(normalized)
    if metro is not None:
        city, country, lat, lon = metro.name, metro.country, metro.lat, metro.lon
    else:
        airport = get_airport(normalized)
        if airport is None:
            return None
        city, country = _clean_city(airport.city) or airport.name, airport.country
        lat, lon = airport.lat, airport.lon
    return {
        "city": city,
        "country": country,
        "currency": COUNTRY_CURRENCY.get(country),
        "lat": lat,
        "lon": lon,
    }


# --------------------------------------------------------------------------- search


def _metro_entry(metro: Metro) -> dict[str, Any]:
    return {
        "code": metro.code,
        "name": f"{metro.name} (all airports)",
        "city": metro.name,
        "city_ko": _korean_alias(metro.aliases),
        "city_code": metro.code,
        "country": metro.country,
        "lat": metro.lat,
        "lon": metro.lon,
        "is_metro": True,
    }


def _korean_alias(aliases: tuple[str, ...]) -> str | None:
    """First alias, the Korean city name (``None`` when there is none)."""
    return aliases[0] if aliases else None


def airport_entry(code: str) -> dict[str, Any] | None:
    """``AirportOut``-shaped dict for an airport or metro code, or ``None`` if unknown."""
    normalized = _norm(code)
    if is_metro(normalized):
        return _metro_entry(METRO_CODES[normalized])
    airport = get_airport(normalized)
    if airport is None:
        return None
    metro = _metro_by_airport().get(airport.code)
    return {
        "code": airport.code,
        "name": airport.name,
        "city": metro.name if metro else _clean_city(airport.city),
        "city_ko": _korean_alias(metro.aliases if metro else AIRPORT_ALIASES.get(airport.code, ())),
        "city_code": metro.code if metro else None,
        "country": airport.country,
        "lat": airport.lat,
        "lon": airport.lon,
        "is_metro": False,
    }


def _text_score(query: str, texts: list[str], starts: float, contains: float) -> float | None:
    folded = [_fold(t) for t in texts if t]
    if any(t.startswith(query) for t in folded):
        return starts
    if any(query in t for t in folded):
        return contains
    return None


def _best(*scores: float | None) -> float | None:
    present = [s for s in scores if s is not None]
    return min(present) if present else None


def search(q: str, limit: int = 20) -> list[dict[str, Any]]:
    """Autocomplete over airports and metro codes (``AirportOut``-shaped dicts, metros first)."""
    query = _fold(q.strip())
    if not query:
        return []
    code_query = query.upper()
    ranked: dict[str, tuple[float, int, str]] = {}
    metro_scores: dict[str, float] = {}

    for metro in _METROS:
        score = 0.0 if metro.code == code_query else _text_score(
            query, [metro.name, *metro.aliases], 1.0, 3.0
        )
        if score is None:
            continue
        metro_scores[metro.code] = score
        if is_metro(metro.code):
            ranked[metro.code] = (score, 0, metro.code)

    for airport in _airports().values():
        if airport.code in ranked:
            continue  # code already listed as a metro (e.g. BKK)
        metro = _metro_by_airport().get(airport.code)
        score = _best(
            0.0 if airport.code == code_query else None,
            1.5 if len(code_query) < 3 and airport.code.startswith(code_query) else None,
            _text_score(
                query,
                [airport.city, airport.name, *AIRPORT_ALIASES.get(airport.code, ())],
                2.0,
                4.0,
            ),
            metro_scores[metro.code] + 0.5 if metro and metro.code in metro_scores else None,
        )
        if score is not None:
            ranked[airport.code] = (score, 1, airport.code)

    ordered = sorted(ranked.values())[: max(limit, 0)]
    return [entry for _, _, code in ordered if (entry := airport_entry(code)) is not None]
