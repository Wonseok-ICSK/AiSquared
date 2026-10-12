/** Itineraries tab: generated itineraries list + "Generate itinerary" form (polls while running). */

import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, GitBranch, MapPinned, Sparkles, Trash2 } from "lucide-react";
import { api } from "../../api/endpoints";
import { ApiError, errorMessage } from "../../api/client";
import type { FlightOfferOut, ItineraryCreate, ItineraryOut, TripOut } from "../../api/types";
import {
  Badge,
  Banner,
  Button,
  EmptyState,
  ErrorBanner,
  Field,
  Segmented,
  SkeletonBlock,
  Spinner,
  StatusChip,
  Switch,
} from "../../components/ui";
import { ITINERARY_STATUS_LABEL, formatDate, formatLocalDateTime, formatMoney, formatUtc } from "../../lib/format";
import { useAsync, usePoll } from "../../lib/hooks";
import { josa, tr } from "../../lib/i18n";
import { checkLength } from "../../lib/validation";
import { offerAirline } from "./tripText";

const POLL_MS = 3000;

function isBusy(it: ItineraryOut): boolean {
  return it.status === "pending" || it.status === "running";
}

function OfferChoice({ offer, checked, onSelect }: { offer: FlightOfferOut; checked: boolean; onSelect: () => void }) {
  return (
    <label className={`offer-choice${checked ? " is-on" : ""}`}>
      <input type="radio" name="offer" checked={checked} onChange={onSelect} />
      <span className="offer-choice__body">
        <span className="strong">{offerAirline(offer)}</span>
        <span className="small">
          {tr("Arrive", "도착")} {formatLocalDateTime(offer.outbound.arrival_time)} ({offer.outbound.arrival_airport})
        </span>
        <span className="small">
          {offer.inbound
            ? tr(
                `Depart ${formatLocalDateTime(offer.inbound.departure_time)} (${offer.inbound.departure_airport})`,
                `출발 ${formatLocalDateTime(offer.inbound.departure_time)} (${offer.inbound.departure_airport})`,
              )
            : offer.return_date
              ? tr(
                  `Return ${formatDate(offer.return_date)} (flight confirmed on the booking step)`,
                  `오는 날 ${formatDate(offer.return_date)} (오는 편은 예약 준비에서 확인해요)`,
                )
              : tr("One way", "편도")}
        </span>
      </span>
      <span className="offer-choice__price tnum">
        {formatMoney(offer.price_per_person, offer.currency)}
        <span className="xsmall subtle">{tr("per person", "1인")}</span>
      </span>
    </label>
  );
}

