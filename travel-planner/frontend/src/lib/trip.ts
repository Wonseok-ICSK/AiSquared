/** Trip display helpers and shared copy. */

import type { TripOut } from "../api/types";
import { CABIN_LABEL, formatAdults, formatDateRange } from "./format";
import { tr } from "./i18n";

/** Shown wherever Naver Flights can be enabled (DESIGN §13.1: opt-in with a ToS warning). */
export function naverTosWarning(): string {
  return tr(
    "Naver's terms of service forbid automated collection (macros, bots, scrapers). This app " +
      "only runs a few slow searches in a real Edge window for your personal use, but it is still " +
      "against Naver's terms and Naver may block your IP. Use at your own risk.",
    "Naver 이용약관은 매크로·봇·스크래퍼 등을 이용한 자동 수집을 금지해요. 이 앱은 개인 용도로 " +
      "실제 Edge 창에서 느린 검색을 몇 번만 실행하지만, 그래도 Naver 약관에 어긋나며 IP가 차단될 수 " +
      "있어요. 본인 책임하에 사용하세요.",
  );
}

/** "Nov 20 – Nov 27 · 2 adults · Economy · Direct only" / "11월 20일 – 11월 27일 · 성인 2명 · 일반석 · 직항만". */
export function tripFacts(trip: TripOut): string {
  const stops = trip.direct_only
    ? tr("Direct only", "직항만")
    : trip.max_stops !== null
      ? tr(`≤ ${trip.max_stops} stop${trip.max_stops === 1 ? "" : "s"}`, `경유 ${trip.max_stops}회 이하`)
      : tr("Any stops", "경유 횟수 무관");
  return [
    formatDateRange(trip.depart_date, trip.trip_type === "round_trip" ? trip.return_date : null),
    formatAdults(trip.adults),
    CABIN_LABEL[trip.cabin],
    stops,
  ].join(" · ");
}
