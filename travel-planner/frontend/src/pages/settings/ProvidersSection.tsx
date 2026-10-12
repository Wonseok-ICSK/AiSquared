/** Settings → Providers: status, default sources, per-provider options, test search, login sessions. */

import { useState } from "react";
import { FlaskConical, Globe, KeyRound, LogIn, LogOut, Monitor, Server } from "lucide-react";
import { api } from "../../api/endpoints";
import { ApiError, errorMessage } from "../../api/client";
import type { ProviderInfo, ProviderTestOut, RuntimeSettingsOut } from "../../api/types";
import { Badge, Banner, Button, Checkbox, ErrorBanner, SkeletonBlock, Switch } from "../../components/ui";
import { formatMoney, formatSeconds, elapsedSeconds } from "../../lib/format";
import { usePoll } from "../../lib/hooks";
import { isLastDefault, nextDefaultProviders } from "../../lib/providerDefaults";
import { naverTosWarning } from "../../lib/trip";
import { providerDescription, providerMessage, providerName, providerSessionMessage, providerTestMessage } from "../../lib/providerText";
import { labels, tr } from "../../lib/i18n";
import { SaveStatus, SettingsCard, useSettingsSaver } from "./shared";

const SESSION_LABEL = labels<"none" | "active" | "expired">({
  none: ["Not logged in", "로그인 안 됨"],
  active: ["Session active", "세션 활성"],
  expired: ["Session expired", "세션 만료됨"],
});

function TestResult({ test }: { test: ProviderTestOut }) {
  if (test.status === "running") {
    return (
      <span className="live-status">
        <span className="spinner" aria-hidden>
          <FlaskConical size={14} />
        </span>
        {tr("Test search running…", "테스트 검색 중…")} {providerTestMessage(test.message)}
      </span>
    );
  }
  const secs = test.started_at && test.finished_at ? formatSeconds(elapsedSeconds(test.started_at, test.finished_at)) : null;
  const took = secs ? tr(` in ${secs}`, ` (${secs})`) : "";
  if (test.status === "ok") {
    return (
      <Banner tone="good" title={tr(`Test search OK${took}`, `테스트 검색 성공${took}`)}>
        {tr(`${test.offers_found ?? 0} offers found`, `항공권 ${test.offers_found ?? 0}개 찾음`)}
        {test.min_price !== null && test.currency
          ? tr(` · cheapest ${formatMoney(test.min_price, test.currency)}`, ` · 최저가 ${formatMoney(test.min_price, test.currency)}`)
          : ""}
        {test.message ? ` · ${providerTestMessage(test.message)}` : ""}
      </Banner>
    );
  }
  return (
    <Banner tone="error" title={tr(`Test search failed${took}`, `테스트 검색 실패${took}`)}>
      {providerTestMessage(test.message) || tr("Unknown error", "알 수 없는 오류")}
    </Banner>
  );
}

function ProviderTester({ provider }: { provider: ProviderInfo }) {
  const [test, setTest] = useState<ProviderTestOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [noPoll, setNoPoll] = useState(false);

  usePoll<ProviderTestOut>(
    test?.status === "running" && !noPoll ? () => api.getProviderTest(provider.key) : null,
    (next) => {
      setTest(next);
      return next.status === "running";
    },
    2000,
    (err) => {
      if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
        setNoPoll(true);
        return false;
      }
      return true;
    },
  );

  const start = async () => {
    setStarting(true);
    setError(null);
    setNoPoll(false);
    try {
      setTest(await api.testProvider(provider.key));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="stack-sm" aria-live="polite">
      <div className="row">
        <Button
          size="sm"
          icon={<FlaskConical size={14} aria-hidden />}
          busy={starting || (test?.status === "running" && !noPoll)}
          onClick={() => void start()}
        >
          {tr("Test search", "테스트 검색")}
        </Button>
        <span className="xsmall subtle">
          {tr("Runs one small live search to check that this source works.", "작은 실시간 검색을 한 번 실행해 이 소스가 작동하는지 확인해요.")}
        </span>
      </div>
      {test && !(test.status === "running" && noPoll) && <TestResult test={test} />}
      {test?.status === "running" && noPoll && (
        <Banner tone="info" title={tr("Test started — result unknown", "테스트 시작됨 — 결과 알 수 없음")}>
          {tr(
            "The test search is running in the background, but this backend version does not report test results, so the outcome can’t be shown here. To check this source, run “Check now” on a trip that uses it and look at the run’s source status under Price history.",
            "테스트 검색이 백그라운드에서 실행 중이지만, 이 백엔드 버전은 테스트 결과를 알려 주지 않아 여기에 결과를 표시할 수 없어요. 이 소스를 확인하려면 이 소스를 쓰는 여행에서 “지금 확인”을 실행한 뒤 가격 기록에서 해당 확인의 소스 상태를 살펴보세요.",
          )}
        </Banner>
      )}
      {error && (
        <Banner tone="error" title={tr("Could not start the test", "테스트를 시작하지 못했어요")}>
          {error}
        </Banner>
      )}
    </div>
  );
}

