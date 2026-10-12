/** Settings → Notification channels: in-app, desktop, Telegram, e-mail, ntfy, KakaoTalk. */

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { BellRing, ExternalLink, Mail, MessageCircle, Monitor, Radio, Save, Search, Send } from "lucide-react";
import { KAKAO_AUTHORIZE_URL, api } from "../../api/endpoints";
import { errorMessage } from "../../api/client";
import type { NotificationSettingsIn, RuntimeSettingsOut } from "../../api/types";
import { TagInput } from "../../components/TagInput";
import { Badge, Banner, Button, Checkbox, Field, Switch } from "../../components/ui";
import { formatUtc } from "../../lib/format";
import { josa, tr } from "../../lib/i18n";
import { SaveStatus, SecretInput, SettingsCard, TestSendButton, useSettingsSaver } from "./shared";

type OnSettings = (s: RuntimeSettingsOut) => void;

function ChannelCard({
  icon,
  title,
  subtitle,
  enabled,
  onToggle,
  children,
  footer,
}: {
  icon: ReactNode;
  title: string;
  subtitle?: ReactNode;
  enabled?: boolean;
  onToggle?: (v: boolean) => void;
  children?: ReactNode;
  footer: ReactNode;
}) {
  return (
    <li className={`channel${enabled === false ? " is-off" : ""}`}>
      <div className="channel__head">
        <span className="channel__icon" aria-hidden>
          {icon}
        </span>
        <div className="stack-xs grow">
          <h3 className="channel__title">{title}</h3>
          {subtitle && <span className="xsmall subtle">{subtitle}</span>}
        </div>
        {onToggle && enabled !== undefined && <Switch checked={enabled} onChange={onToggle} label={tr("Enabled", "사용")} />}
        {!onToggle && <Badge tone="good">{tr("Always on", "항상 켜짐")}</Badge>}
      </div>
      {children && <div className="channel__body stack-sm">{children}</div>}
      <div className="channel__foot">{footer}</div>
    </li>
  );
}

function useChannelSave(onSettings: OnSettings) {
  const saver = useSettingsSaver(onSettings);
  const save = (notifications: NotificationSettingsIn) => saver.save({ notifications });
  return { ...saver, save };
}

function SaveButton({ busy, onClick, disabled }: { busy: boolean; onClick: () => void; disabled?: boolean }) {
  return (
    <Button size="sm" className="btn--soft" icon={<Save size={14} aria-hidden />} busy={busy} disabled={disabled} onClick={onClick}>
      {tr("Save", "저장")}
    </Button>
  );
}

function DesktopChannel({ settings, onSettings }: { settings: RuntimeSettingsOut; onSettings: OnSettings }) {
  const s = useChannelSave(onSettings);
  return (
    <ChannelCard
      icon={<Monitor size={16} aria-hidden />}
      title={tr("Desktop (Windows)", "Windows 알림")}
      subtitle={tr("A Windows toast; clicking it opens the booking page.", "Windows 알림 팝업을 띄우고, 누르면 예약 페이지가 열려요.")}
      enabled={settings.notifications.desktop.enabled}
      onToggle={(v) => void s.save({ desktop: { enabled: v } })}
      footer={
        <>
          <SaveStatus saved={s.saved} error={s.error} />
          <TestSendButton channel="desktop" />
        </>
      }
    />
  );
}

