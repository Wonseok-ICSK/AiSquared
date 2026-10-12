# AiSquared Travel — Design Contract

Personal, locally-run flight price monitor + AI travel planner (Windows 11, Korea).
This document is the **contract** every module is built against. If code and this
document disagree, fix one of them deliberately — do not drift silently.

---

## 1. Stack and runtime

| Concern | Choice |
|---|---|
| Backend | Python 3.13, FastAPI, Uvicorn, SQLAlchemy 2.x (sync ORM, `Mapped[]` typing), Pydantic v2, pydantic-settings |
| DB | SQLite (WAL) at `data/app.db` — schema kept PostgreSQL-compatible (no SQLite-only SQL) |
| Scheduler | APScheduler 3.x `BackgroundScheduler` in-process (timezone `Asia/Seoul`) |
| Browser automation | Playwright **sync** API, default channel `msedge` (installed Edge), fallback `chrome` → bundled `chromium` |
| LLM | Anthropic Python SDK 1.x (`anthropic`), default model `claude-opus-5-5` |
| HTTP | `httpx` (sync) for provider/third-party calls |
| Frontend | React + Vite + TypeScript SPA in `frontend/`; built `frontend/dist` served by FastAPI at `/` |
| UI language | Korean (default) or English, switched from the top-bar menu and stored in `localStorage["ast-lang"]`. Strings are inline pairs `tr("English", "한국어")` (`frontend/src/lib/i18n.tsx`); switching remounts the tree. Separate from the backend `language` setting (notification/itinerary text) |
| UI look | Sidebar + top bar, warm paper background, forest-green accent, DM Sans / Manrope (Google Fonts) with Korean system fallbacks — modelled on the sibling Wayfinder sample's design. City photos in `frontend/public/images/` are Unsplash photos (`photo-1540959733332-eab4deabeeaf` Tokyo, `photo-1590559899731-a382839e5549` Osaka) |
| Venv | `C:\Users\User\Desktop\AiSquared\travel-planner\.venv` (`.venv\Scripts\python.exe`) |
| Location | Everything lives in `C:\Users\User\Desktop\AiSquared\travel-planner\`. The parent folder `AiSquared\` hosts an unrelated Node app ("Wayfinder") built by another agent — never touch files outside `travel-planner\` |
| Ports | Backend `127.0.0.1:8765` (8000 is taken on this machine); Vite dev `5173` proxying `/api` → 8765 |

Everything backend-side is **synchronous** (providers, services, scheduler jobs). FastAPI
endpoints that do slow work (searches, itinerary generation) start a background thread and
return immediately; the UI polls.

## 2. Repository layout

```
AiSquared/travel-planner/       # "repo root" of this app (config.REPO_ROOT)
  DESIGN.md  README.md  .env.example  .gitignore  .venv/
  data/                         # runtime (gitignored): app.db, browser_profiles/, sessions/, logs/
  scripts/                      # run.ps1, dev.ps1, install-startup-task.ps1, build_airports.py
  backend/
    requirements.txt  pytest.ini
    app/
      main.py                   # create_app(): routers, lifespan (init_db, scheduler), static SPA
      config.py                 # Settings (env/.env), get_settings(), paths
      db.py                     # engine, SessionLocal, Base, init_db(), session_scope(), get_db()
      models.py                 # ORM models (§4)
      schemas.py                # Pydantic API schemas (§6) — the frontend mirrors these
      timeutil.py               # utcnow(), to_utc_iso(), local tz helpers
      settings_store.py         # RuntimeSettings load/save, secret encryption, env fallback (§8)
      security/secrets.py       # SecretBox (Fernet key in Windows Credential Manager via keyring)
      core/airports.py          # airport/metro lookup + data/airports.json
      core/fx.py                # currency conversion (Frankfurter API + cache + static fallback)
      browser/manager.py        # BrowserManager: dedicated Playwright thread (§9)
      browser/sessions.py       # encrypted storage_state per provider, interactive login
      providers/flights/base.py # FlightProvider ABC + dataclasses (§5)
      providers/flights/registry.py
      providers/flights/*.py    # concrete providers
      providers/accommodation/base.py   # future hotel providers (interface only)
      services/search.py        # query expansion, provider fan-out, normalization, filtering
      services/monitoring.py    # run_trip_check()
      services/alerts.py        # evaluate_alerts()
      services/scheduler.py     # MonitorScheduler
      services/notifications/   # base, formatter, inapp, desktop, email, kakao, telegram, dispatcher
      services/itinerary/       # models, candidates, routing, planner, llm, weather, budget, service
      api/                      # routers (§7)
    tests/                      # pytest; fixtures/ holds captured provider payloads
  frontend/                     # Vite React TS app
```

Import style: absolute (`from app.models import Trip`). Run backend from `backend/`:
`..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765`.

## 3. Conventions

* **Datetimes**: stored as *naive UTC* in DB (`timeutil.utcnow()` returns naive UTC). API
  serializes them as ISO-8601 with `Z`. Flight times are **airport-local naive** ISO strings
  (`"2026-11-20T09:05"`) and are never converted.
* **Money**: `float`, major currency units (KRW/JPY have no minor unit). Every amount travels
  with a `currency` code. No implicit FX: comparisons only between same-currency values;
  use `core.fx.convert()` explicitly (budget summaries).
* **Price basis**: `"per_person"` or `"total"`. Offers always carry both `price_total`
  and `price_per_person` (= total / adults). Trips, alert rules and budgets declare a basis.
* **Budget "unlimited"**: `None` = not specified; `0` or `-1` = unlimited ("search in a
  reasonable range" — planner uses a sensible mid-range default); positive = cap. Any other
  negative value is rejected (422).
* **Enums** are lowercase strings (see §6). Validate with `Literal[...]` in schemas.
* **Secrets** are never stored or logged in plain text (§8). API responses redact them.
* **Logging**: `logging.getLogger(__name__)`; never log secrets/cookies.
* **Tests**: pytest, no network by default. Live tests are marked `@pytest.mark.live` and
  skipped unless `RUN_LIVE=1`. Use an in-memory/temp SQLite per test (`conftest.py`).

## 4. Data model (`app/models.py`)

All tables have integer `id` PK unless noted; `created_at`/`updated_at` naive UTC.
JSON list/dict columns are `MutableList`/`MutableDict` and hold `[]`/`{}` from object
construction on (safe to mutate before the first flush); nullable JSON columns
(`FlightOffer.inbound`, `Itinerary.plan`) store `None` as SQL `NULL`.

**Trip** — a monitored trip
- `name: str`
- `origin: str`, `destination: str` — IATA airport (`ICN`) or metro code (`SEL`, `TYO`)
- `trip_type: "round_trip" | "one_way"`
- `depart_date: date`, `return_date: date | None` (required when round_trip)
- `adults: int` (1–2 per spec)
- `cabin: "economy" | "premium_economy" | "business" | "first"`
- `direct_only: bool`, `max_stops: int | None` (ignored when direct_only)
- time windows, each `str | None` `"HH:MM"`: `outbound_depart_from/to`, `outbound_arrive_from/to`,
  `return_depart_from/to`, `return_arrive_from/to`
- `max_price: float | None` (offers above are excluded), `target_price: float | None`,
  `price_basis: "per_person" | "total"`, `currency: str` (API default `DEFAULT_CURRENCY`, §8)
- `flex_depart_days: int` (0/1/3/7), `flex_return_days: int` (0/1/3/7)
- `include_nearby_airports: bool`, `alt_destinations: list[str]` (JSON)
- `providers: list[str]` (JSON; empty ⇒ runtime setting `default_providers` if non-empty, else every
  provider with `enabled_by_default` — never `mock`; unconfigured providers are skipped. See
  `registry.resolve_provider_keys`, §5)
- `schedule_mode: "times" | "interval"`, `schedule_times: list[str]` (JSON `["08:30","17:30"]`),
  `schedule_interval_hours: float | None`
- `active: bool`, `notes: str | None`
- `last_checked_at`, `last_status: "ok" | "partial" | "error" | None`, `last_error: str | None`
- relationships: `runs`, `offers`, `alert_rules`, `notifications`, `preferences` (1–1), `itineraries`
  (all `cascade="all, delete-orphan"`)

**SearchRun** — one monitoring execution
- `trip_id`, `trigger: "schedule" | "manual"`, `started_at`, `finished_at`
- `status: "running" | "ok" | "partial" | "error"`, `error: str | None`
- `combinations_total`, `combinations_searched` (ints), `offers_found`, `offers_matching`
- `min_price_total`, `min_price_per_person: float | None` (over *matching* offers), `currency`
- `best_offer_id: int | None`
- `provider_stats: dict` (JSON) `{provider_key: {"status": "ok|error|skipped", "offers": n, "error": str|None, "duration_s": float, "queries": n}}`
- `log: list[str]` (JSON) — human-readable notes incl. anything skipped/capped

**FlightOffer** — one observed price (price-history row)
- `trip_id`, `run_id`, `searched_at`, `provider` (source key), `seller: str | None`
- `offer_key: str` (indexed; §5), `origin`, `destination` (actual airports), `depart_date`, `return_date`
- spec fields (flattened, outbound leg): `airline` (display names joined ", "), `flight_number`
  (outbound flight numbers joined " / "), `departure_airport`, `arrival_airport`,
  `departure_time`, `arrival_time` (local ISO), `duration_minutes`, `stops` (max over legs)
- `outbound: dict`, `inbound: dict | None` (JSON-serialized `FlightLeg`)
- `cabin`, `fare_type: str | None`, `baggage: str | None`
- `price_total`, `price_per_person`, `currency`
- `previous_price: float | None` — previous observation of the same (`offer_key`, `provider`) for this trip (per-person)
- `lowest_price: float | None` — lowest ever per-person for (`offer_key`, `provider`), including this one
- `booking_url`, `source_url: str | None`, `matches_filters: bool`, `raw: dict` (trimmed)

**AlertRule**
- `trip_id`, `kind: "below_target" | "price_drop" | "new_low"`, `enabled: bool`
- `threshold: float | None` (below_target; `None` ⇒ use trip.target_price)
- `drop_percent: float | None` (price_drop, e.g. 10.0)
- `price_basis: "per_person" | "total"`
- `armed: bool = True`, `last_triggered_at`, `last_triggered_price: float | None`

**Notification**
- `trip_id: int | None`, `alert_rule_id: int | None`, `kind: "price_alert" | "itinerary" | "system"`
- `title`, `body` (plain text), `url: str | None`, `data: dict` (JSON)
- `read_at: datetime | None`, `channel_results: dict` (JSON) `{channel: "sent" | "skipped" | "failed: <reason>"}`

**TravelPreference** (PK/FK `trip_id`, 1–1)
- `style_mode: "percent" | "score"`
- `style_weights: dict[str, float]` keys exactly: `sightseeing, food, relaxation, hotel_resort,
  shopping, nightlife, nature, photography, culture, local_experience`
  (percent: 0–100 summing ≤ 100, default 10 each; score: 0–5, default 3 each — the default
  follows `style_mode` when weights are omitted)
- `companion: "solo" | "couple" | "family"`, `pace: "relaxed" | "moderate" | "packed"`
- `food_wishlist: list[str]`, `preferred_cuisines: list[str]`, `avoid_foods: list[str]`
- `restaurant_budget_min / _max: float | None`, `restaurant_budget_currency: str | None` (e.g. "JPY")
- `hotel_area: str | None`, `must_visit: list[str]`, `avoid: list[str]`, `notes: str | None`
- budget: `budget_basis: "per_person" | "total"`, `budget_currency: str` (API default `DEFAULT_CURRENCY`),
  `budget_total: float` (required), `budget_flight: float` (required),
  `budget_hotel, budget_food, budget_transport, budget_activities, budget_shopping, budget_other: float | None`

**Itinerary**
- `trip_id`, `status: "pending" | "running" | "done" | "error"`, `error`, `progress: str | None`
- `offer_id: int | None`, `arrival_at`, `departure_at` (destination-local ISO; arrival at destination, departure from destination),
  `arrival_airport`, `departure_airport`, `hotel_area`
- `generator: "claude" | "rule_based"`, `model: str | None`
- `request: dict` (inputs snapshot), `plan: dict | None` (`ItineraryPlan` JSON, §10)
- `parent_id: int | None` (refinement chain), `feedback: str | None`, `usage: dict` (tokens, web searches, est. USD)

**ProviderSession** (PK `provider: str`) — `status: "none" | "active" | "expired"`, `storage_path`, `account_hint`, `updated_at`

**AppSetting** (PK `key: str`) — `value: dict` (JSON), `updated_at`

**GeocodeCache** (PK `query: str`, normalized "name|city|country") — `lat`, `lon: float | None`,
`display_name: str | None`, `source: "nominatim" | "llm" | "none"`, `created_at` (owned by the itinerary module;
add it to `models.py` if missing)

## 5. Flight provider abstraction (`app/providers/flights/base.py`)

```python
CabinClass = Literal["economy", "premium_economy", "business", "first"]

@dataclass(frozen=True)
class FlightQuery:
    origin: str                      # IATA airport or metro code
    destination: str
    depart_date: date
    return_date: date | None         # None => one-way
    adults: int = 1
    cabin: CabinClass = "economy"
    direct_only: bool = False
    max_stops: int | None = None
    currency: str = "KRW"
    @property
    def is_round_trip(self) -> bool: ...

@dataclass
class FlightSegment:
    carrier_code: str | None; carrier_name: str | None; flight_number: str | None  # "KE703"
    departure_airport: str; arrival_airport: str
    departure_time: str; arrival_time: str            # local ISO "YYYY-MM-DDTHH:MM"
    duration_minutes: int | None = None
    operating_carrier: str | None = None; aircraft: str | None = None

@dataclass
class FlightLeg:
    departure_airport: str; arrival_airport: str
    departure_time: str; arrival_time: str            # local ISO
    duration_minutes: int | None
    stops: int
    airlines: list[str]                               # display names
    flight_numbers: list[str]                         # may be empty if provider lacks them
    segments: list[FlightSegment] = field(default_factory=list)
    layovers: list[str] = field(default_factory=list) # airport codes

@dataclass
class FlightOffer:
    provider: str
    outbound: FlightLeg
    inbound: FlightLeg | None
    price_total: float
    price_per_person: float
    currency: str
    cabin: CabinClass
    adults: int = 1
    fare_type: str | None = None
    baggage: str | None = None
    seller: str | None = None
    booking_url: str | None = None
    source_url: str | None = None
    provider_offer_id: str | None = None
    raw: dict = field(default_factory=dict)
    def offer_key(self) -> str: ...
    def to_dict(self) -> dict: ...   # JSON-safe (asdict)

@dataclass
class ProviderStatus:
    configured: bool
    message: str = ""

class ProviderError(Exception): ...
class ProviderNotConfigured(ProviderError): ...
class ProviderBlocked(ProviderError): ...   # captcha / anti-bot / consent wall

@dataclass
class ProviderContext:
    get_secret: Callable[[str], str | None]   # e.g. get_secret("serpapi")
    browser: "BrowserManager | None"
    http_timeout: float = 30.0

class FlightProvider(ABC):
    key: ClassVar[str]; display_name: ClassVar[str]; description: ClassVar[str] = ""
    requires_api_key: ClassVar[bool] = False
    api_key_name: ClassVar[str | None] = None       # secret name in settings_store
    uses_browser: ClassVar[bool] = False
    supports_login: ClassVar[bool] = False
    supports_metro_codes: ClassVar[bool] = False    # accepts SEL/TYO directly
    max_queries_per_run: ClassVar[int] = 12         # cost/latency cap hint
    enabled_by_default: ClassVar[bool] = True       # False for mock / opt-in providers
    def __init__(self, ctx: ProviderContext): self.ctx = ctx
    def status(self) -> ProviderStatus: ...
    @abstractmethod
    def search_flights(self, query: FlightQuery) -> list[FlightOffer]: ...
    def get_flight_details(self, offer: FlightOffer) -> FlightOffer: return offer
    def get_price(self, offer: FlightOffer) -> float | None: ...   # default: re-search, match offer_key
    def get_booking_url(self, offer: FlightOffer) -> str | None: return offer.booking_url or offer.source_url
```

`FlightOffer.return_date: str | None` ("YYYY-MM-DD") is set for round trips whose inbound leg details
are unknown (Google Flights lists outbound options priced at the round-trip total); then
`IN` = `"RT:<return_date>"` in the offer key. Providers may fill the inbound leg later via
`get_flight_details()` (monitoring calls it best-effort for the best offer when an alert fires).

**offer_key** = `"{OUT}|{IN}|{cabin}"` where each leg part is
`"{dep_airport}-{arr_airport}-{dep_local_iso}-{flight_numbers joined '+'}"` (upper-case; when flight
numbers are missing use `airlines joined '+'` instead). One-way ⇒ `IN` = `"-"`. Same flight seen
again tomorrow (or from another provider) yields the same key.

`registry.py`: `register(cls)`, `get_provider_class(key)`, `list_provider_classes()`,
`create_provider(key, ctx)`, `default_provider_keys(default_providers=())` (runtime
`default_providers` if non-empty, else every provider with `enabled_by_default`) and
`resolve_provider_keys(trip_providers, default_providers=())` (trip list if non-empty, else the
defaults; unknown keys dropped with a warning). Callers still skip providers whose
`status().configured` is False. Providers self-register on import of `app.providers.flights`.
Concrete providers (finalised in §5a after research): `mock` (deterministic fake data, dev/tests,
`enabled_by_default = False`: runs only when a trip or `default_providers` names it), plus real ones.

### 5a. Concrete flight providers
See §13 (filled in after research).

## 6. API schemas (`app/schemas.py`) — summary

Pydantic v2 models. Every response model (`*Out`, `ProviderInfo`, `ItineraryPlan` and the models
nested in them) derives from `OutModel` (`ConfigDict(from_attributes=True)`), so it validates from
ORM rows, provider dataclasses or dicts.
Naming: `TripCreate`, `TripUpdate` (all optional; `merged_with(current) -> TripCreate` re-validates
the merged trip and regenerates an auto-generated name `"ORIGIN → DESTINATION"` when the route
changes), `TripOut` (+ computed: `latest_min_price`,
`latest_min_price_basis_value` (per trip basis), `lowest_ever`, `next_run_at`, `unread_alerts`),
`SearchRunOut`, `FlightOfferOut`, `PriceHistoryOut` (`points: [{run_id, checked_at, min_total,
min_per_person, by_provider: {key: min_per_person}}]`), `OfferHistoryOut`, `AlertRuleCreate/Update/Out`
(`AlertRuleUpdate.merged_with(current) -> AlertRuleCreate` re-validates kind fields; `armed` applied separately),
`NotificationOut`, `PreferencesIn/Out`, `ItineraryCreate` (`offer_id | manual arrival_at/departure_at`,
`hotel_area`, `notes`, `use_ai: bool = True`), `ItineraryRefine` (`feedback`), `ItineraryOut`,
`ProviderInfo` (`key, display_name, description, requires_api_key, configured, enabled_by_default,
enabled, uses_browser, supports_login, session_status, message`; `enabled` = used for trips without
an explicit provider list), `RuntimeSettingsOut/In` (`llm_model` blank = reset to env), `AirportOut`
(`code, name, city, city_ko, city_code, country, lat, lon, is_metro`; `city_ko` = Korean city name for popular cities, else null), `SchedulerJobOut`, `MetaOut`
(enums + style keys + defaults incl. `style_weights` (percent) and `score_style_weights`). Validation
lives in schemas (e.g. round_trip ⇒ return_date ≥ depart_date; adults 1–2; times "HH:MM";
flex ∈ {0,1,3,7}; origin/destination must not share an airport via metro codes (`SEL`/`ICN`);
`alt_destinations` drops codes overlapping the origin or already covered by the destination).

## 7. REST API (prefix `/api`)

| Method & path | Purpose |
|---|---|
| GET `/health` | `{status:"ok", version}` |
| GET `/meta` | enums, style keys, cabin list, defaults, providers summary |
| GET `/airports?q=` | autocomplete (airports + metro codes), max 20 |
| GET/POST `/trips`; GET/PUT/DELETE `/trips/{id}` | trip CRUD (POST also creates default alert rules from `target_price`: `below_target` + `new_low`) |
| POST `/trips/{id}/check` | start a manual check in background → `SearchRunOut` (status running) |
| GET `/trips/{id}/runs?limit=` ; GET `/runs/{run_id}` | run list / detail (poll this) |
| GET `/trips/{id}/offers?run_id=&only_matching=true&sort=price|departure|duration&limit=` | offers of a run (default latest finished run) |
| GET `/trips/{id}/price-history` | lowest price per run (+ per provider) |
| GET `/trips/{id}/offers/{offer_id}/history` | history for that offer_key |
| GET `/offers/{offer_id}/book` | "Book" button: resolves the exact booking-options page (both flights, dates, passengers; one extra provider lookup for round trips with unknown return leg, cached on the row) and 302-redirects; falls back to the stored link |
| GET/POST `/trips/{id}/alerts`; PUT/DELETE `/alerts/{id}` | alert rules |
| GET `/notifications?unread_only=&limit=`; GET `/notifications/unread-count`; POST `/notifications/{id}/read`; POST `/notifications/read-all` | in-app inbox |
| GET/PUT `/trips/{id}/preferences` | travel style + budget |
| POST `/trips/{id}/itineraries` → `ItineraryOut` (pending, generated in background); GET `/trips/{id}/itineraries`; GET/DELETE `/itineraries/{id}`; POST `/itineraries/{id}/refine` | itinerary |
| GET/PUT `/settings`; POST `/settings/notifications/test` `{channel}` → `{channel, result}` | runtime settings (secrets redacted) |
| POST `/settings/telegram/detect-chat` → `{chat_id, username}` | read `getUpdates` after the user sent `/start` |
| GET `/providers`; POST `/providers/{key}/test` (small live search, background) ; POST `/providers/{key}/login`; DELETE `/providers/{key}/session` | providers + login sessions |
| GET `/integrations/kakao/authorize` (302 to Kakao); GET `/integrations/kakao/callback?code=` | KakaoTalk OAuth |
| GET `/scheduler/jobs` | next run times per trip |

Errors: `HTTPException` with `{"detail": "..."}`; 404 for missing ids and unknown `/api/*` paths
(any method; 405 only for a wrong method on an existing path); 422 validation.
Routers: every `app/api/<name>.py` defining `router = APIRouter()` (paths relative to `/api`) is
discovered and mounted automatically (`_`-prefixed modules and modules without `router` are skipped).

## 8. Settings & secrets

* `config.Settings` (pydantic-settings, `travel-planner/.env`, prefix none): `HOST`, `BACKEND_PORT=8765`
  (field `port`; **not** `PORT`, which the repo's Node app reads from the same `.env`),
  `DATA_DIR`, `DATABASE_URL`, `TIMEZONE=Asia/Seoul`, `DEFAULT_CURRENCY=KRW` (default currency of new
  trips and budgets in `TripCreate`/`PreferencesIn`),
  `BROWSER_CHANNEL=msedge`, `BROWSER_HEADLESS=true`, `SCHEDULER_ENABLED=true`,
  `ANTHROPIC_API_KEY`, `LLM_MODEL=claude-opus-5-5`, `LLM_EFFORT=high`, `SERPAPI_API_KEY`,
  `GOOGLE_MAPS_API_KEY`, `SMTP_*`, `KAKAO_*`, `TELEGRAM_*`, `MAX_SEARCH_COMBINATIONS=12`.
* `settings_store.RuntimeSettings` (UI-editable, persisted in `AppSetting["runtime"]`) overrides env.
  `default_providers: list[str]` — provider keys used for trips with an empty `providers` list;
  empty ⇒ every provider with `enabled_by_default` (§5).
  `provider_options: dict[str, dict]` → passed as `ProviderContext.options` (e.g.
  `{"google_flights": {"fetch_mode": "browser" | "http"}, "naver": {"show_window": false}}`).
  `language: "en" | "ko"` (default "ko"; the UI itself is Korean) — itinerary + notification text language.
  `notifications.ntfy = {enabled, server, topic, token*}`. Secret names for `get_secret`: `anthropic`,
  `serpapi`, `google_maps`, `smtp_password`, `kakao_rest_api_key`, `kakao_client_secret`,
  `kakao_access_token`, `kakao_refresh_token`, `telegram_bot_token`, `ntfy_token`.
  Secret fields are encrypted individually with `SecretBox` (`"enc:v1:<fernet token>"`).
  `get_secret(name)` resolves UI value → env var → None. `GET /settings` returns secrets as
  `{"is_set": bool}`; on `PUT`, a secret omitted/`null` = unchanged, `""` = clear.
* `SecretBox`: Fernet key stored in Windows Credential Manager via `keyring`
  (service `AiSquaredTravel`, user `secretbox-key`); if keyring is unavailable fall back to a key
  file protected with Windows DPAPI (`win32crypt` if available) — never a plain key file without
  a logged warning. Browser `storage_state` files are Fernet-encrypted on disk.

## 9. Browser automation (`app/browser/`)

* `BrowserManager` owns **one dedicated worker thread** running Playwright sync API
  (Playwright objects are thread-bound). Public API:
  `run(fn: Callable[[BrowserHandle], T], timeout: float) -> T` (executes on the worker thread),
  `shutdown()`. `BrowserHandle.new_context(profile: str | None = None, headless: bool | None = None,
  storage_state: dict | None = None) -> BrowserContext` (persistent profile under
  `%LOCALAPPDATA%\AiSquaredTravel\browser_profiles\<profile>` when profile given — see §13.2). Channel order: settings channel
  (`msedge`) → `chrome` → bundled `chromium`; the channel actually used is logged and exposed.
* Windows event loops: per verified research (§13.2) a plain worker thread running the **sync**
  API works under uvicorn ≥ 0.36 even with `--reload`; never use async Playwright.
* Flight-parsing logic lives in the provider modules as **pure functions** over captured
  JSON/HTML (unit-tested with fixtures); navigation lives in a thin `_fetch_*` function using
  the BrowserManager. Browser automation is thereby separate from flight-search logic.
* `sessions.py`: `start_login(provider_key, url)` opens a **headed** persistent context, waits
  for the user to finish (window closed or timeout), then saves the encrypted `storage_state`
  and marks `ProviderSession.status = active`. `load_storage_state(provider_key)` decrypts.

## 10. Itinerary pipeline (`app/services/itinerary/`)

1. **Inputs** (`PlanInputs`): destination city/country/currency, arrival/departure local datetimes,
   airports, adults, preferences, budget (remaining after flight), hotel area, weather.
2. **Candidates** (`CandidateSource` implementations, merged & de-duplicated):
   `ClaudeWebResearchSource` (Claude + server web search; facts with source URLs),
   `GooglePlacesSource` (if `google_maps` key), `OverpassSource` (OSM, free fallback).
   Each `Candidate` carries category, styles served, area, lat/lng, visit minutes, opening hours,
   best time of day, cost estimate (local currency), rating (+source), cuisine, source URLs.
3. **Deterministic planner** (`routing.py` + `planner.py`): score candidates by style weights and
   wishlists; compute available hours per day (arrival/departure, pace, hotel check-in);
   cluster candidates geographically into day-areas (balanced k-means/k-medoids on lat/lng);
   order within a day by nearest-neighbour + 2-opt with opening-hours/time-of-day feasibility;
   insert meals (restaurants near the route at meal times, within restaurant budget);
   estimate travel minutes from haversine distance (urban transit model: walk ≤1 km at 4.5 km/h,
   else transit avg ~20 km/h + 8 min overhead); always produces a valid `ItineraryPlan`.
4. **Claude composition** (optional): Claude receives facts + the draft plan and returns the final
   plan (structured output, same schema) — may reorder within reason, must keep geographic
   grouping, realistic travel times and budget. Validated by Pydantic + route checker; warnings attached.
5. **Budget summary** converts local costs to the budget currency via `core.fx`.
6. **Refine**: user feedback → Claude revises the plan (or rule-based tweaks when no key).

`ItineraryPlan` JSON: `{title, summary, destination, currency_local, days: [{date, theme, areas: [],
items: [{start, end, kind: arrival|transit|hotel|activity|meal|free|departure, title, area, lat, lng,
description, est_cost, cost_currency, travel_from_prev: {mode, minutes, distance_km} | null,
tips, map_url, source_urls: [], candidate_id}] , est_day_cost, weather}], budget: {currency,
lines: [{category, budget, estimated, unlimited}], total_budget, total_estimated, within_budget,
notes}, warnings: [], sources: [{title, url}], generator, route_metrics: {total_travel_minutes,
avg_hop_km, max_hop_km, backtrack_hops}}`.

LLM rules (from the claude-api skill): model from settings (default `claude-opus-5-5`);
never send `budget_tokens`/`thinking: disabled`/`temperature`; set `output_config.effort`
explicitly; use `client.beta.messages` with `betas=["server-side-fallback-2026-07-01"]` and
`fallbacks="default"`; check `stop_reason` (`refusal`, `max_tokens`, `pause_turn`) before reading
content; stream (`.stream(...).get_final_message()`) for long outputs; no assistant prefill; no
forced `tool_choice` (`auto` + instruction); handle typed SDK errors most-specific-first.

## 11. Monitoring & alerts

`run_trip_check(trip_id, trigger)`: per-trip lock (skip if already running) → create SearchRun →
`services.search.search_trip()` (expands flexible dates/airports with a cap — capped/skipped combos are
logged into `run.log`) → persist offers (+`previous_price`/`lowest_price`) → compute best matching
offer → finish run (`ok` if all providers ok, `partial` if some failed, `error` if all failed or no
provider) → `evaluate_alerts()` → `dispatcher.notify()` per event.

Alert semantics (`price` = best matching offer price in the rule's basis, same currency):
* `below_target`: fire when `price <= threshold` and (`armed` or `price < last_triggered_price`);
  then `armed=False`. Re-arm when a later run has `price > threshold`.
* `price_drop`: fire when previous finished run's best price `p0` exists and `(p0 - price)/p0*100 >= drop_percent`.
* `new_low`: fire when at least one earlier finished run exists and `price < min(previous runs' best)`.
Shared data objects: `app/services/events.py` (`AlertEvent`, `NotificationMessage`) — already written.
Interfaces: `services.alerts.evaluate_alerts(db, trip, run) -> list[AlertEvent]`;
`services.notifications.formatter.format_price_alert(trip, run, best_offer, events) -> NotificationMessage`;
`services.notifications.dispatcher.notify(db, *, kind, message, trip_id=None, alert_rule_id=None) -> Notification`;
`services.notifications.dispatcher.notify_price_alert(db, trip, run, best_offer, events) -> Notification | None`
(monitoring calls this after evaluating alerts).
One run can fire several rules; the dispatcher merges them into one notification per trip per run
(title lists reasons). Message format follows the spec example (route, dates, previous lowest,
current, target, verdict line, booking link).

## 12. Notifications

Channels (`NotificationChannel.key`): `inapp` (always; the DB row itself), `desktop` (Windows toast,
click opens URL), `telegram`, `email` (SMTP STARTTLS), `ntfy`, `kakao` (KakaoTalk "send to me") — see §13.3.
`dispatcher.notify(db, trip, kind, message, alert_rule_id=None) -> Notification` sends to every
enabled+configured channel; one failure never blocks others; results stored in `channel_results`.

## 13. Provider/integration decisions (from verified research, 2026-10-06)

Full research notes (verified by an independent fact-check pass) live in `docs/research/*.md`.
Read the relevant file before implementing a provider/integration.

### 13.1 Flight sources

| key | Status | Notes |
|---|---|---|
| `google_flights` | **default**, no key | Google Flights page `https://www.google.com/travel/flights?tfs=<b64 protobuf>&tfu=EgQIABABIgA&hl=en&gl=KR&curr=KRW` (base path is not robots-disallowed; `/travel/flights/search` and `/booking` are — those are only for user-facing links). Own **tfs encoder** (hand-rolled protobuf varint writer, no protobuf dep) + own **ds:1 JSON parser** (index map in `docs/research/google-flights-browser.md`; reference implementation of fast-flights master in scratch `fcgf/ffm/`), aria-label DOM fallback. Price = "round trip total for **all** passengers" in KRW. Skip unpriced rows (`k[1][0] == []`). Two fetch modes: `browser` (default; real Edge via BrowserManager, headless, read `page.content()`) and `http` (optional setting; httpx GET). Detect blocks (HTTP 429, `/sorry/`, `consent.google.com`, missing `script.ds:1`) → `ProviderBlocked`; never solve captchas or rotate IPs. ≥5 s jittered spacing between queries. User-facing link: `/travel/flights/search?tfs=...&hl=ko&gl=KR&curr=KRW`. |
| `naver` | **opt-in** (off by default; UI must show ToS warning: Naver terms forbid automated collection; personal low-volume use only) | Real Edge, **headed** window placed off-screen (`--window-position=-32000,-32000`) because `flight-api.naver.com` rejects HeadlessChrome UA. Navigate to `https://flight.naver.com/flights/international/{O}-{D}-{YYYYMMDD}/{D}-{O}-{YYYYMMDD}?adult=N&isDirect=true|false&fareType=Y|P|C|F`; capture the `searchFlights` SSE stream **in-page** (init script wrapping `window.fetch`, forward only the final event `status.isCompleted == true` via `expose_binding`). Price: `fareType == "A01"` fares only (no card/payment condition); `adult.totalFare` is **per adult** incl. tax+fuel → total = × adults. Take flight details from the mapping holding the min A01 fare. On 503/429/non-SSE response close the browser immediately (the page retries every 1 s forever). Max 4 queries/run, 15–30 s apart. Never send Naver data to Claude (robots.txt bans AI/RAG). Link: the Naver search URL. |
| `serpapi` | optional, key `serpapi` | `GET https://serpapi.com/search.json?engine=google_flights&departure_id&arrival_id&outbound_date&return_date&type=1|2&travel_class=1..4&adults&stops=0|1|2|3&currency=KRW&hl=en&gl=kr`. One call per query (outbound list priced at round-trip total); keep `price_insights` in `raw`. Free plan 250/month → `max_queries_per_run = 1` (exact dates only). Fallback candidate when google_flights is blocked. |
| `mock` | dev/tests only, never in defaults | deterministic fake offers |

Rejected (documented in research): Amadeus self-service (decommissioned 2026-07-17), Kiwi Tequila
(closed), Skyscanner (partner-only), Duffel (booking API, no LCCs, test mode fake), Travelpayouts
(cached prices, no KR market), airline APIs (none public). SearchApi.io / Ignav are possible future
key-based providers. Airline login support = generic browser-session mechanism (§9) for future
airline providers.

### 13.2 Browser (`app/browser/`)
* One `ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")`; each job runs
  `with sync_playwright() as p:` inside that thread (fresh driver per job, nothing leaks; serializes
  profile access). Never use async Playwright or `asyncio.create_subprocess_exec` (uvicorn uses a
  Selector loop under `--reload`). Don't run the scheduler under `--reload`.
* `channel="msedge"` uses installed Edge; never run `playwright install msedge`.
* Public searches use throwaway contexts (`locale="ko-KR"`, `timezone_id="Asia/Seoul"`).
  Login-needing sites use a dedicated profile under `%LOCALAPPDATA%\AiSquaredTravel\browser_profiles\<key>`
  (outside the Desktop/OneDrive/project folder) **and** an encrypted exported `storage_state`
  (session cookies don't survive persistent-profile restarts). Never touch the real Edge profile.
* No stealth/evasion tooling, no UA spoofing in browser mode, no captcha solving. Block pages →
  `ProviderBlocked` + screenshot saved to `data/logs/`.
* keyring backend must be `WinVaultKeyring` (explicit check, not `assert`); on key rotation call
  `delete_password` before `set_password`.

### 13.3 Notifications (priority order)
1. `inapp` (always) — DB row + bell/badge in SPA.
2. `desktop` — Windows toast via `windows-toasts` 1.3.1 (`launch_action` = booking URL; register a
   custom AUMID "AiSquared Travel" once).
3. `telegram` — Bot API `sendMessage` with inline URL button; settings UI helps detect `chat_id`
   via `getUpdates` (only within 24 h of the user's `/start`).
4. `email` — SMTP STARTTLS presets: Gmail `smtp.gmail.com:587`, Naver `smtp.naver.com:587`
   (both need 2-step verification + app password).
5. `ntfy` — `POST https://ntfy.sh/` JSON body `{topic,title,message,click,tags}` (JSON avoids
   non-ASCII header errors).
6. `kakao` — "나에게 보내기" `POST https://kapi.kakao.com/v2/api/talk/memo/default/send`
   (`template_object` text template, ≤200 chars shown, **always** include `link` with
   `web_url`+`mobile_web_url`, `button_title`). Label in UI: "records to KakaoTalk 'chat with
   myself' — no push alert". OAuth code flow (`kauth.kakao.com/oauth/authorize`, `/oauth/token`,
   scope `talk_message`, client_secret sent when set). Access token ~6 h, refresh ~2 months;
   a refresh may rotate the refresh token (old one revoked) → persist immediately, single lock
   around refresh. Link domains must be registered in the Kakao console (first registered domain
   becomes the default; unregistered links are silently redirected to it) — document in README.
`NOTIFICATION_CHANNELS = ["inapp","desktop","telegram","email","ntfy","kakao"]`.

### 13.4 Itinerary data
* **Research call**: Claude (`claude-opus-5-5`, explicit effort) with server tool
  `{"type":"web_search_20260209","name":"web_search","max_uses":10,"user_location":{"type":"approximate","city":..,"country":..,"timezone":..}}`
  (retry once without `country` on 400) + strict client tool `submit_candidates`; `tool_choice`
  auto + instruction. Continue on `pause_turn` by appending the assistant content unchanged
  (append-only history; never trim earlier blocks — thinking blocks are bound to the prefix).
  Keep only `source_urls` that actually appeared in `web_search_tool_result` blocks / citations.
* **Geocoding**: Nominatim (`https://nominatim.openstreetmap.org/search`, ≤1 req/s, explicit
  `User-Agent: AiSquaredTravel/0.1 (personal use)`, permanent cache table `GeocodeCache`); LLM
  coordinates only as fallback (flag as approximate).
* **Weather**: Open-Meteo forecast if all dates ≤ 15 days ahead, else climatology from the archive
  API (same calendar window, last 5 years). Attribution "Weather data by Open-Meteo.com (CC BY 4.0)".
* **Holidays**: `holidays` package (closures on public holidays, e.g. "closed Monday; next day if holiday").
* **Planner**: pure-Python balanced clustering (k-means++ init + capacity-constrained greedy/regret
  assignment, ≤ ~40 points), cluster→date assignment considering weather (outdoor on dry days) and
  closures, per-day ordering: brute force ≤ 8 stops else nearest-neighbour + 2-opt, with time windows.
* **Compose call**: Claude without web search, input = optimized draft plan + candidate facts,
  output via strict client tool `submit_itinerary` (structured `output_config.format` is not mixed with
  web search). Links built in code (never by the LLM): Google Maps
  `https://www.google.com/maps/search/?api=1&query=<name+area>` and per-leg
  `https://www.google.com/maps/dir/?api=1&origin=..&destination=..&travelmode=transit`;
  Tabelog area/genre links for Japan restaurants (`https://tabelog.com/kr/...` pattern in research).
* **Without a Claude key**: candidates from Overpass (small bbox queries; OSM attribution) and
  the deterministic planner produce a `rule_based` itinerary.
* Google Places is **not** used in the MVP (ToS forbids feeding Places content into an LLM / storing it).
* Console encoding: set `PYTHONUTF8=1` in run scripts; log files UTF-8.
