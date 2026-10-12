/**
 * TypeScript mirror of backend/app/schemas.py (DESIGN §6). Field names match exactly.
 *
 * Conventions (DESIGN §3):
 * - `UTCDateTime` strings are ISO-8601 UTC with a trailing "Z" ("2026-10-06T08:30:00Z").
 * - `ISODate` strings are "YYYY-MM-DD".
 * - `LocalDateTime` strings are airport/destination-local "YYYY-MM-DDTHH:MM" (never converted).
 * - Money is a number in major units and always travels with a currency code.
 */

/** ISO-8601 UTC timestamp with "Z" (rendered in Asia/Seoul by the UI). */
export type UTCDateTime = string;
/** "YYYY-MM-DD". */
export type ISODate = string;
/** Local naive "YYYY-MM-DDTHH:MM" (airport/destination time, shown as-is). */
export type LocalDateTime = string;
/** "HH:MM" 24h. */
export type HHMM = string;

// ----------------------------------------------------------------------------- enums

export type Cabin = "economy" | "premium_economy" | "business" | "first";
export type TripType = "round_trip" | "one_way";
export type PriceBasis = "per_person" | "total";
export type ScheduleMode = "times" | "interval";
export type TripStatus = "ok" | "partial" | "error";
export type RunTrigger = "schedule" | "manual";
export type RunStatus = "running" | "ok" | "partial" | "error";
export type ProviderRunStatus = "ok" | "error" | "skipped";
export type AlertKind = "below_target" | "price_drop" | "new_low";
export type NotificationKind = "price_alert" | "itinerary" | "system";
export type NotificationChannel = "inapp" | "desktop" | "telegram" | "email" | "ntfy" | "kakao";
export type Language = "en" | "ko";
export type StyleMode = "percent" | "score";
export type Companion = "solo" | "couple" | "family";
export type Pace = "relaxed" | "moderate" | "packed";
export type ItineraryStatus = "pending" | "running" | "done" | "error";
export type ItineraryGenerator = "claude" | "rule_based";
export type PlanItemKind =
  | "arrival"
  | "transit"
  | "hotel"
  | "activity"
  | "meal"
  | "free"
  | "departure";
export type SessionStatus = "none" | "active" | "expired";
export type LlmEffort = "low" | "medium" | "high" | "xhigh" | "max";
export type FlexDays = 0 | 1 | 3 | 7;
export type BrowserChannel = "msedge" | "chrome" | "chromium";

export const CABINS: readonly Cabin[] = ["economy", "premium_economy", "business", "first"];
export const PRICE_BASES: readonly PriceBasis[] = ["per_person", "total"];
export const FLEX_DAYS: readonly FlexDays[] = [0, 1, 3, 7];
export const ALERT_KINDS: readonly AlertKind[] = ["below_target", "price_drop", "new_low"];
export const NOTIFICATION_CHANNELS: readonly NotificationChannel[] = [
  "inapp",
  "desktop",
  "telegram",
  "email",
  "ntfy",
  "kakao",
];
export const COMPANIONS: readonly Companion[] = ["solo", "couple", "family"];
export const PACES: readonly Pace[] = ["relaxed", "moderate", "packed"];
export const LLM_EFFORTS: readonly LlmEffort[] = ["low", "medium", "high", "xhigh", "max"];
export const BROWSER_CHANNELS: readonly BrowserChannel[] = ["msedge", "chrome", "chromium"];
export const STYLE_KEYS = [
  "sightseeing",
  "food",
  "relaxation",
  "hotel_resort",
  "shopping",
  "nightlife",
  "nature",
  "photography",
  "culture",
  "local_experience",
] as const;
export type StyleKey = (typeof STYLE_KEYS)[number];

export const MAX_SCHEDULE_TIMES = 6;
export const DEFAULT_SCHEDULE_TIMES: readonly HHMM[] = ["08:30", "17:30"];
export const STYLE_PERCENT_MAX = 100;
export const STYLE_SCORE_MAX = 5;
export const BUDGET_UNLIMITED = -1;