function TelegramChannel({ settings, onSettings }: { settings: RuntimeSettingsOut; onSettings: OnSettings }) {
  const cfg = settings.notifications.telegram;
  const s = useChannelSave(onSettings);
  const [enabled, setEnabled] = useState(cfg.enabled);
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState(cfg.chat_id ?? "");
  const [detecting, setDetecting] = useState(false);
  const [detectMsg, setDetectMsg] = useState<{ tone: "good" | "error"; text: string } | null>(null);
  useEffect(() => {
    setEnabled(cfg.enabled);
    setChatId(cfg.chat_id ?? "");
  }, [cfg.enabled, cfg.chat_id]);

  const detect = async () => {
    setDetecting(true);
    setDetectMsg(null);
    try {
      if (token.trim()) await s.save({ telegram: { bot_token: token.trim() } });
      const r = await api.detectTelegramChat();
      if (r.chat_id === null || r.chat_id === undefined || r.chat_id === "") {
        setDetectMsg({
          tone: "error",
          text: tr(
            "No chat found. Send /start to your bot, then try again (within 24 h).",
            "채팅을 찾지 못했어요. 봇에게 /start를 보낸 뒤 24시간 안에 다시 시도하세요.",
          ),
        });
      } else {
        setChatId(String(r.chat_id));
        const who = `${r.chat_id}${r.username ? ` (@${r.username})` : ""}`;
        setDetectMsg({
          tone: "good",
          text: tr(`Found chat ${who}. Saved.`, `채팅 ${josa(who, "을/를")} 찾아서 저장했어요.`),
        });
      }
    } catch (err) {
      setDetectMsg({ tone: "error", text: errorMessage(err) });
    } finally {
      setDetecting(false);
    }
  };
  const save = async () => {
    if (await s.save({ telegram: { enabled, chat_id: chatId.trim() || null, ...(token.trim() ? { bot_token: token.trim() } : {}) } })) {
      setToken("");
    }
  };
  return (
    <ChannelCard
      icon={<Send size={16} aria-hidden />}
      title="Telegram"
      subtitle={tr(
        "Recommended phone push: create a bot with @BotFather, paste its token, send /start to it, then detect the chat ID.",
        "휴대폰 푸시 알림으로 추천해요. @BotFather로 봇을 만들고 토큰을 붙여 넣은 뒤, 봇에게 /start를 보내고 채팅 ID를 찾으세요.",
      )}
      enabled={enabled}
      onToggle={setEnabled}
      footer={
        <>
          <SaveStatus saved={s.saved} error={s.error} />
          <TestSendButton channel="telegram" />
          <SaveButton busy={s.busy} onClick={() => void save()} />
        </>
      }
    >
      <SecretInput
        id="tg-token"
        label={tr("Bot token", "봇 토큰")}
        state={cfg.bot_token}
        value={token}
        onChange={setToken}
        onClear={() => void s.save({ telegram: { bot_token: "" } })}
      />
      <Field
        label={tr("Chat ID", "채팅 ID")}
        hint={tr(
          "Detection reads the bot's recent messages (Telegram keeps them for 24 h).",
          "봇이 최근 받은 메시지에서 찾아요 (Telegram은 메시지를 24시간 동안 보관해요).",
        )}
      >
        {({ id, describedBy }) => (
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <input id={id} className="input mono" value={chatId} placeholder={tr("e.g. 123456789", "예: 123456789")} aria-describedby={describedBy} onChange={(e) => setChatId(e.target.value)} />
            <Button size="sm" icon={<Search size={14} aria-hidden />} busy={detecting} onClick={() => void detect()}>
              {tr("Detect chat ID", "채팅 ID 찾기")}
            </Button>
          </div>
        )}
      </Field>
      <span aria-live="polite">{detectMsg && <Banner tone={detectMsg.tone}>{detectMsg.text}</Banner>}</span>
    </ChannelCard>
  );
}

const EMAIL_PRESETS = {
  gmail: { label: ["Gmail", "Gmail"], host: "smtp.gmail.com", port: 587 },
  naver: { label: ["Naver", "네이버"], host: "smtp.naver.com", port: 587 },
} as const;

