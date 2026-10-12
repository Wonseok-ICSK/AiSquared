/** Settings "/settings": providers, API keys, AI, browser/search, notification channels, scheduler. */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Bell, Bot, CalendarClock, Globe, KeyRound, Plane, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { api } from "../api/endpoints";
import type { RuntimeSettingsOut } from "../api/types";
import { Button, EmptyState, ErrorBanner, SkeletonBlock } from "../components/ui";
import { formatRelative, formatUtc } from "../lib/format";
import { useAsync, useNow, useTitle } from "../lib/hooks";
import { getLang, tr } from "../lib/i18n";
import { AiSection, ApiKeysSection, BrowserSection } from "./settings/GeneralSections";
import { NotificationsSection } from "./settings/NotificationsSection";
import { ProvidersSection } from "./settings/ProvidersSection";
import { SettingsCard } from "./settings/shared";

function sections(): { id: string; label: string; icon: ReactNode }[] {
  return [
    { id: "providers", label: tr("Flight sources", "항공권 검색 소스"), icon: <Plane size={16} aria-hidden /> },
    { id: "api-keys", label: tr("API keys", "API 키"), icon: <KeyRound size={16} aria-hidden /> },
    { id: "ai", label: "AI", icon: <Bot size={16} aria-hidden /> },
    { id: "browser", label: tr("Browser & search", "브라우저 및 검색"), icon: <Globe size={16} aria-hidden /> },
    { id: "notifications", label: tr("Notifications", "알림"), icon: <Bell size={16} aria-hidden /> },
    { id: "scheduler", label: tr("Scheduler", "자동 확인"), icon: <CalendarClock size={16} aria-hidden /> },
  ];
}

/** Display name of a non-trip job ("housekeeping"), else the job id. */
function jobName(jobId: string): string {
  if (jobId === "housekeeping") return tr("Past-trip cleanup", "지난 여행 정리");
  return jobId;
}

/**
 * Schedule text for the backend's English trigger ("daily at 08:30 (Asia/Seoul)"):
 * unchanged in English, translated in Korean.
 */
function scheduleText(trigger: string): string {
  if (getLang() !== "ko") return trigger;
  return trigger
    .split("; ")
    .map((part) =>
      part
        .replace(/^daily at ([\d:, ]+) \(([^)]+)\)/, (_m, times: string, tz: string) =>
          `매일 ${times} (${tz === "Asia/Seoul" ? "한국 시간" : tz})`,
        )
        .replace(/^every hour$/, "1시간마다")
        .replace(/^every ([\d.]+) hours$/, "$1시간마다")
        .replace(" — deactivates past trips", " — 출발일이 지난 여행의 추적을 끝내요"),
    )
    .join("; ");
}

/** Id of the settings section currently at the top of the viewport (for the side nav). */
function useActiveSection(ids: string[], ready: boolean): string {
  const [active, setActive] = useState(ids[0]);
  const key = ids.join(",");
  useEffect(() => {
    if (!ready || typeof IntersectionObserver === "undefined") return;
    const visible = new Map<string, boolean>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) visible.set(entry.target.id, entry.isIntersecting);
        const first = ids.find((id) => visible.get(id));
        if (first) setActive(first);
      },
      { rootMargin: "-90px 0px -55% 0px" },
    );
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ready]);
  return active;
}