// ----------------------------------------------------------------------------- misc

export interface HealthOut {
  status: "ok";
  version: string;
}

export interface MessageOut {
  ok: boolean;
  message: string;
}

/** FastAPI error body: `detail` is a string, or a list of validation errors (422). */
export interface ValidationErrorItem {
  loc: (string | number)[];
  msg: string;
  type: string;
}
export interface ErrorBody {
  detail?: string | ValidationErrorItem[];
}

// ----------------------------------------------------------------------------- trips

/** Body of POST /api/trips (TripCreate). Omitted fields take the backend defaults. */
export interface TripCreate {
  name?: string;
  origin: string;
  destination: string;
  trip_type?: TripType;
  depart_date: ISODate;
  return_date?: ISODate | null;
  adults?: number;
  cabin?: Cabin;
  direct_only?: boolean;
  max_stops?: number | null;

  outbound_depart_from?: HHMM | null;
  outbound_depart_to?: HHMM | null;
  outbound_arrive_from?: HHMM | null;
  outbound_arrive_to?: HHMM | null;
  return_depart_from?: HHMM | null;
  return_depart_to?: HHMM | null;
  return_arrive_from?: HHMM | null;
  return_arrive_to?: HHMM | null;

  max_price?: number | null;
  target_price?: number | null;
  price_basis?: PriceBasis;
  currency?: string;

  flex_depart_days?: FlexDays;
  flex_return_days?: FlexDays;
  include_nearby_airports?: boolean;
  alt_destinations?: string[];
  providers?: string[];

  schedule_mode?: ScheduleMode;
  schedule_times?: HHMM[];
  schedule_interval_hours?: number | null;

  active?: boolean;
  notes?: string | null;
}

/** Body of PUT /api/trips/{id} (TripUpdate): any subset; explicit null clears nullable fields. */
export type TripUpdate = Partial<TripCreate>;

export interface TripOut {
  id: number;
  name: string;
  origin: string;
  destination: string;
  trip_type: TripType;
  depart_date: ISODate;
  return_date: ISODate | null;
  adults: number;
  cabin: Cabin;
  direct_only: boolean;
  max_stops: number | null;

  outbound_depart_from: HHMM | null;
  outbound_depart_to: HHMM | null;
  outbound_arrive_from: HHMM | null;
  outbound_arrive_to: HHMM | null;
  return_depart_from: HHMM | null;
  return_depart_to: HHMM | null;
  return_arrive_from: HHMM | null;
  return_arrive_to: HHMM | null;

  max_price: number | null;
  target_price: number | null;
  price_basis: PriceBasis;
  currency: string;

  flex_depart_days: number;
  flex_return_days: number;
  include_nearby_airports: boolean;
  alt_destinations: string[];
  providers: string[];

  schedule_mode: ScheduleMode;
  schedule_times: HHMM[];
  schedule_interval_hours: number | null;

  active: boolean;
  notes: string | null;
  last_checked_at: UTCDateTime | null;
  last_status: TripStatus | null;
  last_error: string | null;
  created_at: UTCDateTime;
  updated_at: UTCDateTime;

  /** Lowest matching price_total of the latest finished run. */
  latest_min_price: number | null;
  /** Same offer's price in the trip's price_basis. */
  latest_min_price_basis_value: number | null;
  /** Lowest matching price ever observed, in the trip's price_basis. */
  lowest_ever: number | null;
  next_run_at: UTCDateTime | null;
  unread_alerts: number;
}

// ----------------------------------------------------------------------------- runs & offers

export interface ProviderRunStat {
  status: ProviderRunStatus;
  offers: number;
  error: string | null;
  duration_s: number;
  queries: number;
  [extra: string]: unknown;
}

export interface SearchRunOut {
  id: number;
  trip_id: number;
  trigger: RunTrigger;
  started_at: UTCDateTime;
  finished_at: UTCDateTime | null;
  status: RunStatus;
  error: string | null;
  combinations_total: number;
  combinations_searched: number;
  offers_found: number;
  offers_matching: number;
  min_price_total: number | null;
  min_price_per_person: number | null;
  currency: string;
  best_offer_id: number | null;
  provider_stats: Record<string, ProviderRunStat>;
  log: string[];
}