function EmailChannel({ settings, onSettings }: { settings: RuntimeSettingsOut; onSettings: OnSettings }) {
  const cfg = settings.notifications.email;
  const s = useChannelSave(onSettings);
  const [enabled, setEnabled] = useState(cfg.enabled);
  const [host, setHost] = useState(cfg.smtp_host ?? "");
  const [port, setPort] = useState(String(cfg.smtp_port));
  const [username, setUsername] = useState(cfg.username ?? "");
  const [password, setPassword] = useState("");
  const [from, setFrom] = useState(cfg.from_addr ?? "");
  const [to, setTo] = useState<string[]>(cfg.to_addrs);
  const [starttls, setStarttls] = useState(cfg.use_starttls);
  // Resync only when a stored value changes: `cfg` is a new object after every settings PUT
  // (any section) and every reload, which must not wipe unsaved edits here.
  const toKey = cfg.to_addrs.join("\n");
  useEffect(() => {
    setEnabled(cfg.enabled);
    setHost(cfg.smtp_host ?? "");
    setPort(String(cfg.smtp_port));
    setUsername(cfg.username ?? "");
    setFrom(cfg.from_addr ?? "");
    setTo(toKey ? toKey.split("\n") : []);
    setStarttls(cfg.use_starttls);
  }, [cfg.enabled, cfg.smtp_host, cfg.smtp_port, cfg.username, cfg.from_addr, toKey, cfg.use_starttls]);

  const portNum = Number(port);
  const portError = !Number.isInteger(portNum) || portNum < 1 || portNum > 65535 ? "1–65535" : null;
  const save = async () => {
    const ok = await s.save({
      email: {
        enabled,
        smtp_host: host.trim() || null,
        smtp_port: portNum,
        username: username.trim() || null,
        from_addr: from.trim() || null,
        to_addrs: to,
        use_starttls: starttls,
        ...(password ? { password } : {}),
      },
    });
    if (ok) setPassword("");
  };
  const emailOk = (raw: string) => (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(raw.trim()) ? raw.trim() : null);

  return (
    <ChannelCard
      icon={<Mail size={16} aria-hidden />}
      title={tr("E-mail (SMTP)", "이메일 (SMTP)")}
      subtitle={tr(
        "Gmail and Naver need 2-step verification and an app password (not your normal password).",
        "Gmail과 네이버는 2단계 인증과 앱 비밀번호가 필요해요 (평소 쓰는 비밀번호가 아니에요).",
      )}
      enabled={enabled}
      onToggle={setEnabled}
      footer={
        <>
          <SaveStatus saved={s.saved} error={s.error} />
          <TestSendButton channel="email" />
          <SaveButton busy={s.busy} disabled={Boolean(portError)} onClick={() => void save()} />
        </>
      }
    >
      <div className="row">
        <span className="xsmall subtle">{tr("Presets:", "빠른 설정:")}</span>
        {Object.values(EMAIL_PRESETS).map((p) => (
          <Button
            key={p.host}
            size="sm"
            variant="ghost"
            onClick={() => {
              setHost(p.host);
              setPort(String(p.port));
              setStarttls(true);
              if (!from && username) setFrom(username);
            }}
          >
            {tr(p.label[0], p.label[1])} ({p.host})
          </Button>
        ))}
      </div>
      <div className="form-grid">
        <Field label={tr("SMTP server", "SMTP 서버")} className="span-8">
          {({ id }) => <input id={id} className="input mono" value={host} placeholder="smtp.gmail.com" onChange={(e) => setHost(e.target.value)} />}
        </Field>
        <Field label={tr("Port", "포트")} className="span-4" error={portError}>
          {({ id, invalid }) => <input id={id} className="input input--num" inputMode="numeric" value={port} aria-invalid={invalid || undefined} onChange={(e) => setPort(e.target.value)} />}
        </Field>
        <Field label={tr("Username", "사용자 이름")} className="span-6">
          {({ id }) => <input id={id} className="input" autoComplete="off" value={username} placeholder="you@gmail.com" onChange={(e) => setUsername(e.target.value)} />}
        </Field>
        <div className="span-6">
          <SecretInput
            id="smtp-password"
            label={tr("App password", "앱 비밀번호")}
            state={cfg.password}
            value={password}
            onChange={setPassword}
            onClear={() => void s.save({ email: { password: "" } })}
          />
        </div>
        <Field label={tr("From address", "보내는 주소")} className="span-6">
          {({ id }) => <input id={id} className="input" value={from} placeholder="you@gmail.com" onChange={(e) => setFrom(e.target.value)} />}
        </Field>
        <Field label={tr("Send to", "받는 주소")} className="span-6" hint={tr("Enter or comma to add addresses", "Enter 또는 쉼표로 주소를 추가하세요")}>
          {({ id, describedBy }) => <TagInput id={id} describedBy={describedBy} value={to} onChange={setTo} transform={emailOk} placeholder="you@example.com" />}
        </Field>
        <div className="span-12">
          <Checkbox checked={starttls} onChange={setStarttls} label={tr("Use STARTTLS (port 587)", "STARTTLS 사용 (포트 587)")} />
        </div>
      </div>
      <p className="xsmall subtle">
        {tr(
          "Gmail: myaccount.google.com/apppasswords (2-step verification required). Naver: 내 정보 → 보안 설정 → 2단계 인증 → 애플리케이션 비밀번호, and enable POP3/IMAP in Naver Mail settings.",
          "Gmail: myaccount.google.com/apppasswords (2단계 인증 필요). 네이버: 내 정보 → 보안 설정 → 2단계 인증 → 애플리케이션 비밀번호를 만들고, 네이버 메일 환경설정에서 POP3/IMAP을 사용으로 설정하세요.",
        )}
      </p>
    </ChannelCard>
  );
}

