/** Notifications inbox "/notifications": unread dot, mark read / all read, open link. */

import { useState } from "react";
import { Link } from "react-router-dom";
import { BellOff, CheckCheck, ExternalLink, Info, Map as MapIcon, TrendingDown } from "lucide-react";
import type { ReactElement } from "react";
import { api } from "../api/endpoints";
import { errorMessage } from "../api/client";
import type { NotificationKind, NotificationOut } from "../api/types";
import { Button, EmptyState, ErrorBanner, SkeletonBlock, Switch } from "../components/ui";
import { useApp } from "../lib/appContext";
import { APP_TZ, formatRelative, formatSeoulDay, formatSeoulTime, formatUtc, humanize, parseUtc } from "../lib/format";
import { useAsync, useNow, useTitle } from "../lib/hooks";
import { labels, tr } from "../lib/i18n";

const KIND_ICON: Record<NotificationKind, ReactElement> = {
  price_alert: <TrendingDown size={14} aria-hidden />,
  itinerary: <MapIcon size={14} aria-hidden />,
  system: <Info size={14} aria-hidden />,
};

const KIND_LABEL = labels<NotificationKind>({
  price_alert: ["Price alert", "가격 알림"],
  itinerary: ["Itinerary", "여행 일정"],
  system: ["System", "시스템"],
});

const CHANNEL_LABEL: Readonly<Record<string, string>> = labels({
  inapp: ["In-app", "앱 알림"],
  desktop: ["Windows", "Windows 알림"],
  email: ["Email", "이메일"],
  telegram: ["Telegram", "Telegram"],
  kakao: ["KakaoTalk", "카카오톡"],
  ntfy: ["ntfy", "ntfy"],
});

/** A body longer than this (or with more lines) starts collapsed. */
const LONG_BODY_CHARS = 240;
const LONG_BODY_LINES = 4;

function channelTone(result: string): "good" | "bad" | "neutral" {
  if (result === "sent") return "good";
  if (result.startsWith("failed")) return "bad";
  return "neutral";
}

/** Short status for a channel result ("sent" | "skipped: …" | "failed: …"); the raw text stays in the tooltip. */
function channelStatus(result: string): string {
  if (result === "sent") return tr("sent", "전송됨");
  if (result.startsWith("failed")) return tr("failed", "실패");
  if (result.startsWith("skipped")) return tr("skipped", "건너뜀");
  return result;
}

// "YYYY-MM-DD" / "YYYY" in Asia/Seoul (locale-independent keys, so safe to build once)
const DAY_KEY = new Intl.DateTimeFormat("en-CA", { timeZone: APP_TZ });
const YEAR_KEY = new Intl.DateTimeFormat("en-CA", { timeZone: APP_TZ, year: "numeric" });
const seoulDayKey = (ms: number) => DAY_KEY.format(new Date(ms));
const seoulYear = (ms: number) => YEAR_KEY.format(new Date(ms));

/** "Today" / "Yesterday" / "Oct 6" (with the year when it is not this year), in Asia/Seoul. */
function dayLabel(ms: number, now: number): string {
  const key = seoulDayKey(ms);
  if (key === seoulDayKey(now)) return tr("Today", "오늘");
  if (key === seoulDayKey(now - 86_400_000)) return tr("Yesterday", "어제");
  const day = formatSeoulDay(ms);
  const year = seoulYear(ms);
  return year === seoulYear(now) ? day : tr(`${day}, ${year}`, `${year}년 ${day}`);
}

/** Consecutive notifications grouped by their Seoul calendar day (the list is newest first). */
function groupByDay(items: NotificationOut[], now: number): { key: string; label: string; items: NotificationOut[] }[] {
  const groups: { key: string; label: string; items: NotificationOut[] }[] = [];
  for (const n of items) {
    const ms = parseUtc(n.created_at).getTime();
    const key = Number.isNaN(ms) ? "unknown" : seoulDayKey(ms);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(n);
    else groups.push({ key, label: Number.isNaN(ms) ? tr("Earlier", "이전") : dayLabel(ms, now), items: [n] });
  }
  return groups;
}

/** Internal app links (start with "/") use the router; anything else opens in a new tab. */
function OpenLink({ url, onOpen }: { url: string; onOpen: () => void }) {
  if (url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/api/")) {
    return (
      <Link className="btn btn--sm" to={url} onClick={onOpen}>
        {tr("Open", "열기")}
      </Link>
    );
  }
  return (
    <a className="btn btn--sm" href={url} target="_blank" rel="noopener noreferrer" onClick={onOpen}>
      {tr("Open", "열기")} <ExternalLink size={13} aria-hidden />
      <span className="sr-only">{tr("(opens in a new tab)", "(새 탭에서 열림)")}</span>
    </a>
  );
}

