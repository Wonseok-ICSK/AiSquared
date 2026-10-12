// Fixture data shaped exactly like backend/app/schemas.py (dev-only; not part of the build).

export const STYLE_KEYS = [
  "sightseeing", "food", "relaxation", "hotel_resort", "shopping",
  "nightlife", "nature", "photography", "culture", "local_experience",
];

const pad = (n) => String(n).padStart(2, "0");
/** Naive UTC ISO with Z, like timeutil.to_utc_iso. */
export function utc(date) {
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}Z`;
}
export const nowUtc = () => utc(new Date());

// KST 08:30 = 23:30 UTC (prev day), 17:30 KST = 08:30 UTC
function runTimes(count) {
  const out = [];
  const base = Date.UTC(2026, 8, 30, 8, 30); // 2026-09-30 17:30 KST
  for (let i = 0; i < count; i++) out.push(new Date(base + i * 12 * 3600_000 - (i % 2) * 9 * 3600_000));
  return out.sort((a, b) => a - b);
}

export const AIRPORTS = [
  { code: "SEL", name: "Seoul (all airports)", city: "Seoul", city_code: "SEL", country: "KR", lat: 37.5665, lon: 126.978, is_metro: true },
  { code: "ICN", name: "Incheon International Airport", city: "Seoul", city_code: "SEL", country: "KR", lat: 37.4691, lon: 126.451, is_metro: false },
  { code: "GMP", name: "Gimpo International Airport", city: "Seoul", city_code: "SEL", country: "KR", lat: 37.5583, lon: 126.791, is_metro: false },
  { code: "TYO", name: "Tokyo (all airports)", city: "Tokyo", city_code: "TYO", country: "JP", lat: 35.6762, lon: 139.6503, is_metro: true },
  { code: "NRT", name: "Narita International Airport", city: "Tokyo", city_code: "TYO", country: "JP", lat: 35.7647, lon: 140.386, is_metro: false },
  { code: "HND", name: "Tokyo Haneda International Airport", city: "Tokyo", city_code: "TYO", country: "JP", lat: 35.5523, lon: 139.78, is_metro: false },
  { code: "OSA", name: "Osaka (all airports)", city: "Osaka", city_code: "OSA", country: "JP", lat: 34.6937, lon: 135.5023, is_metro: true },
  { code: "KIX", name: "Kansai International Airport", city: "Osaka", city_code: "OSA", country: "JP", lat: 34.4273, lon: 135.244, is_metro: false },
  { code: "BKK", name: "Bangkok (all airports)", city: "Bangkok", city_code: "BKK", country: "TH", lat: 13.7563, lon: 100.5018, is_metro: true },
  { code: "FUK", name: "Fukuoka Airport", city: "Fukuoka", city_code: "FUK", country: "JP", lat: 33.5859, lon: 130.451, is_metro: false },
  { code: "PUS", name: "Gimhae International Airport", city: "Busan", city_code: "PUS", country: "KR", lat: 35.1795, lon: 128.938, is_metro: false },
  { code: "CJU", name: "Jeju International Airport", city: "Jeju", city_code: "CJU", country: "KR", lat: 33.5113, lon: 126.493, is_metro: false },
];

export const PROVIDERS = [
  { key: "google_flights", display_name: "Google Flights", description: "Google Flights page in a real Edge browser (no key needed).", requires_api_key: false, configured: true, enabled_by_default: true, enabled: true, uses_browser: true, supports_login: false, session_status: null, message: "" },
  { key: "naver", display_name: "Naver Flights", description: "Korean OTA and airline prices from flight.naver.com (opt-in).", requires_api_key: false, configured: true, enabled_by_default: false, enabled: false, uses_browser: true, supports_login: false, session_status: null, message: "" },
  { key: "serpapi", display_name: "SerpApi (Google Flights API)", description: "Google Flights results via SerpApi; needs an API key.", requires_api_key: true, configured: false, enabled_by_default: true, enabled: true, uses_browser: false, supports_login: false, session_status: null, message: "SerpApi API key not set" },
  { key: "mock", display_name: "Mock (demo data)", description: "Deterministic fake offers for development and testing. Not real prices.", requires_api_key: false, configured: true, enabled_by_default: false, enabled: false, uses_browser: false, supports_login: true, session_status: "active", message: "" },
];

export function meta() {
  return {
    version: "0.1.0-mock",
    cabins: ["economy", "premium_economy", "business", "first"],
    trip_types: ["round_trip", "one_way"],
    price_bases: ["per_person", "total"],
    schedule_modes: ["times", "interval"],
    flex_days: [0, 1, 3, 7],
    alert_kinds: ["below_target", "price_drop", "new_low"],
    notification_kinds: ["price_alert", "itinerary", "system"],
    notification_channels: ["inapp", "desktop", "telegram", "email", "ntfy", "kakao"],
    style_keys: STYLE_KEYS,
    style_modes: ["percent", "score"],
    companions: ["solo", "couple", "family"],
    paces: ["relaxed", "moderate", "packed"],
    llm_efforts: ["low", "medium", "high", "xhigh", "max"],
    defaults: {
      currency: "KRW", timezone: "Asia/Seoul", adults: 1, cabin: "economy", trip_type: "round_trip",
      price_basis: "per_person", schedule_mode: "times", schedule_times: ["08:30", "17:30"], style_mode: "percent",
      style_weights: Object.fromEntries(STYLE_KEYS.map((k) => [k, 10])),
      score_style_weights: Object.fromEntries(STYLE_KEYS.map((k) => [k, 3])),
      budget_basis: "total", max_schedule_times: 6,
    },
    providers: PROVIDERS,
  };
}

function baseTrip(over) {
  return {
    name: "", origin: "ICN", destination: "TYO", trip_type: "round_trip", depart_date: "2026-11-20",
    return_date: "2026-11-23", adults: 1, cabin: "economy", direct_only: false, max_stops: null,
    outbound_depart_from: null, outbound_depart_to: null, outbound_arrive_from: null, outbound_arrive_to: null,
    return_depart_from: null, return_depart_to: null, return_arrive_from: null, return_arrive_to: null,
    max_price: null, target_price: null, price_basis: "per_person", currency: "KRW", flex_depart_days: 0,
    flex_return_days: 0, include_nearby_airports: false, alt_destinations: [], providers: [],
    schedule_mode: "times", schedule_times: ["08:30", "17:30"], schedule_interval_hours: null, active: true,
    notes: null, last_checked_at: null, last_status: null, last_error: null,
    created_at: "2026-09-29T02:10:00Z", updated_at: "2026-10-05T23:30:12Z",
    latest_min_price: null, latest_min_price_basis_value: null, lowest_ever: null, next_run_at: null, unread_alerts: 0,
    ...over,
  };
}

function leg(dep, arr, depTime, arrTime, minutes, airline, fns, stops = 0, layovers = []) {
  return {
    departure_airport: dep, arrival_airport: arr, departure_time: depTime, arrival_time: arrTime,
    duration_minutes: minutes, stops, airlines: [airline], flight_numbers: fns, segments: [], layovers,
  };
}

export function buildState() {
  const trips = [
    baseTrip({
      id: 1, name: "Tokyo food trip", origin: "ICN", destination: "TYO", adults: 2, direct_only: true,
      outbound_depart_from: "07:00", outbound_depart_to: "13:00", return_depart_from: "15:00",
      max_price: 450000, target_price: 300000, flex_depart_days: 1, providers: ["google_flights", "naver"],
      alt_destinations: ["KIX"], notes: "Sushi, izakaya, local Japanese food.",
    }),
    baseTrip({
      id: 2, name: "SEL → OSA", origin: "SEL", destination: "OSA", trip_type: "one_way", depart_date: "2026-12-24",
      return_date: null, target_price: 150000, schedule_mode: "interval", schedule_interval_hours: 6, schedule_times: [],
    }),
    baseTrip({
      id: 3, name: "Bangkok winter escape", origin: "ICN", destination: "BKK", depart_date: "2027-01-15",
      return_date: "2027-01-22", cabin: "premium_economy", target_price: 700000, active: false,
      last_status: "error", last_error: "google_flights: blocked by a consent page (ProviderBlocked)",
      last_checked_at: "2026-10-03T08:30:40Z",
    }),
  ];

  // ---- runs + history for trip 1
  const times = runTimes(13);
  const minsPP = [345000, 339000, 352000, 331000, 324000, 318000, 322000, 309000, 298000, 305000, 291000, 284000, 278000];
  const naverPP = [349000, 344000, 350000, 336000, 329000, 315000, 326000, 312000, 301000, 309000, 295000, 289000, 283500];
  const runs = [];
  times.forEach((t, i) => {
    const started = utc(t);
    const finished = utc(new Date(t.getTime() + 74_000 + i * 1300));
    const failed = i === 6;
    runs.push({
      id: 100 + i, trip_id: 1, trigger: i === 9 ? "manual" : "schedule", started_at: started, finished_at: finished,
      status: failed ? "partial" : "ok", error: null, combinations_total: 6, combinations_searched: 6,
      offers_found: 38 + (i % 5) * 3, offers_matching: 9 + (i % 4), min_price_total: minsPP[i] * 2,
      min_price_per_person: minsPP[i], currency: "KRW", best_offer_id: i === 12 ? 1001 : null,
      provider_stats: {
        google_flights: { status: "ok", offers: 24 + (i % 3), error: null, duration_s: 41.2 + i, queries: 6 },
        naver: failed
          ? { status: "error", offers: 0, error: "HTTP 503 from flight-api.naver.com", duration_s: 18.4, queries: 1 }
          : { status: "ok", offers: 14 + (i % 4), error: null, duration_s: 62.5 - i, queries: 3 },
      },
      log: [
        "Expanded 3 departure dates × 2 destinations = 6 combinations",
        ...(failed ? ["naver: HTTP 503, closed browser and skipped remaining queries"] : []),
        "Best matching offer: Korean Air KE703 / KE704",
      ],
    });
  });
  runs.reverse();
  const history = {
    trip_id: 1, currency: "KRW", price_basis: "per_person",
    points: times.map((t, i) => ({
      run_id: 100 + i, checked_at: utc(new Date(t.getTime() + 74_000)), min_total: minsPP[i] * 2,
      min_per_person: minsPP[i], by_provider: i === 6 ? { google_flights: minsPP[i] } : { google_flights: minsPP[i] + (i % 3 === 0 ? 0 : 2000), naver: naverPP[i] },
    })),
  };
  Object.assign(trips[0], {
    last_checked_at: runs[0].finished_at, last_status: "ok", latest_min_price: 556000,
    latest_min_price_basis_value: 278000, lowest_ever: 278000, next_run_at: "2026-10-06T08:30:00Z", unread_alerts: 2,
  });
  Object.assign(trips[1], { next_run_at: "2026-10-06T12:00:00Z" });

  // ---- offers of the latest run (trip 1)
  const searched = runs[0].finished_at;
  const mk = (id, o) => ({
    id, trip_id: 1, run_id: 112, searched_at: searched, provider: "google_flights", seller: null,
    offer_key: `KEY-${id}`, origin: "ICN", destination: "NRT", depart_date: "2026-11-20", return_date: "2026-11-23",
    cabin: "economy", fare_type: null, baggage: null, booking_url: "https://www.google.com/travel/flights/search?tfs=CBwQAhoeEgoyMDI2LTExLTIw&hl=ko&gl=KR&curr=KRW",
    source_url: null, matches_filters: true, previous_price: null, lowest_price: null, ...o,
    airline: o.outbound.airlines.join(", "), flight_number: o.outbound.flight_numbers.join(" / "),
    departure_airport: o.outbound.departure_airport, arrival_airport: o.outbound.arrival_airport,
    departure_time: o.outbound.departure_time, arrival_time: o.outbound.arrival_time,
    duration_minutes: o.outbound.duration_minutes, stops: Math.max(o.outbound.stops, o.inbound?.stops ?? 0),
    price_per_person: o.price_total / 2,
  });
  const offers = [
    mk(1001, { outbound: leg("ICN", "NRT", "2026-11-20T09:05", "2026-11-20T11:25", 140, "Korean Air", ["KE703"]), inbound: leg("NRT", "ICN", "2026-11-23T18:30", "2026-11-23T21:15", 165, "Korean Air", ["KE704"]), price_total: 556000, previous_price: 284000, lowest_price: 278000, baggage: "1 checked bag" }),
    mk(1002, { provider: "naver", seller: "Interpark", outbound: leg("ICN", "NRT", "2026-11-20T08:30", "2026-11-20T10:55", 145, "Asiana Airlines", ["OZ102"]), inbound: leg("NRT", "ICN", "2026-11-23T16:00", "2026-11-23T18:45", 165, "Asiana Airlines", ["OZ101"]), price_total: 567000, previous_price: 289000, lowest_price: 283500, fare_type: "A01", booking_url: "https://flight.naver.com/flights/international/ICN-TYO-20261120/TYO-ICN-20261123?adult=2&isDirect=true&fareType=Y" }),
    mk(1003, { destination: "HND", outbound: leg("GMP", "HND", "2026-11-20T08:00", "2026-11-20T10:10", 130, "Japan Airlines", ["JL92"]), inbound: null, price_total: 598000, previous_price: 301000, lowest_price: 295000 }),
    mk(1004, { depart_date: "2026-11-21", outbound: leg("ICN", "NRT", "2026-11-21T07:35", "2026-11-21T09:55", 140, "Jeju Air", ["7C1102"]), inbound: leg("NRT", "ICN", "2026-11-23T20:40", "2026-11-23T23:15", 155, "Jeju Air", ["7C1103"]), price_total: 612000, previous_price: 306000, lowest_price: 289000, baggage: "Carry-on only" }),
    mk(1005, { outbound: leg("ICN", "NRT", "2026-11-20T12:20", "2026-11-20T14:45", 145, "Air Premia", ["YP731"]), inbound: leg("NRT", "ICN", "2026-11-23T15:45", "2026-11-23T18:20", 155, "Air Premia", ["YP732"]), price_total: 648000, previous_price: 330000, lowest_price: 312000 }),
    mk(1006, { matches_filters: false, outbound: leg("ICN", "NRT", "2026-11-20T19:10", "2026-11-20T21:35", 145, "ZIPAIR", ["ZG42"]), inbound: leg("NRT", "ICN", "2026-11-23T09:15", "2026-11-23T11:50", 155, "ZIPAIR", ["ZG41"]), price_total: 402000, previous_price: 201000, lowest_price: 199000 }),
    mk(1007, { matches_filters: false, outbound: leg("ICN", "NRT", "2026-11-20T10:00", "2026-11-20T16:40", 400, "China Eastern", ["MU5042", "MU271"], 1, ["PVG"]), inbound: leg("NRT", "ICN", "2026-11-23T13:00", "2026-11-23T21:30", 510, "China Eastern", ["MU272", "MU5041"], 1, ["PVG"]), price_total: 458000, previous_price: null, lowest_price: 229000 }),
  ];

  const offerHistory = (offer) => ({
    trip_id: 1, offer_key: offer.offer_key, currency: "KRW", lowest_per_person: offer.lowest_price,
    points: history.points.slice(-6).map((p, i) => ({
      offer_id: offer.id - 10 * (6 - i), run_id: p.run_id, searched_at: p.checked_at, provider: offer.provider,
      price_total: offer.price_total + (5 - i) * 6000, price_per_person: offer.price_per_person + (5 - i) * 3000,
    })),
  });

  const alerts = [
    { id: 11, trip_id: 1, kind: "below_target", enabled: true, threshold: null, drop_percent: null, price_basis: "per_person", armed: false, last_triggered_at: "2026-10-05T08:31:20Z", last_triggered_price: 291000, created_at: "2026-09-29T02:10:00Z", updated_at: "2026-10-05T08:31:20Z" },
    { id: 12, trip_id: 1, kind: "new_low", enabled: true, threshold: null, drop_percent: null, price_basis: "per_person", armed: true, last_triggered_at: "2026-10-05T23:31:20Z", last_triggered_price: 278000, created_at: "2026-09-29T02:10:00Z", updated_at: "2026-10-05T23:31:20Z" },
    { id: 13, trip_id: 1, kind: "price_drop", enabled: false, threshold: null, drop_percent: 10, price_basis: "total", armed: true, last_triggered_at: null, last_triggered_price: null, created_at: "2026-09-30T02:10:00Z", updated_at: "2026-09-30T02:10:00Z" },
  ];

  const notifications = [
    { id: 501, trip_id: 1, alert_rule_id: 12, kind: "price_alert", title: "New lowest price · Seoul (ICN) ↔ Tokyo (NRT) ₩278,000", body: "Flight Price Alert\n\nSeoul (ICN) ↔ Tokyo (NRT)\nTravel Dates: Nov 20 – Nov 23\nPrevious Lowest Price: KRW 284,000\nCurrent Price: KRW 278,000\nTarget Price: KRW 300,000\n\nThe current price is below your target price.", url: "https://www.google.com/travel/flights/search?tfs=CBwQAhoeEgoyMDI2LTExLTIw&hl=ko", data: {}, read_at: null, channel_results: { inapp: "sent", desktop: "sent", telegram: "sent", email: "failed: SMTP authentication failed (535)" }, created_at: "2026-10-05T23:31:22Z" },
    { id: 500, trip_id: 1, alert_rule_id: 11, kind: "price_alert", title: "Below target · Tokyo food trip ₩291,000", body: "Seoul (ICN) ↔ Tokyo (NRT)\nCurrent Price: KRW 291,000 (target KRW 300,000)", url: "/trips/1", data: {}, read_at: null, channel_results: { inapp: "sent", desktop: "sent", telegram: "sent" }, created_at: "2026-10-05T08:31:22Z" },
    { id: 499, trip_id: 1, alert_rule_id: null, kind: "itinerary", title: "Itinerary ready: 4 days of food & neighbourhoods in Tokyo", body: "Your itinerary for Tokyo food trip is ready.", url: "/itineraries/31", data: {}, read_at: "2026-10-04T03:00:00Z", channel_results: { inapp: "sent", desktop: "skipped" }, created_at: "2026-10-04T02:40:00Z" },
    { id: 498, trip_id: null, alert_rule_id: null, kind: "system", title: "Test notification", body: "This is a test from AiSquared Travel.", url: null, data: {}, read_at: "2026-10-02T03:00:00Z", channel_results: { inapp: "sent" }, created_at: "2026-10-02T02:59:00Z" },
  ];

  const preferences = {
    1: {
      trip_id: 1, style_mode: "percent",
      style_weights: { sightseeing: 30, food: 40, relaxation: 5, hotel_resort: 0, shopping: 15, nightlife: 5, nature: 0, photography: 0, culture: 5, local_experience: 0 },
      companion: "couple", pace: "moderate", food_wishlist: ["sushi", "ramen", "yakitori", "izakaya"],
      preferred_cuisines: ["Japanese"], avoid_foods: ["natto"], restaurant_budget_min: 1000, restaurant_budget_max: 5000,
      restaurant_budget_currency: "JPY", hotel_area: "Shinjuku", must_visit: ["Senso-ji", "Tsukiji Outer Market"], avoid: [],
      notes: null, budget_basis: "per_person", budget_currency: "KRW", budget_total: 1500000, budget_flight: 350000,
      budget_hotel: 500000, budget_food: 300000, budget_transport: 100000, budget_activities: 150000, budget_shopping: -1,
      budget_other: null, created_at: "2026-10-01T01:00:00Z", updated_at: "2026-10-04T02:30:00Z",
    },
  };

  const plan = buildPlan();
  const itineraries = [
    { id: 31, trip_id: 1, status: "done", error: null, progress: null, offer_id: 1001, arrival_at: "2026-11-20T11:25", departure_at: "2026-11-23T18:30", arrival_airport: "NRT", departure_airport: "NRT", hotel_area: "Shinjuku", generator: "claude", model: "claude-opus-5-5", request: {}, plan, parent_id: null, feedback: null, usage: { input_tokens: 48210, output_tokens: 9120, web_searches: 8, est_usd: 0.62 }, created_at: "2026-10-04T02:31:00Z", updated_at: "2026-10-04T02:40:00Z" },
    { id: 32, trip_id: 1, status: "done", error: null, progress: null, offer_id: 1001, arrival_at: "2026-11-20T11:25", departure_at: "2026-11-23T18:30", arrival_airport: "NRT", departure_airport: "NRT", hotel_area: "Shinjuku", generator: "rule_based", model: null, request: {}, plan: { ...plan, title: "Tokyo — lighter day 2", generator: "rule_based", sources: [] }, parent_id: 31, feedback: "Less walking on day 2, add a ramen dinner near the hotel.", usage: {}, created_at: "2026-10-04T03:10:00Z", updated_at: "2026-10-04T03:12:00Z" },
  ];

  const settings = {
    browser_channel: "msedge", browser_headless: true, llm_model: "claude-opus-5-5", llm_effort: "high",
    max_search_combinations: 12, default_providers: [], provider_options: { google_flights: { fetch_mode: "browser" } },
    language: "en",
    notifications: {
      desktop: { enabled: true },
      email: { enabled: false, smtp_host: "smtp.gmail.com", smtp_port: 587, username: "me@gmail.com", password: { is_set: true, source: "ui" }, from_addr: "me@gmail.com", to_addrs: ["me@gmail.com"], use_starttls: true },
      kakao: { enabled: false, rest_api_key: { is_set: false, source: null }, client_secret: { is_set: false, source: null }, redirect_uri: "http://localhost:8765/api/integrations/kakao/callback", access_token: { is_set: false, source: null }, refresh_token: { is_set: false, source: null }, access_expires_at: null, refresh_expires_at: null },
      telegram: { enabled: true, bot_token: { is_set: true, source: "env" }, chat_id: "123456789" },
      ntfy: { enabled: false, server: "https://ntfy.sh", topic: null, token: { is_set: false, source: null } },
    },
    api_keys: { anthropic: { is_set: true, source: "env" }, serpapi: { is_set: false, source: null }, google_maps: { is_set: false, source: null } },
  };

  const jobs = [
    { job_id: "trip-1", trip_id: 1, trip_name: "Tokyo food trip", next_run_at: "2026-10-06T08:30:00Z", trigger: "daily at 08:30, 17:30 (Asia/Seoul)" },
    { job_id: "trip-2", trip_id: 2, trip_name: "SEL → OSA", next_run_at: "2026-10-06T12:00:00Z", trigger: "every 6 h" },
  ];

  return { trips, runs, history, offers, offerHistory, alerts, notifications, preferences, itineraries, settings, jobs, providers: PROVIDERS.map((p) => ({ ...p })) };
}

function item(start, end, kind, title, extra = {}) {
  return { start, end, kind, title, area: null, lat: null, lng: null, description: null, est_cost: null, cost_currency: null, travel_from_prev: null, tips: null, map_url: null, source_urls: [], candidate_id: null, ...extra };
}
const maps = (q) => `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(q)}`;

function buildPlan() {
  return {
    title: "4 days of food & neighbourhoods in Tokyo",
    summary: "West Tokyo on arrival day, old Tokyo (Asakusa–Ueno) on day 2, markets and Ginza on day 3, and a relaxed morning before the evening flight. Days are grouped by area to keep transit short.",
    destination: "Tokyo, Japan",
    currency_local: "JPY",
    days: [
      {
        date: "2026-11-20", theme: "Arrival & Shinjuku evening", areas: ["Shinjuku"], est_day_cost: 9800,
        weather: { summary: "Partly cloudy", temp_min_c: 9.2, temp_max_c: 16.8, precip_mm: 0.4 },
        items: [
          item("11:25", "12:30", "arrival", "Arrive at Narita (NRT)", { area: "Narita", lat: 35.7647, lng: 140.386, tips: "Buy a Suica card at the JR counter; take the Narita Express to Shinjuku.", map_url: maps("Narita Airport Terminal 1") }),
          item("12:30", "14:00", "transit", "Narita Express to Shinjuku", { area: "Shinjuku", lat: 35.6896, lng: 139.7006, est_cost: 3250, cost_currency: "JPY", travel_from_prev: { mode: "train", minutes: 85, distance_km: 66.2 } }),
          item("14:30", "15:00", "hotel", "Check in near Shinjuku Station", { area: "Shinjuku", lat: 35.6905, lng: 139.6995, travel_from_prev: { mode: "walk", minutes: 8, distance_km: 0.6 } }),
          item("16:00", "17:30", "activity", "Shinjuku Gyoen National Garden", { area: "Shinjuku", lat: 35.6852, lng: 139.71, description: "Autumn leaves peak late November; last entry 16:00.", est_cost: 500, cost_currency: "JPY", travel_from_prev: { mode: "walk", minutes: 14, distance_km: 1.1 }, map_url: maps("Shinjuku Gyoen"), source_urls: ["https://www.env.go.jp/garden/shinjukugyoen/"] }),
          item("19:00", "20:30", "meal", "Yakitori in Omoide Yokocho", { area: "Shinjuku", lat: 35.6933, lng: 139.6995, description: "Tiny smoky counters; most seat 6–10 people.", est_cost: 4000, cost_currency: "JPY", travel_from_prev: { mode: "walk", minutes: 18, distance_km: 1.4 }, tips: "Cash is still common here.", map_url: maps("Omoide Yokocho Shinjuku") }),
          item("21:00", "22:00", "activity", "Tokyo Metropolitan Government Building observation deck", { area: "Nishi-Shinjuku", lat: 35.6896, lng: 139.6917, est_cost: 0, cost_currency: "JPY", travel_from_prev: { mode: "walk", minutes: 12, distance_km: 0.9 }, map_url: maps("Tokyo Metropolitan Government Building") }),
        ],
      },
      {
        date: "2026-11-21", theme: "Old Tokyo: Asakusa, Ueno & Akihabara", areas: ["Asakusa", "Ueno", "Akihabara"], est_day_cost: 12600,
        weather: { summary: "Sunny", temp_min_c: 8.1, temp_max_c: 17.5, precip_mm: 0 },
        items: [
          item("09:00", "09:45", "meal", "Breakfast at a kissaten", { area: "Asakusa", lat: 35.7119, lng: 139.7967, est_cost: 900, cost_currency: "JPY", travel_from_prev: { mode: "transit", minutes: 32, distance_km: 11.8 } }),
          item("10:00", "11:45", "activity", "Senso-ji & Nakamise-dori", { area: "Asakusa", lat: 35.7148, lng: 139.7967, description: "Tokyo's oldest temple; arrive before 10:00 to beat the crowds.", travel_from_prev: { mode: "walk", minutes: 5, distance_km: 0.3 }, map_url: maps("Senso-ji"), source_urls: ["https://www.senso-ji.jp/"] }),
          item("12:00", "13:00", "meal", "Tempura lunch near Kaminarimon", { area: "Asakusa", lat: 35.7111, lng: 139.7963, est_cost: 2500, cost_currency: "JPY", travel_from_prev: { mode: "walk", minutes: 6, distance_km: 0.4 } }),
          item("14:00", "16:00", "activity", "Ueno Park & Ameyoko market", { area: "Ueno", lat: 35.7141, lng: 139.7741, travel_from_prev: { mode: "transit", minutes: 18, distance_km: 6.2 }, map_url: maps("Ameyoko") }),
          item("16:30", "18:00", "activity", "Akihabara electronics & retro games", { area: "Akihabara", lat: 35.6984, lng: 139.7731, travel_from_prev: { mode: "walk", minutes: 20, distance_km: 1.6 } }),
          item("19:00", "20:30", "meal", "Sushi omakase (counter)", { area: "Ueno", lat: 35.7101, lng: 139.7745, est_cost: 5000, cost_currency: "JPY", tips: "Book 2–3 weeks ahead.", travel_from_prev: { mode: "transit", minutes: 9, distance_km: 2.0 } }),
        ],
      },
      {
        date: "2026-11-22", theme: "Markets, Ginza & Shibuya night", areas: ["Tsukiji", "Ginza", "Shibuya"], est_day_cost: 15200,
        weather: { summary: "Light rain in the afternoon", temp_min_c: 10.4, temp_max_c: 14.9, precip_mm: 5.6 },
        items: [
          item("08:30", "10:30", "meal", "Tsukiji Outer Market breakfast crawl", { area: "Tsukiji", lat: 35.6654, lng: 139.7707, est_cost: 3000, cost_currency: "JPY", travel_from_prev: { mode: "transit", minutes: 26, distance_km: 9.4 }, map_url: maps("Tsukiji Outer Market") }),
          item("11:00", "13:30", "activity", "Ginza window shopping & Itoya", { area: "Ginza", lat: 35.6717, lng: 139.765, travel_from_prev: { mode: "walk", minutes: 12, distance_km: 0.9 } }),
          item("14:00", "16:00", "free", "Rest at the hotel (rain expected)", { area: "Shinjuku", travel_from_prev: { mode: "transit", minutes: 22, distance_km: 8.1 } }),
          item("18:00", "19:30", "activity", "Shibuya Sky at sunset", { area: "Shibuya", lat: 35.6585, lng: 139.7023, est_cost: 2500, cost_currency: "JPY", travel_from_prev: { mode: "transit", minutes: 12, distance_km: 3.6 }, tips: "Indoor deck stays open if the rooftop closes for rain.", map_url: maps("Shibuya Sky") }),
          item("20:00", "21:30", "meal", "Ramen in Shibuya", { area: "Shibuya", lat: 35.6595, lng: 139.6988, est_cost: 1400, cost_currency: "JPY", travel_from_prev: { mode: "walk", minutes: 7, distance_km: 0.5 } }),
        ],
      },
      {
        date: "2026-11-23", theme: "Slow morning & departure", areas: ["Shinjuku"], est_day_cost: 5200,
        weather: { summary: "Clear", temp_min_c: 7.5, temp_max_c: 15.2, precip_mm: 0 },
        items: [
          item("09:30", "11:00", "activity", "Meiji Jingu forest walk", { area: "Harajuku", lat: 35.6764, lng: 139.6993, travel_from_prev: { mode: "transit", minutes: 10, distance_km: 2.4 } }),
          item("11:30", "12:30", "meal", "Udon lunch", { area: "Shinjuku", lat: 35.6909, lng: 139.7003, est_cost: 1200, cost_currency: "JPY", travel_from_prev: { mode: "transit", minutes: 9, distance_km: 2.6 } }),
          item("13:30", "15:30", "transit", "Narita Express to the airport", { area: "Narita", lat: 35.7647, lng: 140.386, est_cost: 3250, cost_currency: "JPY", travel_from_prev: { mode: "train", minutes: 85, distance_km: 66.2 } }),
          item("18:30", "18:30", "departure", "Depart Narita (NRT) KE704", { area: "Narita" }),
        ],
      },
    ],
    budget: {
      currency: "KRW",
      lines: [
        { category: "flight", budget: 350000, estimated: 278000, unlimited: false },
        { category: "hotel", budget: 500000, estimated: 465000, unlimited: false },
        { category: "food", budget: 300000, estimated: 318000, unlimited: false },
        { category: "transport", budget: 100000, estimated: 72000, unlimited: false },
        { category: "activities", budget: 150000, estimated: 48000, unlimited: false },
        { category: "shopping", budget: null, estimated: 120000, unlimited: true },
      ],
      total_budget: 1500000, total_estimated: 1301000, within_budget: true,
      notes: "JPY converted at 1 JPY = 9.21 KRW (Frankfurter, 2026-10-04).",
    },
    warnings: ["Shinjuku Gyoen: last entry 16:00 — arrive on time.", "Day 3 has rain in the afternoon; indoor alternatives were preferred."],
    sources: [
      { title: "Senso-ji official site", url: "https://www.senso-ji.jp/" },
      { title: "Shinjuku Gyoen National Garden", url: "https://www.env.go.jp/garden/shinjukugyoen/" },
    ],
    generator: "claude",
    route_metrics: { total_travel_minutes: 486, avg_hop_km: 3.9, max_hop_km: 66.2, backtrack_hops: 0 },
    // extra fields the planner adds (ItineraryPlan allows extras)
    cost_basis: "per_person",
    attributions: [
      "Map data © OpenStreetMap contributors (ODbL)",
      "Geocoding: Nominatim, © OpenStreetMap contributors (ODbL)",
      "Weather data by Open-Meteo.com (CC BY 4.0)",
    ],
  };
}