export interface FlightSegmentOut {
  carrier_code: string | null;
  carrier_name: string | null;
  flight_number: string | null;
  departure_airport: string;
  arrival_airport: string;
  departure_time: LocalDateTime;
  arrival_time: LocalDateTime;
  duration_minutes: number | null;
  operating_carrier: string | null;
  aircraft: string | null;
}

export interface FlightLegOut {
  departure_airport: string;
  arrival_airport: string;
  departure_time: LocalDateTime;
  arrival_time: LocalDateTime;
  duration_minutes: number | null;
  stops: number;
  airlines: string[];
  flight_numbers: string[];
  segments: FlightSegmentOut[];
  layovers: string[];
}

export interface FlightOfferOut {
  id: number;
  trip_id: number;
  run_id: number;
  searched_at: UTCDateTime;
  provider: string;
  seller: string | null;
  offer_key: string;
  origin: string;
  destination: string;
  depart_date: ISODate;
  return_date: ISODate | null;
  airline: string | null;
  flight_number: string | null;
  departure_airport: string;
  arrival_airport: string;
  departure_time: LocalDateTime;
  arrival_time: LocalDateTime;
  duration_minutes: number | null;
  stops: number;
  outbound: FlightLegOut;
  inbound: FlightLegOut | null;
  cabin: Cabin;
  fare_type: string | null;
  baggage: string | null;
  price_total: number;
  price_per_person: number;
  currency: string;
  /** Previous observation of the same offer (per person). */
  previous_price: number | null;
  /** Lowest ever for the same offer (per person). */
  lowest_price: number | null;
  booking_url: string | null;
  source_url: string | null;
  matches_filters: boolean;
}

export type OfferSort = "price" | "departure" | "duration";

export interface PricePoint {
  run_id: number;
  checked_at: UTCDateTime;
  min_total: number | null;
  min_per_person: number | null;
  /** provider key -> lowest per-person price in that run. */
  by_provider: Record<string, number>;
}

export interface PriceHistoryOut {
  trip_id: number;
  currency: string;
  price_basis: PriceBasis;
  points: PricePoint[];
}

export interface OfferHistoryPoint {
  offer_id: number;
  run_id: number;
  searched_at: UTCDateTime;
  provider: string;
  price_total: number;
  price_per_person: number;
}

export interface OfferHistoryOut {
  trip_id: number;
  offer_key: string;
  currency: string;
  lowest_per_person: number | null;
  points: OfferHistoryPoint[];
}

// ----------------------------------------------------------------------------- alerts & notifications

export interface AlertRuleCreate {
  kind: AlertKind;
  enabled?: boolean;
  /** below_target only; null = use the trip's target_price. */
  threshold?: number | null;
  /** price_drop only, e.g. 10. */
  drop_percent?: number | null;
  price_basis?: PriceBasis;
}

export interface AlertRuleUpdate {
  kind?: AlertKind;
  enabled?: boolean;
  threshold?: number | null;
  drop_percent?: number | null;
  price_basis?: PriceBasis;
  armed?: boolean;
}

export interface AlertRuleOut {
  id: number;
  trip_id: number;
  kind: AlertKind;
  enabled: boolean;
  threshold: number | null;
  drop_percent: number | null;
  price_basis: PriceBasis;
  armed: boolean;
  last_triggered_at: UTCDateTime | null;
  last_triggered_price: number | null;
  created_at: UTCDateTime;
  updated_at: UTCDateTime;
}

export interface NotificationOut {
  id: number;
  trip_id: number | null;
  alert_rule_id: number | null;
  kind: NotificationKind;
  title: string;
  body: string;
  url: string | null;
  data: Record<string, unknown>;
  read_at: UTCDateTime | null;
  /** channel -> "sent" | "skipped" | "failed: <reason>". */
  channel_results: Record<string, string>;
  created_at: UTCDateTime;
}