function SessionControls({ provider, onChanged }: { provider: ProviderInfo; onChanged: () => void }) {
  const [busy, setBusy] = useState<"login" | "forget" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [watching, setWatching] = useState(false);

  usePoll(
    watching ? () => api.listProviders() : null,
    (list) => {
      const p = list.find((x) => x.key === provider.key);
      if (p?.session_status === "active") {
        onChanged();
        setMessage(tr("Login saved.", "로그인을 저장했어요."));
        return false;
      }
      return true;
    },
    4000,
  );

  const login = async () => {
    setBusy("login");
    setError(null);
    try {
      const r = await api.loginProvider(provider.key);
      setMessage(
        providerSessionMessage(r.message) ||
          tr("A browser window opened. Log in there, then close the window.", "브라우저 창이 열렸어요. 그 창에서 로그인한 뒤 창을 닫으세요."),
      );
      setWatching(true);
      window.setTimeout(() => setWatching(false), 5 * 60_000);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };
  const forget = async () => {
    const name = providerName(provider);
    if (!window.confirm(tr(`Forget the saved ${name} login session?`, `저장된 ${name} 로그인 세션을 삭제할까요?`))) return;
    setBusy("forget");
    setError(null);
    try {
      const r = await api.forgetProviderSession(provider.key);
      setMessage(providerSessionMessage(r.message) || tr("Session removed.", "세션을 삭제했어요."));
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };
  const status = provider.session_status ?? "none";
  return (
    <div className="stack-sm">
      <div className="row">
        <span className="small strong">{tr("Login session", "로그인 세션")}</span>
        <Badge tone={status === "active" ? "good" : status === "expired" ? "warn" : "outline"}>{SESSION_LABEL[status]}</Badge>
      </div>
      <div className="row">
        <Button size="sm" icon={<LogIn size={14} aria-hidden />} busy={busy === "login"} onClick={() => void login()}>
          {tr("Open login window", "로그인 창 열기")}
        </Button>
        {status !== "none" && (
          <Button size="sm" variant="danger" icon={<LogOut size={14} aria-hidden />} busy={busy === "forget"} onClick={() => void forget()}>
            {tr("Forget session", "세션 삭제")}
          </Button>
        )}
      </div>
      <span aria-live="polite" className="small">
        {message && <span className="muted">{message}</span>}
        {error && <span className="err-text">{error}</span>}
      </span>
      <span className="xsmall subtle">
        {tr(
          "The login window uses a separate browser profile; cookies are stored encrypted on this PC.",
          "로그인 창은 별도의 브라우저 프로필을 사용하고, 쿠키는 이 PC에 암호화되어 저장돼요.",
        )}
      </span>
    </div>
  );
}

function ProviderOptions({
  provider,
  settings,
  onSave,
}: {
  provider: ProviderInfo;
  settings: RuntimeSettingsOut;
  onSave: (key: string, opts: Record<string, unknown>) => void;
}) {
  const opts = settings.provider_options[provider.key] ?? {};
  if (provider.key === "google_flights") {
    const mode = opts.fetch_mode === "http" ? "http" : "browser";
    return (
      <label className="field" style={{ maxWidth: 360 }}>
        <span className="field__label">{tr("Fetch mode", "가져오기 방식")}</span>
        <select className="select" value={mode} onChange={(e) => onSave(provider.key, { ...opts, fetch_mode: e.target.value })}>
          <option value="browser">{tr("Browser (Edge, headless) — recommended", "브라우저 (Edge, 헤드리스) — 추천")}</option>
          <option value="http">{tr("HTTP (faster, blocked more easily)", "HTTP (더 빠르지만 차단되기 쉬움)")}</option>
        </select>
      </label>
    );
  }
  if (provider.key === "naver") {
    return (
      <Checkbox
        checked={opts.show_window === true}
        onChange={(v) => onSave(provider.key, { ...opts, show_window: v })}
        label={tr("Show the Naver browser window", "네이버 브라우저 창 보이기")}
        hint={tr(
          "By default it opens off-screen (Naver rejects headless browsers).",
          "기본적으로 화면 밖에 열려요 (네이버는 헤드리스 브라우저를 거부해요).",
        )}
      />
    );
  }
  return null;
}

function ProviderCard({
  provider,
  settings,
  lastDefault,
  onToggleDefault,
  onSaveOptions,
  onReload,
}: {
  provider: ProviderInfo;
  settings: RuntimeSettingsOut;
  /** The only source used by default: its switch cannot be turned off. */
  lastDefault: boolean;
  onToggleDefault: (key: string, on: boolean) => void;
  onSaveOptions: (key: string, opts: Record<string, unknown>) => void;
  onReload: () => void;
}) {
  const isNaver = provider.key === "naver";
  const [ack, setAck] = useState(provider.enabled);
  const hintId = `provider-${provider.key}-default-hint`;
  return (
    <li className={`provider-card${provider.enabled ? " is-on" : ""}`}>
      <div className="provider-card__body stack-sm">
        <div className="provider-card__top">
          <span className="provider-card__icon" aria-hidden>
            {provider.uses_browser ? <Monitor size={18} /> : provider.requires_api_key ? <KeyRound size={18} /> : <Server size={18} />}
          </span>
          <div className="stack-xs grow">
            <span className="row" style={{ gap: 6 }}>
              <span className="provider-card__name">{providerName(provider)}</span>
              <span className="mono xsmall subtle">{provider.key}</span>
            </span>
            <span className="row" style={{ gap: 4 }}>
              {provider.configured ? (
                <Badge tone="good">{tr("Configured", "설정됨")}</Badge>
              ) : (
                <Badge tone="warn">{tr("Not configured", "설정 안 됨")}</Badge>
              )}
              {provider.enabled_by_default && <Badge tone="outline">{tr("On by default", "기본 사용")}</Badge>}
              {provider.uses_browser && (
                <Badge tone="outline" icon={<Monitor size={11} aria-hidden />}>
                  {tr("Browser", "브라우저")}
                </Badge>
              )}
              {provider.requires_api_key && (
                <Badge tone="outline" icon={<KeyRound size={11} aria-hidden />}>
                  {tr("API key", "API 키")}
                </Badge>
              )}
            </span>
            {provider.description && <span className="small muted">{providerDescription(provider)}</span>}
            {provider.message && <span className="xsmall subtle">{providerMessage(provider.message)}</span>}
          </div>
          <div className="stack-xs provider-card__default">
            <Switch
              checked={provider.enabled}
              disabled={(isNaver && !provider.enabled && !ack) || lastDefault}
              onChange={(v) => onToggleDefault(provider.key, v)}
              label={tr("Use for trips by default", "여행에 기본으로 사용")}
              describedBy={lastDefault ? hintId : undefined}
            />
            {lastDefault && (
              <span id={hintId} className="xsmall subtle">
                {tr(
                  "At least one default source is needed. Turn another one on first; trips can still pick their own.",
                  "기본 소스는 하나 이상 있어야 해요. 다른 소스를 먼저 켜 주세요. 여행마다 소스를 따로 고를 수도 있어요.",
                )}
              </span>
            )}
          </div>
        </div>
        {isNaver && (
          <>
            <Banner tone="warn" title={tr("Naver Flights terms of service", "네이버 항공권 이용약관")}>
              {naverTosWarning()}
            </Banner>
            {!provider.enabled && (
              <Checkbox
                checked={ack}
                onChange={setAck}
                label={tr(
                  "I understand the risk and want to be able to enable Naver as a default source.",
                  "위험을 이해했으며 네이버를 기본 소스로 켤 수 있게 하고 싶어요.",
                )}
              />
            )}
          </>
        )}
        {provider.requires_api_key && !provider.configured && (
          <span className="small">
            {tr("Add the API key under ", "이 소스를 사용하려면 ")}
            <a href="#api-keys">{tr("API keys", "API 키")}</a>
            {tr(" to use this source.", "에서 키를 추가하세요.")}
          </span>
        )}
        <ProviderOptions provider={provider} settings={settings} onSave={onSaveOptions} />
        <ProviderTester provider={provider} />
        {provider.supports_login && <SessionControls provider={provider} onChanged={onReload} />}
      </div>
    </li>
  );
}

/** Providers section. */
export function ProvidersSection({
  settings,
  providers,
  providersError,
  onSettings,
  reloadProviders,
}: {
  settings: RuntimeSettingsOut;
  providers: ProviderInfo[] | undefined;
  providersError: unknown;
  onSettings: (s: RuntimeSettingsOut) => void;
  reloadProviders: () => void;
}) {
  const saver = useSettingsSaver((s) => {
    onSettings(s);
    reloadProviders();
  });

  const toggleDefault = (key: string, on: boolean) => {
    if (!providers) return;
    const next = nextDefaultProviders(providers, key, on);
    // [] would mean "built-in defaults" to the backend; the last switch is disabled instead.
    if (next) void saver.save({ default_providers: next });
  };
  const saveOptions = (key: string, opts: Record<string, unknown>) =>
    void saver.save({ provider_options: { ...settings.provider_options, [key]: opts } });
  const enabledNames = (providers ?? [])
    .filter((p) => p.enabled)
    .map((p) => providerName(p))
    .join(", ");

  return (
    <SettingsCard
      id="providers"
      icon={<Server size={18} aria-hidden />}
      title={tr("Flight sources", "항공권 검색 소스")}
      desc={tr(
        "Default sources are searched for trips that don't pick their own. Unconfigured sources are skipped.",
        "검색 소스를 따로 고르지 않은 여행은 기본 소스로 검색해요. 설정되지 않은 소스는 건너뛰어요.",
      )}
      actions={<SaveStatus saved={saver.saved} error={saver.error} />}
    >
      <ErrorBanner error={providersError} title={tr("Could not load providers", "검색 소스를 불러오지 못했어요")} onRetry={reloadProviders} />
      {!providers && !providersError && <SkeletonBlock lines={4} label={tr("Loading providers", "검색 소스 불러오는 중")} />}
      {providers && (
        <>
          {settings.default_providers.length === 0 && (
            <Banner tone="info">
              {tr(
                `No explicit default list saved — sources marked “On by default” are used (${enabledNames || "none"}).`,
                `저장된 기본 소스 목록이 없어서 “기본 사용” 표시가 있는 소스를 사용해요 (${enabledNames || "없음"}).`,
              )}
            </Banner>
          )}
          <ul className="provider-list">
            {providers.map((p) => (
              <ProviderCard
                key={p.key}
                provider={p}
                settings={settings}
                lastDefault={isLastDefault(providers, p.key)}
                onToggleDefault={toggleDefault}
                onSaveOptions={saveOptions}
                onReload={reloadProviders}
              />
            ))}
          </ul>
          {settings.default_providers.length > 0 && (
            <div className="row">
              <Button size="sm" variant="ghost" icon={<Globe size={14} aria-hidden />} busy={saver.busy} onClick={() => void saver.save({ default_providers: [] })}>
                {tr("Reset to built-in defaults", "기본값으로 되돌리기")}
              </Button>
            </div>
          )}
        </>
      )}
    </SettingsCard>
  );
}