function NtfyChannel({ settings, onSettings }: { settings: RuntimeSettingsOut; onSettings: OnSettings }) {
  const cfg = settings.notifications.ntfy;
  const s = useChannelSave(onSettings);
  const [enabled, setEnabled] = useState(cfg.enabled);
  const [server, setServer] = useState(cfg.server);
  const [topic, setTopic] = useState(cfg.topic ?? "");
  const [token, setToken] = useState("");
  useEffect(() => {
    setEnabled(cfg.enabled);
    setServer(cfg.server);
    setTopic(cfg.topic ?? "");
  }, [cfg.enabled, cfg.server, cfg.topic]);
  const save = async () => {
    if (await s.save({ ntfy: { enabled, server: server.trim() || null, topic: topic.trim() || null, ...(token ? { token } : {}) } })) setToken("");
  };
  return (
    <ChannelCard
      icon={<Radio size={16} aria-hidden />}
      title="ntfy"
      subtitle={tr(
        "Free push via the ntfy app: subscribe to a long, unguessable topic name (it acts as the password).",
        "ntfy 앱으로 받는 무료 푸시 알림이에요. 길고 추측하기 어려운 토픽 이름을 구독하세요 (토픽 이름이 비밀번호 역할을 해요).",
      )}
      enabled={enabled}
      onToggle={setEnabled}
      footer={
        <>
          <SaveStatus saved={s.saved} error={s.error} />
          <TestSendButton channel="ntfy" />
          <SaveButton busy={s.busy} onClick={() => void save()} />
        </>
      }
    >
      <div className="form-grid">
        <Field label={tr("Server", "서버")} className="span-6">
          {({ id }) => <input id={id} className="input mono" value={server} placeholder="https://ntfy.sh" onChange={(e) => setServer(e.target.value)} />}
        </Field>
        <Field label={tr("Topic", "토픽")} className="span-6">
          {({ id }) => <input id={id} className="input mono" value={topic} placeholder="flights-x7q2k9…" onChange={(e) => setTopic(e.target.value)} />}
        </Field>
        <div className="span-12">
          <SecretInput id="ntfy-token" label={tr("Access token (optional)", "액세스 토큰 (선택)")} state={cfg.token} value={token} onChange={setToken} onClear={() => void s.save({ ntfy: { token: "" } })} />
        </div>
      </div>
    </ChannelCard>
  );
}