function SchedulerSection() {
  const jobs = useAsync(() => api.schedulerJobs(), []);
  const now = useNow();
  const list = [...(jobs.data ?? [])].sort((a, b) => (a.next_run_at ?? "~").localeCompare(b.next_run_at ?? "~"));
  return (
    <SettingsCard
      id="scheduler"
      icon={<CalendarClock size={18} aria-hidden />}
      title={tr("Scheduler", "자동 확인")}
      desc={tr("Upcoming automatic price checks (Asia/Seoul).", "예정된 자동 가격 확인 (한국 시간).")}
      actions={
        <Button size="sm" variant="ghost" icon={<RefreshCw size={14} aria-hidden />} busy={jobs.loading && Boolean(jobs.data)} onClick={() => void jobs.reload()}>
          {tr("Refresh", "새로고침")}
        </Button>
      }
    >
      <ErrorBanner
        error={jobs.error}
        title={tr("Could not load scheduled jobs", "예약된 확인 작업을 불러오지 못했어요")}
        onRetry={() => void jobs.reload()}
      />
      {jobs.loading && !jobs.data && <SkeletonBlock lines={3} label={tr("Loading jobs", "작업 불러오는 중")} />}
      {jobs.data && list.length === 0 && (
        <EmptyState compact icon={<CalendarClock size={24} />} title={tr("No scheduled checks", "예약된 확인이 없어요")}>
          {tr("Active trips appear here with their next check time.", "활성화된 여행이 다음 확인 시각과 함께 여기에 표시돼요.")}
        </EmptyState>
      )}
      {list.length > 0 && (
        <div className="table-wrap sched-table">
          <table className="table table--cards">
            <caption className="sr-only">{tr("Scheduled jobs", "예약된 작업")}</caption>
            <thead>
              <tr>
                <th scope="col">{tr("Trip", "여행")}</th>
                <th scope="col">{tr("Schedule", "일정")}</th>
                <th scope="col">{tr("Next run", "다음 확인")}</th>
              </tr>
            </thead>
            <tbody>
              {list.map((job) => (
                <tr key={job.job_id}>
                  <td className="cell-main" data-label="">
                    {job.trip_id !== null ? (
                      <Link to={`/trips/${job.trip_id}`} className="strong">
                        {job.trip_name ?? tr(`Trip #${job.trip_id}`, `여행 #${job.trip_id}`)}
                      </Link>
                    ) : (
                      <span className="strong">{job.trip_name ?? jobName(job.job_id)}</span>
                    )}
                    <div className="xsmall subtle mono">{job.job_id}</div>
                  </td>
                  <td data-label={tr("Schedule", "일정")}>{job.trigger ? scheduleText(job.trigger) : "—"}</td>
                  <td data-label={tr("Next run", "다음 확인")} className="nowrap">
                    {job.next_run_at ? (
                      <span>
                        {formatUtc(job.next_run_at)} <span className="subtle">({formatRelative(job.next_run_at, now)})</span>
                      </span>
                    ) : (
                      tr("Paused", "일시 중지됨")
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </SettingsCard>
  );
}

/** Settings page. */
export function SettingsPage() {
  useTitle(tr("Settings", "설정"));
  const settings = useAsync(() => api.getSettings(), []);
  const providers = useAsync(() => api.listProviders(), []);
  const onSettings = (s: RuntimeSettingsOut) => settings.setData(s);
  const list = sections();
  const active = useActiveSection(
    list.map((s) => s.id),
    Boolean(settings.data),
  );

  // Pick up changes made in another tab (e.g. after the KakaoTalk OAuth flow).
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        void settings.reload();
        void providers.reload();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <div className="page-head settings-head">
        <div className="page-head__title">
          <span className="settings-head__eyebrow">{tr("Your workspace, your way", "나에게 꼭 맞는 여행 공간")}</span>
          <h1>{tr("Settings", "설정")}</h1>
          <span className="settings-head__sub">
            {tr(
              "Stored on this PC. Secrets are encrypted and never shown again.",
              "이 PC에 저장돼요. 비밀 값은 암호화되며 다시 표시되지 않아요.",
            )}
          </span>
        </div>
      </div>
      <div className="settings-layout">
        <nav className="settings-nav" aria-label={tr("Settings sections", "설정 섹션")}>
          {list.map((s) => (
            <a key={s.id} href={`#${s.id}`} aria-current={active === s.id ? "location" : undefined}>
              {s.icon}
              <span>{s.label}</span>
            </a>
          ))}
        </nav>
        <div className="stack settings-main">
          <ErrorBanner
            error={settings.error}
            title={tr("Could not load settings", "설정을 불러오지 못했어요")}
            onRetry={() => void settings.reload()}
          />
          {!settings.data && !settings.error && (
            <div className="card">
              <div className="card__body">
                <SkeletonBlock lines={8} label={tr("Loading settings", "설정 불러오는 중")} />
              </div>
            </div>
          )}
          {settings.data && (
            <>
              <ProvidersSection
                settings={settings.data}
                providers={providers.data}
                providersError={providers.error}
                onSettings={onSettings}
                reloadProviders={() => void providers.reload()}
              />
              <ApiKeysSection settings={settings.data} onSettings={onSettings} />
              <AiSection settings={settings.data} onSettings={onSettings} />
              <BrowserSection settings={settings.data} onSettings={onSettings} />
              <NotificationsSection settings={settings.data} onSettings={onSettings} />
            </>
          )}
          <SchedulerSection />
        </div>
      </div>
    </>
  );
}