function GenerateForm({ trip, onCreated }: { trip: TripOut; onCreated: (it: ItineraryOut) => void }) {
  const offers = useAsync(() => api.listOffers(trip.id, { only_matching: true, sort: "price", limit: 6 }), [trip.id]);
  const [mode, setMode] = useState<"offer" | "manual">("offer");
  const [offerId, setOfferId] = useState<number | null>(null);
  const [arrival, setArrival] = useState(`${trip.depart_date}T14:00`);
  const [departure, setDeparture] = useState(trip.return_date ? `${trip.return_date}T19:00` : "");
  const [hotelArea, setHotelArea] = useState("");
  const [notes, setNotes] = useState("");
  const [useAi, setUseAi] = useState(true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const offerList = offers.data ?? [];
  const effectiveMode = offers.data && offerList.length === 0 ? "manual" : mode;
  const selectedOffer = offerId ?? offerList[0]?.id ?? null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const errs: Record<string, string> = {};
    if (effectiveMode === "offer" && selectedOffer === null) errs.offer = tr("Choose a flight", "항공편을 선택하세요");
    if (effectiveMode === "manual") {
      if (!arrival) errs.arrival_at = tr("Required", "필수 항목이에요");
      if (!departure) errs.departure_at = tr("Required", "필수 항목이에요");
      if (arrival && departure && departure <= arrival) errs.departure_at = tr("Must be after the arrival", "도착 시간 이후여야 해요");
    }
    const h = checkLength(hotelArea, 200);
    if (h) errs.hotel_area = h;
    const n = checkLength(notes, 4000);
    if (n) errs.notes = n;
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;

    const body: ItineraryCreate = {
      hotel_area: hotelArea.trim() || null,
      notes: notes.trim() || null,
      use_ai: useAi,
      ...(effectiveMode === "offer"
        ? { offer_id: selectedOffer }
        : { arrival_at: arrival.slice(0, 16), departure_at: departure.slice(0, 16) }),
    };
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.createItinerary(trip.id, body));
    } catch (err) {
      setError(err);
      if (err instanceof ApiError) setErrors(err.fieldErrors);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="stack" onSubmit={submit} noValidate aria-label={tr("Generate itinerary", "일정 생성")}>
      <Segmented
        label={tr("Arrival and departure source", "도착·출발 시간 입력 방식")}
        value={effectiveMode}
        onChange={setMode}
        options={[
          { value: "offer", label: tr("Use a found flight", "찾은 항공편 사용") },
          { value: "manual", label: tr("Enter times manually", "시간 직접 입력") },
        ]}
      />
      {effectiveMode === "offer" && (
        <fieldset className="group">
          <legend>{tr("Cheapest matching flights from the latest check", "최근 확인에서 찾은 조건에 맞는 최저가 항공편")}</legend>
          {offers.loading && !offers.data && <SkeletonBlock lines={3} label={tr("Loading flights", "항공편 불러오는 중")} />}
          <ErrorBanner error={offers.error} title={tr("Could not load flights", "항공편을 불러오지 못했어요")} />
          <div className="offer-choices">
            {offerList.map((o) => (
              <OfferChoice key={o.id} offer={o} checked={selectedOffer === o.id} onSelect={() => setOfferId(o.id)} />
            ))}
          </div>
          {errors.offer && <span className="field__error">{errors.offer}</span>}
        </fieldset>
      )}
      {effectiveMode === "manual" && (
        <div className="form-grid">
          {offers.data && offerList.length === 0 && (
            <div className="span-12">
              <Banner tone="info">
                {tr(
                  "No flights found yet — enter your arrival and departure times at the destination.",
                  "아직 찾은 항공편이 없어요. 여행지 도착·출발 시간을 직접 입력하세요.",
                )}
              </Banner>
            </div>
          )}
          <Field label={tr("Arrival at destination (local time)", "여행지 도착 (현지 시간)")} className="span-6" required error={errors.arrival_at}>
            {({ id, describedBy, invalid }) => (
              <input id={id} type="datetime-local" className="input" value={arrival} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => setArrival(e.target.value)} />
            )}
          </Field>
          <Field label={tr("Departure from destination (local time)", "여행지 출발 (현지 시간)")} className="span-6" required error={errors.departure_at}>
            {({ id, describedBy, invalid }) => (
              <input id={id} type="datetime-local" className="input" value={departure} min={arrival || undefined} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => setDeparture(e.target.value)} />
            )}
          </Field>
        </div>
      )}
      <div className="form-grid">
        <Field
          label={tr("Hotel area", "숙소 지역")}
          className="span-6"
          error={errors.hotel_area}
          hint={tr("Optional; overrides the trip plan's hotel area", "선택 사항이에요. 입력하면 일정 계획의 숙소 지역 대신 사용해요")}
        >
          {({ id, describedBy, invalid }) => (
            <input id={id} className="input" maxLength={200} value={hotelArea} placeholder={tr("e.g. Shinjuku", "예: 신주쿠")} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => setHotelArea(e.target.value)} />
          )}
        </Field>
        <div className="span-6 field" style={{ justifyContent: "flex-end", paddingBottom: 6 }}>
          <Switch checked={useAi} onChange={setUseAi} label={tr("Use AI (Claude) for research and writing", "AI(Claude)로 조사하고 일정 작성")} />
          <span className="field__hint">
            {tr(
              "Without AI (or without an Anthropic key) a rule-based plan from open map data is built.",
              "AI를 끄거나 Anthropic 키가 없으면 공개 지도 데이터로 규칙 기반 일정을 만들어요.",
            )}
          </span>
        </div>
        <Field label={tr("Notes for this itinerary", "이 일정에 대한 메모")} className="span-12" error={errors.notes}>
          {({ id, describedBy, invalid }) => (
            <textarea id={id} className="textarea" maxLength={4000} value={notes} placeholder={tr("e.g. Arriving tired — keep day 1 light", "예: 도착하면 피곤할 테니 첫날은 가볍게")} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => setNotes(e.target.value)} />
          )}
        </Field>
      </div>
      <ErrorBanner error={error} title={tr("Could not start the itinerary", "일정 생성을 시작하지 못했어요")} />
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <Button type="submit" variant="primary" busy={busy} icon={<Sparkles size={16} aria-hidden />}>
          {tr("Generate itinerary", "일정 생성")}
        </Button>
      </div>
    </form>
  );
}