export interface UnreadCountOut {
  count: number;
}

export interface NotificationTestIn {
  channel: NotificationChannel;
}

export interface NotificationTestOut {
  channel: NotificationChannel;
  result: string;
}

/** POST /api/settings/telegram/detect-chat (DESIGN §7; not a named schema). */
export interface TelegramDetectOut {
  chat_id: string | number | null;
  username: string | null;
}

// ----------------------------------------------------------------------------- preferences

export type StyleWeights = Record<StyleKey, number>;

export interface PreferencesIn {
  style_mode?: StyleMode;
  style_weights?: Partial<StyleWeights>;
  companion?: Companion;
  pace?: Pace;
  food_wishlist?: string[];
  preferred_cuisines?: string[];
  avoid_foods?: string[];
  restaurant_budget_min?: number | null;
  restaurant_budget_max?: number | null;
  restaurant_budget_currency?: string | null;
  hotel_area?: string | null;
  must_visit?: string[];
  avoid?: string[];
  notes?: string | null;

  budget_basis?: PriceBasis;
  budget_currency?: string;
  budget_total: number;
  budget_flight: number;
  budget_hotel?: number | null;
  budget_food?: number | null;
  budget_transport?: number | null;
  budget_activities?: number | null;
  budget_shopping?: number | null;
  budget_other?: number | null;
}

export interface PreferencesOut {
  trip_id: number;
  style_mode: StyleMode;
  style_weights: Record<string, number>;
  companion: Companion;
  pace: Pace;
  food_wishlist: string[];
  preferred_cuisines: string[];
  avoid_foods: string[];
  restaurant_budget_min: number | null;
  restaurant_budget_max: number | null;
  restaurant_budget_currency: string | null;
  hotel_area: string | null;
  must_visit: string[];
  avoid: string[];
  notes: string | null;
  budget_basis: PriceBasis;
  budget_currency: string;
  budget_total: number;
  budget_flight: number;
  budget_hotel: number | null;
  budget_food: number | null;
  budget_transport: number | null;
  budget_activities: number | null;
  budget_shopping: number | null;
  budget_other: number | null;
  created_at: UTCDateTime;
  updated_at: UTCDateTime;
}

export const OPTIONAL_BUDGET_FIELDS = [
  "budget_hotel",
  "budget_food",
  "budget_transport",
  "budget_activities",
  "budget_shopping",
  "budget_other",
] as const;
export type OptionalBudgetField = (typeof OPTIONAL_BUDGET_FIELDS)[number];

// ----------------------------------------------------------------------------- itinerary

export interface ItineraryCreate {
  offer_id?: number | null;
  arrival_at?: LocalDateTime | null;
  departure_at?: LocalDateTime | null;
  hotel_area?: string | null;
  notes?: string | null;
  use_ai?: boolean;
}

export interface ItineraryRefine {
  feedback: string;
}

export interface TravelHop {
  /** e.g. walk | transit | taxi */
  mode: string;
  minutes: number;
  distance_km: number;
}

export interface PlanItem {
  start: HHMM;
  end: HHMM;
  kind: PlanItemKind;
  title: string;
  area: string | null;
  lat: number | null;
  lng: number | null;
  description: string | null;
  est_cost: number | null;
  cost_currency: string | null;
  travel_from_prev: TravelHop | null;
  tips: string | null;
  map_url: string | null;
  source_urls: string[];
  candidate_id: string | null;
  [extra: string]: unknown;
}

export interface PlanWeather {
  summary?: string;
  temp_min_c?: number;
  temp_max_c?: number;
  precip_mm?: number;
  [extra: string]: unknown;
}

export interface PlanDay {
  date: ISODate;
  theme: string;
  areas: string[];
  items: PlanItem[];
  est_day_cost: number | null;
  weather: PlanWeather | null;
  [extra: string]: unknown;
}

export interface BudgetLine {
  category: string;
  budget: number | null;
  estimated: number | null;
  unlimited: boolean;
}

