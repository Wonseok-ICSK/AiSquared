/** Home "/": a greeting by Seoul time, the composer that starts a new trip (with suggestion
 * chips), a plain-words summary of the watched trips and the trips themselves. */

import { BrandMark, AssistantNote } from "../components/BrandMark";
import { Composer } from "../components/dashboard/Composer";
import { TripCard, TripCardSkeleton } from "../components/dashboard/TripCard";
import { dayPart, greeting, mostCommonOrigin, seoulHour, watchSummary } from "../components/dashboard/home";
import { api } from "../api/endpoints";
import type { TripOut } from "../api/types";
import { ErrorBanner } from "../components/ui";
import { useAsync, useNow, useTitle } from "../lib/hooks";
import { tr } from "../lib/i18n";

/** Sunrise mark beside "Good morning" (Asia/Seoul), re-checked every minute. */
function Greeting() {
  const now = useNow(60_000);
  return (
    <header className="home-greeting">
      <div className="home-greeting__line">
        <BrandMark size={40} className="home-greeting__mark" />
        <h1>{greeting(dayPart(seoulHour(now)))}</h1>
      </div>
      <p className="home-greeting__sub">
        {tr(
          "Tell me where and when, and I'll keep an eye on the fares.",
          "가고 싶은 곳과 날짜를 알려 주시면 항공권 가격을 지켜볼게요.",
        )}
      </p>
    </header>
  );
}

/** Dashboard page. */
export function Dashboard() {
  useTitle(tr("Trips", "내 여행"));
  const trips = useAsync(() => api.listTrips(), []);
  const list = trips.data ?? [];
  const replace = (next: TripOut) => trips.setData((prev) => (prev ?? []).map((t) => (t.id === next.id ? next : t)));
  const summary = watchSummary(list);

  return (
    <div className="home">
      <section className="home-hero" aria-label={tr("Start a trip", "여행 시작하기")}>
        <Greeting />
        <Composer defaultOrigin={mostCommonOrigin(list) ?? "ICN"} />
      </section>

      <ErrorBanner
        error={trips.error}
        title={tr("Could not load trips", "여행 목록을 불러오지 못했어요")}
        onRetry={() => void trips.reload()}
      />

      {trips.loading && !trips.data && (
        <div className="home-trips__list" aria-busy="true" aria-label={tr("Loading trips", "여행 목록 불러오는 중")}>
          <TripCardSkeleton />
          <TripCardSkeleton />
        </div>
      )}

      {trips.data && list.length === 0 && (
        <AssistantNote className="home-empty" title={tr("Nothing watched yet", "아직 지켜보는 여행이 없어요")}>
          {tr(
            "Fill in where and when above and send it: I'll check fares on your schedule and tell you when they drop.",
            "위에 가고 싶은 곳과 날짜를 적어 보내 주세요. 정해 둔 시각마다 가격을 확인하고, 내려가면 알려 드릴게요.",
          )}
        </AssistantNote>
      )}

      {list.length > 0 && (
        <section className="home-trips" aria-labelledby="watched-trips">
          <h2 id="watched-trips" className="home-trips__title">
            {tr("Watched trips", "지켜보는 여행")}
            <span className="count-chip">{list.length}</span>
          </h2>
          {summary && (
            <AssistantNote tone={summary.tone} className="home-summary">
              {summary.text}
            </AssistantNote>
          )}
          <div className="home-trips__list">
            {list.map((trip) => (
              <TripCard key={trip.id} trip={trip} onChange={replace} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
