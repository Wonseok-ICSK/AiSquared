// Throwaway in-memory mock of the FastAPI backend (DESIGN §7) for UI development/screenshots.
// Usage: node dev-mock/server.mjs [port]   (default 8799). Not part of the production build.

import http from "node:http";
import { AIRPORTS, buildState, meta, nowUtc } from "./fixtures.mjs";

const port = Number(process.argv[2] ?? process.env.MOCK_PORT ?? 8799);
const empty = process.env.MOCK_EMPTY === "1";
const S = buildState();
if (empty) {
  S.trips = [];
  S.notifications = [];
  S.jobs = [];
}
let nextId = 9000;

const json = (res, status, body) => {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(body === undefined ? "" : JSON.stringify(body));
};
const notFound = (res, what = "Not Found") => json(res, 404, { detail: what });

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const text = Buffer.concat(chunks).toString("utf8");
  return text ? JSON.parse(text) : {};
}

const trip = (id) => S.trips.find((t) => t.id === id);

function startRun(t) {
  const run = {
    id: nextId++, trip_id: t.id, trigger: "manual", started_at: nowUtc(), finished_at: null, status: "running",
    error: null, combinations_total: 6, combinations_searched: 0, offers_found: 0, offers_matching: 0,
    min_price_total: null, min_price_per_person: null, currency: t.currency, best_offer_id: null,
    provider_stats: {}, log: [], _t0: Date.now(),
  };
  S.runs.unshift(run);
  return run;
}

function advanceRun(run) {
  if (run.status !== "running") return run;
  const elapsed = (Date.now() - run._t0) / 1000;
  run.combinations_searched = Math.min(6, Math.floor(elapsed));
  run.offers_found = run.combinations_searched * 7;
  if (elapsed > 6) {
    const t = trip(run.trip_id);
    const pp = 271000;
    Object.assign(run, {
      status: "ok", finished_at: nowUtc(), offers_matching: 11, min_price_per_person: pp, min_price_total: pp * t.adults,
      provider_stats: { google_flights: { status: "ok", offers: 30, error: null, duration_s: 5.2, queries: 6 } },
      log: ["Expanded 6 combinations", "Best matching offer: Korean Air KE703 / KE704"],
    });
    if (t.id === 1) {
      S.history.points.push({ run_id: run.id, checked_at: run.finished_at, min_total: pp * 2, min_per_person: pp, by_provider: { google_flights: pp, naver: pp + 4000 } });
    }
    Object.assign(t, { last_checked_at: run.finished_at, last_status: "ok", latest_min_price: pp * t.adults, latest_min_price_basis_value: t.price_basis === "total" ? pp * t.adults : pp, lowest_ever: Math.min(t.lowest_ever ?? Infinity, pp) });
  }
  return run;
}

function advanceItinerary(it) {
  if (it.status !== "pending" && it.status !== "running") return it;
  const elapsed = (Date.now() - it._t0) / 1000;
  if (elapsed > 1.5) Object.assign(it, { status: "running", progress: elapsed > 4 ? "Planning routes (3/4)…" : "Researching places (1/4)…" });
  if (elapsed > 8) Object.assign(it, { status: "done", progress: null, plan: { ...S.itineraries[0].plan, title: "Fresh Tokyo plan" }, updated_at: nowUtc() });
  return it;
}

const toTripOut = (t) => ({ ...t });
const strip = (o) => Object.fromEntries(Object.entries(o).filter(([k]) => !k.startsWith("_")));

function applySecret(target, key, value) {
  if (value === undefined || value === null) return;
  target[key] = value === "" ? { is_set: false, source: null } : { is_set: true, source: "ui" };
}

function applySettings(body) {
  const s = S.settings;
  for (const k of ["browser_channel", "browser_headless", "llm_effort", "max_search_combinations", "language"]) {
    if (k in body) s[k] = body[k] ?? s[k];
  }
  if ("llm_model" in body) s.llm_model = body.llm_model || "claude-opus-5-5";
  if (body.default_providers !== undefined) {
    s.default_providers = body.default_providers ?? [];
    for (const p of S.providers) p.enabled = s.default_providers.length ? s.default_providers.includes(p.key) : p.enabled_by_default;
  }
  if (body.provider_options) s.provider_options = body.provider_options;
  for (const [k, v] of Object.entries(body.api_keys ?? {})) applySecret(s.api_keys, k, v);
  const n = body.notifications ?? {};
  const secretKeys = { email: ["password"], kakao: ["rest_api_key", "client_secret", "access_token", "refresh_token"], telegram: ["bot_token"], ntfy: ["token"] };
  for (const [ch, patch] of Object.entries(n)) {
    if (!patch) continue;
    for (const [k, v] of Object.entries(patch)) {
      if ((secretKeys[ch] ?? []).includes(k)) applySecret(s.notifications[ch], k, v);
      else if (v !== null && v !== undefined) s.notifications[ch][k] = v;
    }
  }
  return s;
}

