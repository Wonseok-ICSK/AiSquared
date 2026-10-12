/** One typed function per REST endpoint (DESIGN §7). */

import { API_BASE, http } from "./client";
import { ApiError } from "./client";
import type {
  AirportOut,
  AlertRuleCreate,
  AlertRuleOut,
  AlertRuleUpdate,
  BookingPlanOut,
  FlightOfferOut,
  HealthOut,
  ItineraryCreate,
  ItineraryOut,
  MessageOut,
  MetaOut,
  NotificationChannel,
  NotificationOut,
  NotificationTestOut,
  OfferHistoryOut,
  OfferSort,
  PreferencesIn,
  PreferencesOut,
  PriceHistoryOut,
  ProviderInfo,
  ProviderTestOut,
  RuntimeSettingsIn,
  RuntimeSettingsOut,
  SchedulerJobOut,
  SearchRunOut,
  TelegramDetectOut,
  TripCreate,
  TripOut,
  TripUpdate,
  UnreadCountOut,
} from "./types";

export interface OfferQuery {
  run_id?: number;
  only_matching?: boolean;
  sort?: OfferSort;
  limit?: number;
}

const byIdDesc = <T extends { id: number }>(list: T[]) => [...list].sort((a, b) => b.id - a.id);
const byKeyAsc = <T>(list: T[], key: (item: T) => string) =>
  [...list].sort((a, b) => key(a).localeCompare(key(b)));

export const api = {
  // meta
  health: () => http.get<HealthOut>("/health"),
  meta: () => http.get<MetaOut>("/meta"),
  airports: (q: string, signal?: AbortSignal) =>
    http.get<AirportOut[]>("/airports", { q }, signal),

  // trips
  listTrips: () => http.get<TripOut[]>("/trips"),
  getTrip: (id: number) => http.get<TripOut>(`/trips/${id}`),
  createTrip: (body: TripCreate) => http.post<TripOut>("/trips", body),
  updateTrip: (id: number, body: TripUpdate) => http.put<TripOut>(`/trips/${id}`, body),
  deleteTrip: (id: number) => http.del<unknown>(`/trips/${id}`),
  checkTrip: (id: number) => http.post<SearchRunOut>(`/trips/${id}/check`),

  // runs & offers
  /** Runs, newest first. */
  listRuns: (tripId: number, limit = 50) =>
    http.get<SearchRunOut[]>(`/trips/${tripId}/runs`, { limit }).then(byIdDesc),
  getRun: (runId: number) => http.get<SearchRunOut>(`/runs/${runId}`),
  listOffers: (tripId: number, q: OfferQuery = {}) =>
    http.get<FlightOfferOut[]>(`/trips/${tripId}/offers`, {
      run_id: q.run_id,
      only_matching: q.only_matching,
      sort: q.sort,
      limit: q.limit,
    }),
  /** Price history with points oldest first. */
  priceHistory: (tripId: number) =>
    http
      .get<PriceHistoryOut>(`/trips/${tripId}/price-history`)
      .then((h) => ({ ...h, points: byKeyAsc(h.points, (p) => p.checked_at) })),
  /** Offer history with points oldest first. */
  offerHistory: (tripId: number, offerId: number) =>
    http
      .get<OfferHistoryOut>(`/trips/${tripId}/offers/${offerId}/history`)
      .then((h) => ({ ...h, points: byKeyAsc(h.points, (p) => p.searched_at) })),
  /**
   * The booking step of an offer. The first call can take ~45 s (the backend looks up the
   * return flight Google pairs with the fare); later calls are instant. The client applies
   * no timeout of its own, so pass a `signal` to cancel (the page aborts after 90 s).
   */
  bookingPlan: (offerId: number, signal?: AbortSignal) =>
    http.get<BookingPlanOut>(`/offers/${offerId}/booking-plan`, undefined, signal),

  // alerts
  listAlerts: (tripId: number) => http.get<AlertRuleOut[]>(`/trips/${tripId}/alerts`),
  createAlert: (tripId: number, body: AlertRuleCreate) =>
    http.post<AlertRuleOut>(`/trips/${tripId}/alerts`, body),
  updateAlert: (id: number, body: AlertRuleUpdate) =>
    http.put<AlertRuleOut>(`/alerts/${id}`, body),
  deleteAlert: (id: number) => http.del<unknown>(`/alerts/${id}`),

  // notifications
  /** Notifications, newest first. */
  listNotifications: (unreadOnly = false, limit = 100) =>
    http.get<NotificationOut[]>("/notifications", { unread_only: unreadOnly, limit }).then(byIdDesc),
  unreadCount: () => http.get<UnreadCountOut>("/notifications/unread-count"),
  markRead: (id: number) => http.post<unknown>(`/notifications/${id}/read`),
  markAllRead: () => http.post<unknown>("/notifications/read-all"),

  // preferences
  /** Stored preferences, or null when none were saved yet (404 or empty body). */
  getPreferences: (tripId: number) =>
    http
      .get<PreferencesOut | undefined | null>(`/trips/${tripId}/preferences`)
      .then((p) => p ?? null)
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }),
  putPreferences: (tripId: number, body: PreferencesIn) =>
    http.put<PreferencesOut>(`/trips/${tripId}/preferences`, body),

  // itineraries
  createItinerary: (tripId: number, body: ItineraryCreate) =>
    http.post<ItineraryOut>(`/trips/${tripId}/itineraries`, body),
  /** Itineraries, newest first. */
  listItineraries: (tripId: number) =>
    http.get<ItineraryOut[]>(`/trips/${tripId}/itineraries`).then(byIdDesc),
  getItinerary: (id: number) => http.get<ItineraryOut>(`/itineraries/${id}`),
  deleteItinerary: (id: number) => http.del<unknown>(`/itineraries/${id}`),
  refineItinerary: (id: number, feedback: string) =>
    http.post<ItineraryOut>(`/itineraries/${id}/refine`, { feedback }),

  // settings
  getSettings: () => http.get<RuntimeSettingsOut>("/settings"),
  putSettings: (body: RuntimeSettingsIn) => http.put<RuntimeSettingsOut>("/settings", body),
  testNotification: (channel: NotificationChannel) =>
    http.post<NotificationTestOut>("/settings/notifications/test", { channel }),
  detectTelegramChat: () => http.post<TelegramDetectOut>("/settings/telegram/detect-chat"),

  // providers
  listProviders: () => http.get<ProviderInfo[]>("/providers"),
  testProvider: (key: string) => http.post<ProviderTestOut>(`/providers/${key}/test`),
  /** Latest test state (polled while running; may 404/405 on backends without it). */
  getProviderTest: (key: string) => http.get<ProviderTestOut>(`/providers/${key}/test`),
  loginProvider: (key: string) => http.post<MessageOut>(`/providers/${key}/login`),
  forgetProviderSession: (key: string) => http.del<MessageOut>(`/providers/${key}/session`),

  // scheduler
  schedulerJobs: () => http.get<SchedulerJobOut[]>("/scheduler/jobs"),
};

/** Browser URL that starts the KakaoTalk OAuth flow (302 to Kakao). */
export const KAKAO_AUTHORIZE_URL = `${API_BASE}/integrations/kakao/authorize`;
