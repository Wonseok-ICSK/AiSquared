/** Notifications inbox "/notifications": unread styling, mark read / all read, open link. */

import { useState } from "react";
import { Link } from "react-router-dom";
import { BellOff, CheckCheck, ExternalLink, Info, Map as MapIcon, TrendingDown } from "lucide-react";
import type { ReactElement } from "react";
import { api } from "../api/endpoints";
import { errorMessage } from "../api/client";
import type { NotificationKind, NotificationOut } from "../api/types";
import { Badge, Button, EmptyState, ErrorBanner, SkeletonBlock, Switch } from "../components/ui";
import { useApp } from "../lib/appContext";
import { formatRelative, formatUtc, humanize } from "../lib/format";
import { useAsync, useNow, useTitle } from "../lib/hooks";
import { labels, tr } from "../lib/i18n";

const KIND_ICON: Record<NotificationKind, ReactElement> = {
  price_alert: <TrendingDown size={18} aria-hidden />,
  itinerary: <MapIcon size={18} aria-hidden />,
  system: <Info size={18} aria-hidden />,
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

/** Internal app links (start with "/") use the router; anything else opens in a new tab. */
function OpenLink({ url, onOpen }: { url: string; onOpen: () => void }) {
  if (url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/api/")) {
    return (
      <Link className="btn btn--sm btn--primary" to={url} onClick={onOpen}>
        {tr("Open", "열기")}
      </Link>
    );
  }
  return (
    <a className="btn btn--sm btn--primary" href={url} target="_blank" rel="noopener noreferrer" onClick={onOpen}>
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
  return (
    <li className={`notif${unread ? " is-unread" : ""}`}>
      <span className={`notif__icon notif__icon--${n.kind}`} title={KIND_LABEL[n.kind]}>
        {KIND_ICON[n.kind]}
      </span>
      <div className="notif__body">
        <div className="notif__head">
          <div className="stack-xs grow">
            <span className="notif__kind">
              {KIND_LABEL[n.kind]}
              {unread && <span className="notif__dot" aria-hidden />}
            </span>
            <span className="notif__title">
              {unread && <span className="sr-only">{tr("Unread: ", "읽지 않음: ")}</span>}
              {n.title}
            </span>
            <span className="notif__meta" title={formatUtc(n.created_at)}>
              {formatRelative(n.created_at, now)} · {formatUtc(n.created_at)}
              {n.trip_id !== null && (
                <>
                  {" · "}
                  <Link to={`/trips/${n.trip_id}`}>{tr("View trip", "여행 보기")}</Link>
                </>
              )}
            </span>
          </div>
          <div className="notif__actions">
            {n.url && <OpenLink url={n.url} onOpen={() => void markRead()} />}
            {unread && (
              <Button size="sm" variant="ghost" busy={busy} onClick={() => void markRead()}>
                {tr("Mark read", "읽음으로 표시")}
              </Button>
            )}
          </div>
        </div>
        <pre className="notif__text">{n.body}</pre>
        {channels.length > 0 && (
          <div className="notif__channels">
            {channels.map(([ch, result]) => (
              <Badge key={ch} tone={channelTone(result)} title={result}>
                {CHANNEL_LABEL[ch] ?? humanize(ch)}: {channelStatus(result)}
              </Badge>
            ))}
          </div>
        )}
        {error && <span className="field__error" role="alert">{error}</span>}
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
          <span className="inbox-head__eyebrow">{tr("Alerts & updates", "가격 알림과 소식")}</span>
          <h1>{tr("Notifications", "알림")}</h1>
          <p className="inbox-head__lede">
            {list.data
              ? unreadCount > 0
                ? tr(
                    `${unreadCount} unread — price alerts and finished itineraries land here.`,
                    `읽지 않은 알림 ${unreadCount}개 · 가격 알림과 완성된 여행 일정이 여기에 모여요.`,
                  )
                : tr("You're all caught up.", "새 알림을 모두 확인했어요.")
              : tr("Price alerts and finished itineraries land here.", "가격 알림과 완성된 여행 일정이 여기에 모여요.")}
          </p>
        </div>
        <div className="page-head__actions">
          <Switch checked={unreadOnly} onChange={setUnreadOnly} label={tr("Unread only", "읽지 않은 알림만")} />
          <Button icon={<CheckCheck size={16} aria-hidden />} busy={busyAll} disabled={unreadCount === 0} onClick={() => void markAll()}>
            {tr("Mark all read", "모두 읽음으로 표시")}
          </Button>
        </div>
      </div>
      <div className="stack">
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
        {items.length > 0 && (
          <ul className={`notif-list${list.loading ? " is-refreshing" : ""}`} aria-label={tr("Notifications", "알림")}>
            {items.map((n) => (
              <NotificationItem key={n.id} n={n} now={now} onRead={onRead} />
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