function validateTrip(t) {
  const errs = [];
  if (!/^[A-Z]{3}$/.test(t.origin ?? "")) errs.push({ loc: ["body", "origin"], msg: "String should match pattern '^[A-Z]{3}$'", type: "string_pattern_mismatch" });
  if (t.trip_type === "round_trip" && !t.return_date) errs.push({ loc: ["body"], msg: "Value error, return_date is required for a round trip", type: "value_error" });
  return errs;
}

const routes = [
  ["GET", /^\/api\/health$/, () => [200, { status: "ok", version: "0.1.0-mock" }]],
  ["GET", /^\/api\/meta$/, () => [200, meta()]],
  ["GET", /^\/api\/airports$/, (_m, _b, q) => {
    const s = (q.get("q") ?? "").toLowerCase();
    return [200, AIRPORTS.filter((a) => a.code.toLowerCase().startsWith(s) || a.city.toLowerCase().includes(s) || a.name.toLowerCase().includes(s)).slice(0, 20)];
  }],
  ["GET", /^\/api\/trips$/, () => [200, S.trips.map(toTripOut)]],
  ["POST", /^\/api\/trips$/, (_m, b) => {
    const errs = validateTrip(b);
    if (errs.length) return [422, { detail: errs }];
    const t = { ...buildState().trips[1], ...b, id: nextId++, name: b.name || `${b.origin} → ${b.destination}`, last_checked_at: null, last_status: null, latest_min_price: null, latest_min_price_basis_value: null, lowest_ever: null, unread_alerts: 0, created_at: nowUtc(), updated_at: nowUtc() };
    S.trips.push(t);
    return [201, t];
  }],
  ["GET", /^\/api\/trips\/(\d+)$/, (m) => (trip(+m[1]) ? [200, toTripOut(trip(+m[1]))] : [404, { detail: "Trip not found" }])],
  ["PUT", /^\/api\/trips\/(\d+)$/, (m, b) => {
    const t = trip(+m[1]);
    if (!t) return [404, { detail: "Trip not found" }];
    // TripUpdate.merged_with: a stored auto name follows the route unless `name` is sent;
    // an explicit "" also falls back to the auto name.
    const auto = t.name === `${t.origin} → ${t.destination}`;
    Object.assign(t, b, { updated_at: nowUtc() });
    if (b.name === "" || (!("name" in b) && auto)) t.name = `${t.origin} → ${t.destination}`;
    return [200, t];
  }],
  ["DELETE", /^\/api\/trips\/(\d+)$/, (m) => {
    S.trips = S.trips.filter((t) => t.id !== +m[1]);
    return [204, undefined];
  }],
  ["POST", /^\/api\/trips\/(\d+)\/check$/, (m) => {
    const t = trip(+m[1]);
    if (!t) return [404, { detail: "Trip not found" }];
    if (t.id === 3) return [409, { detail: "A check for this trip is already running" }];
    return [202, strip(startRun(t))];
  }],
  ["GET", /^\/api\/trips\/(\d+)\/runs$/, (m) => [200, S.runs.filter((r) => r.trip_id === +m[1]).map((r) => strip(advanceRun(r)))]],
  ["GET", /^\/api\/runs\/(\d+)$/, (m) => {
    const r = S.runs.find((x) => x.id === +m[1]);
    return r ? [200, strip(advanceRun(r))] : [404, { detail: "Run not found" }];
  }],
  ["GET", /^\/api\/trips\/(\d+)\/offers$/, (m, _b, q) => {
    if (+m[1] !== 1) return [200, []];
    let list = [...S.offers];
    if (q.get("only_matching") !== "false") list = list.filter((o) => o.matches_filters);
    const sort = q.get("sort") ?? "price";
    list.sort((a, b) => (sort === "departure" ? a.departure_time.localeCompare(b.departure_time) : sort === "duration" ? a.duration_minutes - b.duration_minutes : a.price_total - b.price_total));
    return [200, list.slice(0, Number(q.get("limit") ?? 100))];
  }],
  ["GET", /^\/api\/trips\/(\d+)\/price-history$/, (m) => {
    const t = trip(+m[1]);
    if (!t) return [404, { detail: "Trip not found" }];
    return [200, +m[1] === 1 ? S.history : { trip_id: t.id, currency: t.currency, price_basis: t.price_basis, points: [] }];
  }],
  ["GET", /^\/api\/trips\/(\d+)\/offers\/(\d+)\/history$/, (m) => {
    const o = S.offers.find((x) => x.id === +m[2]);
    return o ? [200, S.offerHistory(o)] : [404, { detail: "Offer not found" }];
  }],
  ["GET", /^\/api\/trips\/(\d+)\/alerts$/, (m) => [200, S.alerts.filter((a) => a.trip_id === +m[1])]],
  ["POST", /^\/api\/trips\/(\d+)\/alerts$/, (m, b) => {
    if (b.kind === "price_drop" && b.drop_percent == null) return [422, { detail: [{ loc: ["body"], msg: "Value error, drop_percent is required for a price_drop rule", type: "value_error" }] }];
    const a = { id: nextId++, trip_id: +m[1], enabled: true, threshold: null, drop_percent: null, price_basis: "per_person", ...b, armed: true, last_triggered_at: null, last_triggered_price: null, created_at: nowUtc(), updated_at: nowUtc() };
    S.alerts.push(a);
    return [201, a];
  }],
  ["PUT", /^\/api\/alerts\/(\d+)$/, (m, b) => {
    const a = S.alerts.find((x) => x.id === +m[1]);
    if (!a) return [404, { detail: "Alert rule not found" }];
    Object.assign(a, b, { updated_at: nowUtc() });
    if (b.enabled === true) a.armed = true;
    return [200, a];
  }],
  ["DELETE", /^\/api\/alerts\/(\d+)$/, (m) => {
    S.alerts = S.alerts.filter((a) => a.id !== +m[1]);
    return [204, undefined];
  }],
  ["GET", /^\/api\/notifications$/, (_m, _b, q) => [200, S.notifications.filter((n) => q.get("unread_only") !== "true" || n.read_at === null)]],
  ["GET", /^\/api\/notifications\/unread-count$/, () => [200, { count: S.notifications.filter((n) => n.read_at === null).length }]],
  ["POST", /^\/api\/notifications\/read-all$/, () => {
    S.notifications.forEach((n) => (n.read_at ??= nowUtc()));
    return [200, { ok: true, message: "" }];
  }],
  ["POST", /^\/api\/notifications\/(\d+)\/read$/, (m) => {
    const n = S.notifications.find((x) => x.id === +m[1]);
    if (!n) return [404, { detail: "Notification not found" }];
    n.read_at ??= nowUtc();
    return [200, n];
  }],
  ["GET", /^\/api\/trips\/(\d+)\/preferences$/, (m) => (S.preferences[+m[1]] ? [200, S.preferences[+m[1]]] : [404, { detail: "Preferences not set" }])],
  ["PUT", /^\/api\/trips\/(\d+)\/preferences$/, (m, b) => {
    if (b.budget_total === undefined) return [422, { detail: [{ loc: ["body", "budget_total"], msg: "Field required", type: "missing" }] }];
    S.preferences[+m[1]] = { trip_id: +m[1], ...b, created_at: nowUtc(), updated_at: nowUtc() };
    return [200, S.preferences[+m[1]]];
  }],
  ["GET", /^\/api\/trips\/(\d+)\/itineraries$/, (m) => [200, S.itineraries.filter((i) => i.trip_id === +m[1]).map((i) => strip(advanceItinerary(i)))]],
  ["POST", /^\/api\/trips\/(\d+)\/itineraries$/, (m, b) => {
    const it = { id: nextId++, trip_id: +m[1], status: "pending", error: null, progress: null, offer_id: b.offer_id ?? null, arrival_at: b.arrival_at ?? "2026-11-20T11:25", departure_at: b.departure_at ?? "2026-11-23T18:30", arrival_airport: "NRT", departure_airport: "NRT", hotel_area: b.hotel_area ?? null, generator: b.use_ai === false ? "rule_based" : "claude", model: b.use_ai === false ? null : "claude-opus-5-5", request: b, plan: null, parent_id: null, feedback: null, usage: {}, created_at: nowUtc(), updated_at: nowUtc(), _t0: Date.now() };
    S.itineraries.push(it);
    return [202, strip(it)];
  }],
  ["GET", /^\/api\/itineraries\/(\d+)$/, (m) => {
    const it = S.itineraries.find((x) => x.id === +m[1]);
    return it ? [200, strip(advanceItinerary(it))] : [404, { detail: "Itinerary not found" }];
  }],
  ["DELETE", /^\/api\/itineraries\/(\d+)$/, (m) => {
    S.itineraries = S.itineraries.filter((x) => x.id !== +m[1]);
    return [204, undefined];
  }],
  ["POST", /^\/api\/itineraries\/(\d+)\/refine$/, (m, b) => {
    const parent = S.itineraries.find((x) => x.id === +m[1]);
    if (!parent) return [404, { detail: "Itinerary not found" }];
    const it = { ...parent, id: nextId++, status: "pending", plan: null, parent_id: parent.id, feedback: b.feedback, created_at: nowUtc(), _t0: Date.now() };
    S.itineraries.push(it);
    return [202, strip(it)];
  }],
  ["GET", /^\/api\/settings$/, () => [200, S.settings]],
  ["PUT", /^\/api\/settings$/, (_m, b) => [200, applySettings(b)]],
  ["POST", /^\/api\/settings\/notifications\/test$/, (_m, b) => {
    const ch = b.channel;
    const cfg = S.settings.notifications[ch];
    if (ch === "email") return [200, { channel: ch, result: "failed: SMTP authentication failed (535 5.7.8)" }];
    return [200, { channel: ch, result: ch === "inapp" || cfg?.enabled ? "sent" : "skipped" }];
  }],
  ["POST", /^\/api\/settings\/telegram\/detect-chat$/, () => [200, { chat_id: 123456789, username: "aisquared_me" }]],
  ["GET", /^\/api\/providers$/, () => [200, S.providers]],
  ["POST", /^\/api\/providers\/([a-z_]+)\/test$/, (m) => {
    S[`test_${m[1]}`] = { key: m[1], status: "running", message: "", started_at: nowUtc(), finished_at: null, offers_found: null, min_price: null, currency: null, _t0: Date.now() };
    return [202, strip(S[`test_${m[1]}`])];
  }],
  ["GET", /^\/api\/providers\/([a-z_]+)\/test$/, (m) => {
    const t = S[`test_${m[1]}`];
    if (!t) return [404, { detail: "No test has run" }];
    if (Date.now() - t._t0 > 3000 && t.status === "running") {
      Object.assign(t, m[1] === "serpapi"
        ? { status: "error", message: "SerpApi API key not set", finished_at: nowUtc() }
        : { status: "ok", offers_found: 23, min_price: 268000, currency: "KRW", finished_at: nowUtc() });
    }
    return [200, strip(t)];
  }],
  ["POST", /^\/api\/providers\/([a-z_]+)\/login$/, () => [200, { ok: true, message: "Login window opened. Log in, then close the window." }]],
  ["DELETE", /^\/api\/providers\/([a-z_]+)\/session$/, (m) => {
    const p = S.providers.find((x) => x.key === m[1]);
    if (p) p.session_status = "none";
    return [200, { ok: true, message: "Session removed." }];
  }],
  ["GET", /^\/api\/scheduler\/jobs$/, () => [200, S.jobs]],
];

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  if (url.pathname === "/api/integrations/kakao/authorize") {
    res.writeHead(302, { Location: "/settings#notifications" });
    return res.end();
  }
  try {
    const body = req.method === "POST" || req.method === "PUT" ? await readBody(req) : {};
    for (const [method, re, handler] of routes) {
      const m = url.pathname.match(re);
      if (m && method === req.method) {
        await new Promise((r) => setTimeout(r, 120)); // a little latency
        const [status, payload] = handler(m, body, url.searchParams);
        return json(res, status, payload);
      }
    }
    return notFound(res);
  } catch (err) {
    return json(res, 500, { detail: String(err) });
  }
});

server.listen(port, "127.0.0.1", () => console.log(`mock API on http://127.0.0.1:${port}`));