export interface BudgetSummary {
  currency: string;
  lines: BudgetLine[];
  total_budget: number | null;
  total_estimated: number | null;
  within_budget: boolean | null;
  notes: string | null;
  [extra: string]: unknown;
}

export interface SourceRef {
  title: string;
  url: string;
}

export interface RouteMetrics {
  total_travel_minutes: number;
  avg_hop_km: number;
  max_hop_km: number;
  backtrack_hops: number;
}

export interface ItineraryPlan {
  title: string;
  summary: string;
  destination: string;
  currency_local: string;
  days: PlanDay[];
  budget: BudgetSummary | null;
  warnings: string[];
  sources: SourceRef[];
  generator: ItineraryGenerator;
  route_metrics: RouteMetrics;
  [extra: string]: unknown;
}

export interface ItineraryUsage {
  input_tokens?: number;
  output_tokens?: number;
  web_searches?: number;
  est_usd?: number;
  [extra: string]: unknown;
}

export interface ItineraryOut {
  id: number;
  trip_id: number;
  status: ItineraryStatus;
  error: string | null;
  progress: string | null;
  offer_id: number | null;
  arrival_at: LocalDateTime | null;
  departure_at: LocalDateTime | null;
  arrival_airport: string | null;
  departure_airport: string | null;
  hotel_area: string | null;
  generator: ItineraryGenerator;
  model: string | null;
  request: Record<string, unknown>;
  plan: ItineraryPlan | null;
  parent_id: number | null;
  feedback: string | null;
  usage: ItineraryUsage;
  created_at: UTCDateTime;
  updated_at: UTCDateTime;
}

// ----------------------------------------------------------------------------- providers, airports, scheduler

export interface ProviderInfo {
  key: string;
  display_name: string;
  description: string;
  requires_api_key: boolean;
  configured: boolean;
  enabled_by_default: boolean;
  /** Used for trips whose providers list is empty. */
  enabled: boolean;
  uses_browser: boolean;
  supports_login: boolean;
  session_status: SessionStatus | null;
  message: string;
}

export interface ProviderTestOut {
  key: string;
  status: "running" | "ok" | "error";
  message: string;
  started_at: UTCDateTime | null;
  finished_at: UTCDateTime | null;
  offers_found: number | null;
  min_price: number | null;
  currency: string | null;
}

export interface AirportOut {
  code: string;
  name: string;
  city: string;
  /** Korean city name ("도쿄") for popular cities, else null. */
  city_ko?: string | null;
  /** Metro/city code the airport belongs to (NRT -> TYO), if any. */
  city_code: string | null;
  country: string;
  lat: number;
  lon: number;
  is_metro: boolean;
}

export interface SchedulerJobOut {
  job_id: string;
  trip_id: number | null;
  trip_name: string | null;
  next_run_at: UTCDateTime | null;
  /** Human-readable schedule, e.g. "daily at 08:30, 17:30 (Asia/Seoul)". */
  trigger: string;
}

// ----------------------------------------------------------------------------- runtime settings

export interface SecretState {
  is_set: boolean;
  source: "ui" | "env" | null;
}

export interface DesktopSettingsOut {
  enabled: boolean;
}

export interface EmailSettingsOut {
  enabled: boolean;
  smtp_host: string | null;
  smtp_port: number;
  username: string | null;
  password: SecretState;
  from_addr: string | null;
  to_addrs: string[];
  use_starttls: boolean;
}

export interface KakaoSettingsOut {
  enabled: boolean;
  rest_api_key: SecretState;
  client_secret: SecretState;
  redirect_uri: string | null;
  access_token: SecretState;
  refresh_token: SecretState;
  access_expires_at: UTCDateTime | null;
  refresh_expires_at: UTCDateTime | null;
}

export interface TelegramSettingsOut {
  enabled: boolean;
  bot_token: SecretState;
  chat_id: string | null;
}

export interface NtfySettingsOut {
  enabled: boolean;
  server: string;
  topic: string | null;
  token: SecretState;
}