function NotificationItem({
  n,
  now,
  onRead,
}: {
  n: NotificationOut;
  now: number;
  onRead: (n: NotificationOut) => void;
}) {
  const unread = n.read_at === null;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const markRead = async () => {
    if (!unread) return;
    setBusy(true);
    try {
      await api.markRead(n.id);
      onRead({ ...n, read_at: new Date().toISOString() });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const channels = Object.entries(n.channel_results).filter(([ch]) => ch !== "inapp");
  const created = parseUtc(n.created_at).getTime();
  const long = n.body.length > LONG_BODY_CHARS || n.body.split("\n").length > LONG_BODY_LINES;
  const bodyId = `notif-${n.id}-body`;
  return (
    <li className={`inbox-row${unread ? " is-unread" : ""}`}>
      <span className="inbox-row__dot" aria-hidden />
      <div className="inbox-row__main">
        <div className="inbox-row__top">
          <span className="inbox-row__kind">
            {KIND_ICON[n.kind]}
            {KIND_LABEL[n.kind]}
          </span>
          <time className="inbox-row__time" dateTime={n.created_at} title={formatUtc(n.created_at)}>
            {formatRelative(n.created_at, now)}
            {!Number.isNaN(created) && <span className="inbox-row__clock"> · {formatSeoulTime(created)}</span>}
          </time>
        </div>
        <p className="inbox-row__title">
          {unread && <span className="sr-only">{tr("Unread: ", "읽지 않음: ")}</span>}
          {n.title}
        </p>
        {n.body && (
          <div id={bodyId} className={`inbox-row__text${long && !expanded ? " is-clamped" : ""}`}>
            {n.body}
          </div>
        )}
        {long && (
          <button type="button" className="inbox-row__more" aria-expanded={expanded} aria-controls={bodyId} onClick={() => setExpanded((v) => !v)}>
            {expanded ? tr("Show less", "접기") : tr("Show more", "더 보기")}
          </button>
        )}
        {(n.url || n.trip_id !== null || unread || channels.length > 0) && (
          <div className="inbox-row__foot">
            {channels.length > 0 && (
              <span className="inbox-row__channels">
                {channels.map(([ch, result]) => (
                  <span key={ch} className={`inbox-row__channel is-${channelTone(result)}`} title={result}>
                    {CHANNEL_LABEL[ch] ?? humanize(ch)} {channelStatus(result)}
                  </span>
                ))}
              </span>
            )}
            <span className="inbox-row__actions">
              {n.trip_id !== null && (
                <Link className="btn btn--sm btn--ghost" to={`/trips/${n.trip_id}`}>
                  {tr("View trip", "여행 보기")}
                </Link>
              )}
              {unread && (
                <Button size="sm" variant="ghost" busy={busy} onClick={() => void markRead()}>
                  {tr("Mark read", "읽음으로 표시")}
                </Button>
              )}
              {n.url && <OpenLink url={n.url} onOpen={() => void markRead()} />}
            </span>
          </div>
        )}
        {error && (
          <span className="field__error" role="alert">
            {error}
          </span>
        )}
      </div>
    </li>
  );
}

/** Inbox page. */
export function Notifications() {
  useTitle(tr("Notifications", "알림"));
  const { refreshUnread } = useApp();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const list = useAsync(() => api.listNotifications(unreadOnly, 200), [unreadOnly]);
  const [busyAll, setBusyAll] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const now = useNow();
  const items = list.data ?? [];
  const unreadCount = items.filter((n) => n.read_at === null).length;
  const groups = groupByDay(items, now);

  const onRead = (n: NotificationOut) => {
    list.setData((prev) =>
      (prev ?? []).map((x) => (x.id === n.id ? n : x)).filter((x) => !unreadOnly || x.read_at === null),
    );
    refreshUnread();
  };

  const markAll = async () => {
    setBusyAll(true);
    setError(null);
    try {
      await api.markAllRead();
      await list.reload();
      refreshUnread();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusyAll(false);
    }
  };

  return (
    <>
      <div className="page-head inbox-head">
        <div className="page-head__title">
          <h1>{tr("Notifications", "알림")}</h1>
          <p className="page-lede" aria-live="polite">
            {list.data
              ? unreadCount > 0
                ? tr(
                    `${unreadCount} unread. Price alerts and finished itineraries land here.`,
                    `읽지 않은 알림이 ${unreadCount}개 있어요. 가격 알림과 완성된 여행 일정이 여기에 모여요.`,
                  )
                : tr("You're all caught up.", "새 알림을 모두 확인했어요.")
              : tr("Price alerts and finished itineraries land here.", "가격 알림과 완성된 여행 일정이 여기에 모여요.")}
          </p>
        </div>
        <div className="page-head__actions inbox-head__actions">
          <Switch checked={unreadOnly} onChange={setUnreadOnly} label={tr("Unread only", "읽지 않은 알림만")} />
          <Button icon={<CheckCheck size={16} aria-hidden />} busy={busyAll} disabled={unreadCount === 0} onClick={() => void markAll()}>
            {tr("Mark all read", "모두 읽음으로 표시")}
          </Button>
        </div>
      </div>
      <div className="stack inbox">
        {error && <ErrorBanner error={error} title={tr("Could not mark notifications as read", "알림을 읽음으로 표시하지 못했어요")} />}
        <ErrorBanner
          error={list.error}
          title={tr("Could not load notifications", "알림을 불러오지 못했어요")}
          onRetry={() => void list.reload()}
        />
        {list.loading && !list.data && (
          <div className="card">
            <div className="card__body">
              <SkeletonBlock lines={6} label={tr("Loading notifications", "알림 불러오는 중")} />
            </div>
          </div>
        )}
        {list.data && items.length === 0 && (
          <div className="card">
            <EmptyState
              icon={<BellOff size={26} />}
              title={unreadOnly ? tr("All caught up", "모든 알림을 확인했어요") : tr("No notifications yet", "아직 알림이 없어요")}
            >
              {tr("Price alerts and finished itineraries show up here.", "가격 알림과 완성된 여행 일정이 여기에 표시돼요.")}
            </EmptyState>
          </div>
        )}
        {groups.map((group) => (
          <section key={group.key} className={`inbox-group${list.loading ? " is-refreshing" : ""}`} aria-labelledby={`inbox-${group.key}`}>
            <h2 className="inbox-group__title" id={`inbox-${group.key}`}>
              {group.label}
            </h2>
            <ul className="inbox-list card">
              {group.items.map((n) => (
                <NotificationItem key={n.id} n={n} now={now} onRead={onRead} />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </>
  );
}