function ItineraryRow({ it, onDelete }: { it: ItineraryOut; onDelete: (id: number) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const title = it.plan?.title ?? tr(`Itinerary #${it.id}`, `여행 일정 #${it.id}`);
  const remove = async () => {
    if (!window.confirm(tr(`Delete “${title}”?`, `“${title}”${josa(title, "을/를").slice(title.length)} 삭제할까요?`))) return;
    setBusy(true);
    try {
      await api.deleteItinerary(it.id);
      onDelete(it.id);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };
  return (
    <li className="itin-row">
      <span className="itin-row__icon" aria-hidden>
        <MapPinned size={18} />
      </span>
      <div className="stack-xs grow">
        <span className="row" style={{ gap: 8 }}>
          <Link className="itin-row__title" to={`/itineraries/${it.id}`}>
            {title}
          </Link>
          <StatusChip status={it.status} label={ITINERARY_STATUS_LABEL[it.status]} />
          {it.parent_id !== null && (
            <Badge tone="outline" icon={<GitBranch size={11} aria-hidden />}>
              {tr(`Refined from #${it.parent_id}`, `#${it.parent_id}에서 수정`)}
            </Badge>
          )}
        </span>
        <span className="small subtle">
          {it.arrival_at && it.departure_at
            ? `${formatLocalDateTime(it.arrival_at)} → ${formatLocalDateTime(it.departure_at)} · `
            : ""}
          {it.generator === "claude" ? `Claude${it.model ? ` (${it.model})` : ""}` : tr("Rule-based", "규칙 기반")} ·{" "}
          {tr(`created ${formatUtc(it.created_at)}`, `생성 ${formatUtc(it.created_at)}`)}
        </span>
        {isBusy(it) && (
          <span className="live-status" aria-live="polite">
            <Spinner size={14} label={tr("Generating", "생성 중")} />
            {it.progress ?? tr("Waiting to start…", "시작 대기 중…")}
          </span>
        )}
        {it.status === "error" && <span className="field__error">{it.error ?? tr("Generation failed", "일정 생성 실패")}</span>}
        {error && <span className="field__error">{error}</span>}
      </div>
      <div className="itin-row__actions">
        <Link className="btn btn--sm" to={`/itineraries/${it.id}`}>
          {tr("Open", "열기")} <ChevronRight size={14} aria-hidden />
        </Link>
        <Button
          size="sm"
          variant="ghost"
          className="detail-head__delete"
          iconOnly
          aria-label={tr("Delete itinerary", "일정 삭제")}
          title={tr("Delete itinerary", "일정 삭제")}
          icon={<Trash2 size={14} aria-hidden />}
          busy={busy}
          onClick={() => void remove()}
        />
      </div>
    </li>
  );
}

/** Itineraries list + generator. */
export function ItinerariesTab({ trip, refreshKey }: { trip: TripOut; refreshKey: number }) {
  const list = useAsync(() => api.listItineraries(trip.id), [trip.id, refreshKey]);
  const prefs = useAsync(() => api.getPreferences(trip.id), [trip.id]);
  const items = [...(list.data ?? [])].sort((a, b) => b.id - a.id);
  const anyBusy = items.some(isBusy);

  usePoll(
    anyBusy ? () => api.listItineraries(trip.id) : null,
    (next) => {
      list.setData(next);
      return next.some(isBusy);
    },
    POLL_MS,
  );

  const prefsMissing = !prefs.loading && !prefs.error && prefs.data === null;

  return (
    <div className="stack">
      {prefsMissing && (
        <Banner tone="warn" title={tr("Trip plan not set", "일정 계획이 설정되지 않았어요")}>
          {tr("Set your travel style and budget in the ", "나에게 맞는 일정을 받으려면 먼저 ")}
          <Link to={`/trips/${trip.id}?tab=plan`}>{tr("Trip plan", "일정 계획")}</Link>
          {tr(" tab first for a personalised itinerary.", " 탭에서 여행 스타일과 예산을 설정하세요.")}
        </Banner>
      )}
      <section className="card itin-generate">
        <div className="card__head">
          <h2>
            <Sparkles size={17} aria-hidden /> {tr("Generate an itinerary", "일정 생성")}
          </h2>
        </div>
        <div className="card__body">
          <GenerateForm trip={trip} onCreated={(it) => list.setData((prev) => [it, ...(prev ?? [])])} />
        </div>
      </section>

      <section className="tab-section" aria-labelledby="itineraries-title">
        <div className="tab-section__head">
          <h2 id="itineraries-title">{tr("Itineraries", "여행 일정")}</h2>
          {items.length > 0 && (
            <span className="tab-section__note">{tr(`${items.length} made for this trip`, `이 여행의 일정 ${items.length}개`)}</span>
          )}
        </div>
        <ErrorBanner error={list.error} title={tr("Could not load itineraries", "여행 일정을 불러오지 못했어요")} onRetry={() => void list.reload()} />
        {list.loading && !list.data && (
          <div className="card card__body">
            <SkeletonBlock lines={3} label={tr("Loading itineraries", "여행 일정 불러오는 중")} />
          </div>
        )}
        {list.data && items.length === 0 && (
          <div className="card">
            <EmptyState compact icon={<MapPinned size={24} />} title={tr("No itineraries yet", "아직 여행 일정이 없어요")}>
              {tr(
                "Generate one above from a found flight or your own arrival and departure times.",
                "위에서 찾은 항공편이나 직접 입력한 도착·출발 시간으로 일정을 만들어 보세요.",
              )}
            </EmptyState>
          </div>
        )}
        {items.length > 0 && (
          <div className="card">
            <ul className="itin-list">
              {items.map((it) => (
                <ItineraryRow key={it.id} it={it} onDelete={(id) => list.setData((prev) => (prev ?? []).filter((x) => x.id !== id))} />
              ))}
            </ul>
          </div>
        )}
      </section>
    </div>
  );
}