export interface NotificationSettingsOut {
  desktop: DesktopSettingsOut;
  email: EmailSettingsOut;
  kakao: KakaoSettingsOut;
  telegram: TelegramSettingsOut;
  ntfy: NtfySettingsOut;
}

export interface ApiKeysOut {
  anthropic: SecretState;
  serpapi: SecretState;
  google_maps: SecretState;
}

export type ProviderOptions = Record<string, Record<string, unknown>>;

export interface RuntimeSettingsOut {
  browser_channel: string;
  browser_headless: boolean;
  llm_model: string;
  llm_effort: LlmEffort;
  max_search_combinations: number;
  default_providers: string[];
  provider_options: ProviderOptions;
  language: Language;
  notifications: NotificationSettingsOut;
  api_keys: ApiKeysOut;
}

export interface DesktopSettingsIn {
  enabled?: boolean | null;
}

/** Secrets: omitted/null = unchanged, "" = clear. */
export interface EmailSettingsIn {
  enabled?: boolean | null;
  smtp_host?: string | null;
  smtp_port?: number | null;
  username?: string | null;
  password?: string | null;
  from_addr?: string | null;
  to_addrs?: string[] | null;
  use_starttls?: boolean | null;
}

export interface KakaoSettingsIn {
  enabled?: boolean | null;
  rest_api_key?: string | null;
  client_secret?: string | null;
  redirect_uri?: string | null;
  access_token?: string | null;
  refresh_token?: string | null;
  access_expires_at?: string | null;
  refresh_expires_at?: string | null;
}

export interface TelegramSettingsIn {
  enabled?: boolean | null;
  bot_token?: string | null;
  chat_id?: string | null;
}

export interface NtfySettingsIn {
  enabled?: boolean | null;
  server?: string | null;
  topic?: string | null;
  token?: string | null;
}

export interface NotificationSettingsIn {
  desktop?: DesktopSettingsIn | null;
  email?: EmailSettingsIn | null;
  kakao?: KakaoSettingsIn | null;
  telegram?: TelegramSettingsIn | null;
  ntfy?: NtfySettingsIn | null;
}

export interface ApiKeysIn {
  anthropic?: string | null;
  serpapi?: string | null;
  google_maps?: string | null;
}

/**
 * PUT /api/settings body (partial). Non-secret fields: omitted = unchanged, null = reset to
 * the .env/default value. Secret fields: omitted/null = unchanged, "" = clear the UI value.
 */
export interface RuntimeSettingsIn {
  browser_channel?: BrowserChannel | null;
  browser_headless?: boolean | null;
  /** Blank/null = reset to LLM_MODEL from env (default claude-opus-5-5). */
  llm_model?: string | null;
  llm_effort?: LlmEffort | null;
  max_search_combinations?: number | null;
  default_providers?: string[] | null;
  /** Replaces the whole per-provider options map when given. */
  provider_options?: ProviderOptions | null;
  language?: Language | null;
  notifications?: NotificationSettingsIn | null;
  api_keys?: ApiKeysIn | null;
}

// ----------------------------------------------------------------------------- meta

export interface MetaDefaults {
  currency: string;
  timezone: string;
  adults: number;
  cabin: Cabin;
  trip_type: TripType;
  price_basis: PriceBasis;
  schedule_mode: ScheduleMode;
  schedule_times: HHMM[];
  style_mode: StyleMode;
  /** Default weights for percent mode. */
  style_weights: Record<string, number>;
  /** Default weights for score mode. */
  score_style_weights: Record<string, number>;
  budget_basis: PriceBasis;
  max_schedule_times: number;
}

export interface MetaOut {
  version: string;
  cabins: string[];
  trip_types: string[];
  price_bases: string[];
  schedule_modes: string[];
  flex_days: number[];
  alert_kinds: string[];
  notification_kinds: string[];
  notification_channels: string[];
  style_keys: string[];
  style_modes: string[];
  companions: string[];
  paces: string[];
  llm_efforts: string[];
  defaults: MetaDefaults;
  providers: ProviderInfo[];
}