function KakaoChannel({ settings, onSettings }: { settings: RuntimeSettingsOut; onSettings: OnSettings }) {
  const cfg = settings.notifications.kakao;
  const s = useChannelSave(onSettings);
  const [enabled, setEnabled] = useState(cfg.enabled);
  const [restKey, setRestKey] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [redirect, setRedirect] = useState(cfg.redirect_uri ?? "");
  useEffect(() => {
    setEnabled(cfg.enabled);
    setRedirect(cfg.redirect_uri ?? "");
  }, [cfg.enabled, cfg.redirect_uri]);
  const save = async () => {
    const ok = await s.save({
      kakao: {
        enabled,
        redirect_uri: redirect.trim() || null,
        ...(restKey ? { rest_api_key: restKey } : {}),
        ...(clientSecret ? { client_secret: clientSecret } : {}),
      },
    });
    if (ok) {
      setRestKey("");
      setClientSecret("");
    }
  };
  const connected = cfg.access_token.is_set || cfg.refresh_token.is_set;
  return (
    <ChannelCard
      icon={<MessageCircle size={16} aria-hidden />}
      title={tr("KakaoTalk", "카카오톡")}
      subtitle={tr(
        "Records to chat-with-myself, no push alert (KakaoTalk 나에게 보내기).",
        "카카오톡 ‘나에게 보내기’로 기록돼요. 푸시 알림은 오지 않아요.",
      )}
      enabled={enabled}
      onToggle={setEnabled}
      footer={
        <>
          <SaveStatus saved={s.saved} error={s.error} />
          <TestSendButton channel="kakao" />
          <SaveButton busy={s.busy} onClick={() => void save()} />
        </>
      }
    >
      <div className="form-grid">
        <div className="span-6">
          <SecretInput id="kakao-rest" label={tr("REST API key", "REST API 키")} state={cfg.rest_api_key} value={restKey} onChange={setRestKey} onClear={() => void s.save({ kakao: { rest_api_key: "" } })} />
        </div>
        <div className="span-6">
          <SecretInput id="kakao-secret" label={tr("Client secret", "클라이언트 시크릿")} state={cfg.client_secret} value={clientSecret} onChange={setClientSecret} onClear={() => void s.save({ kakao: { client_secret: "" } })} />
        </div>
        <Field
          label="Redirect URI"
          className="span-12"
          hint={tr(
            "Must exactly match the URI registered in the Kakao developer console.",
            "Kakao Developers 콘솔에 등록한 URI와 정확히 같아야 해요.",
          )}
        >
          {({ id, describedBy }) => (
            <input id={id} className="input mono" value={redirect} placeholder="http://127.0.0.1:8765/api/integrations/kakao/callback" aria-describedby={describedBy} onChange={(e) => setRedirect(e.target.value)} />
          )}
        </Field>
      </div>
      <div className="row-between kakao-connect">
        <div className="stack-xs">
          <span className="row" style={{ gap: 6 }}>
            <span className="small strong">{tr("Connection", "연결")}</span>
            {connected ? (
              <Badge tone="good">{tr("Connected", "연결됨")}</Badge>
            ) : (
              <Badge tone="outline">{tr("Not connected", "연결 안 됨")}</Badge>
            )}
          </span>
          {cfg.access_expires_at && (
            <span className="xsmall subtle">
              {tr("Access token until", "액세스 토큰 만료:")} {formatUtc(cfg.access_expires_at)}
            </span>
          )}
          {cfg.refresh_expires_at && (
            <span className="xsmall subtle">
              {tr("Refresh token until", "리프레시 토큰 만료:")} {formatUtc(cfg.refresh_expires_at)}
            </span>
          )}
        </div>
        <a className="btn btn--sm" href={KAKAO_AUTHORIZE_URL} target="_blank" rel="noopener">
          {tr("Connect KakaoTalk", "카카오톡 연결")} <ExternalLink size={13} aria-hidden />
        </a>
      </div>
      <p className="xsmall subtle">
        {tr(
          "Save the REST API key and redirect URI first, then connect. Link domains in messages must be registered in the Kakao console.",
          "REST API 키와 Redirect URI를 먼저 저장한 뒤 연결하세요. 메시지에 들어가는 링크 도메인은 Kakao 콘솔에 등록되어 있어야 해요.",
        )}
      </p>
    </ChannelCard>
  );
}

/** Notification channels section. */
export function NotificationsSection({ settings, onSettings }: { settings: RuntimeSettingsOut; onSettings: OnSettings }) {
  return (
    <SettingsCard
      id="notifications"
      icon={<BellRing size={18} aria-hidden />}
      title={tr("Notification channels", "알림 채널")}
      desc={tr(
        "Price alerts go to every enabled channel. One failing channel never blocks the others.",
        "가격 알림은 켜져 있는 모든 채널로 보내요. 한 채널이 실패해도 다른 채널에는 영향이 없어요.",
      )}
    >
      <ul className="channel-list">
        <ChannelCard
          icon={<BellRing size={16} aria-hidden />}
          title={tr("In-app", "앱 내 알림")}
          subtitle={tr("The bell in the header and the Notifications page.", "상단의 종 아이콘과 알림 페이지에 표시돼요.")}
          footer={<TestSendButton channel="inapp" />}
        />
        <DesktopChannel settings={settings} onSettings={onSettings} />
        <TelegramChannel settings={settings} onSettings={onSettings} />
        <EmailChannel settings={settings} onSettings={onSettings} />
        <NtfyChannel settings={settings} onSettings={onSettings} />
        <KakaoChannel settings={settings} onSettings={onSettings} />
      </ul>
    </SettingsCard>
  );
}
