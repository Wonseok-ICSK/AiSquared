/** Settings → API keys, AI, Browser & search. */

import { useEffect, useState } from "react";
import { Bot, Globe2, KeyRound, Save } from "lucide-react";
import type { ApiKeysOut, BrowserChannel, Language, LlmEffort, RuntimeSettingsOut } from "../../api/types";
import { BROWSER_CHANNELS, LLM_EFFORTS } from "../../api/types";
import { Button, Field, Segmented, Switch } from "../../components/ui";
import { getLang, labels, tr } from "../../lib/i18n";
import { SaveStatus, SecretInput, SettingsCard, useSettingsSaver } from "./shared";

type KeyName = keyof ApiKeysOut;

const EFFORT_NAME = labels<LlmEffort>({
  low: ["low", "낮음"],
  medium: ["medium", "보통"],
  high: ["high", "높음"],
  xhigh: ["xhigh", "매우 높음"],
  max: ["max", "최대"],
});

/** "높음 (high)" in Korean, the raw value in English. */
function effortLabel(effort: LlmEffort): string {
  return getLang() === "ko" ? `${EFFORT_NAME[effort]} (${effort})` : effort;
}

const KEY_LABEL: Record<KeyName, string> = {
  anthropic: "Anthropic (Claude)",
  serpapi: "SerpApi",
  google_maps: "Google Maps",
};

function keyHint(key: KeyName): string {
  if (key === "anthropic") {
    return tr(
      "Used for AI itineraries and refinements. Without it, a rule-based planner is used.",
      "AI 여행 일정 생성과 수정에 사용해요. 키가 없으면 규칙 기반 플래너를 사용해요.",
    );
  }
  if (key === "serpapi") {
    return tr(
      "Optional Google Flights API source (free plan: 250 searches/month).",
      "선택 사항 · Google Flights API 검색 소스 (무료 요금제: 월 250회 검색).",
    );
  }
  return tr("Optional; reserved for future place lookups.", "선택 사항 · 추후 장소 검색에 사용할 예정이에요.");
}

/** Write-only API keys with set/source state and clear buttons. */
export function ApiKeysSection({
  settings,
  onSettings,
}: {
  settings: RuntimeSettingsOut;
  onSettings: (s: RuntimeSettingsOut) => void;
}) {
  const saver = useSettingsSaver(onSettings);
  const [values, setValues] = useState<Record<KeyName, string>>({ anthropic: "", serpapi: "", google_maps: "" });
  const [clearing, setClearing] = useState<KeyName | null>(null);
  const staged = (Object.keys(values) as KeyName[]).filter((k) => values[k].trim() !== "");

  const save = async () => {
    const api_keys = Object.fromEntries(staged.map((k) => [k, values[k].trim()]));
    if (await saver.save({ api_keys })) setValues({ anthropic: "", serpapi: "", google_maps: "" });
  };
  const clear = async (key: KeyName) => {
    const label = KEY_LABEL[key];
    if (!window.confirm(tr(`Remove the saved ${label} key?`, `저장된 ${label} 키를 삭제할까요?`))) return;
    setClearing(key);
    await saver.save({ api_keys: { [key]: "" } });
    setClearing(null);
  };

  return (
    <SettingsCard
      id="api-keys"
      icon={<KeyRound size={18} aria-hidden />}
      title={tr("API keys", "API 키")}
      desc={tr(
        "Keys are stored encrypted on this PC and never shown again. Leave a box empty to keep the current key.",
        "키는 이 PC에 암호화되어 저장되며 다시 표시되지 않아요. 칸을 비워 두면 현재 키를 그대로 유지해요.",
      )}
      footer={
        <>
          <SaveStatus saved={saver.saved} error={saver.error} />
          <Button variant="primary" icon={<Save size={16} aria-hidden />} busy={saver.busy && clearing === null} disabled={staged.length === 0} onClick={() => void save()}>
            {tr("Save keys", "키 저장")}
          </Button>
        </>
      }
    >
      {(Object.keys(KEY_LABEL) as KeyName[]).map((key) => (
        <div key={key} className="stack-xs">
          <SecretInput
            id={`key-${key}`}
            label={KEY_LABEL[key]}
            state={settings.api_keys[key]}
            value={values[key]}
            onChange={(v) => setValues((s) => ({ ...s, [key]: v }))}
            onClear={() => void clear(key)}
            clearing={clearing === key}
            describedBy={`key-${key}-hint`}
          />
          <span className="field__hint" id={`key-${key}-hint`}>
            {keyHint(key)}
          </span>
        </div>
      ))}
    </SettingsCard>
  );
}

/** Model / effort / language. */
export function AiSection({ settings, onSettings }: { settings: RuntimeSettingsOut; onSettings: (s: RuntimeSettingsOut) => void }) {
  const saver = useSettingsSaver(onSettings);
  const [model, setModel] = useState(settings.llm_model);
  const [effort, setEffort] = useState<LlmEffort>(settings.llm_effort);
  const [language, setLanguage] = useState<Language>(settings.language);
  useEffect(() => {
    setModel(settings.llm_model);
    setEffort(settings.llm_effort);
    setLanguage(settings.language);
  }, [settings.llm_model, settings.llm_effort, settings.language]);
  const modelError = model.length > 100 ? tr("At most 100 characters", "최대 100자까지 입력할 수 있어요") : null;
  const languageLabel = tr("Itinerary & notification language", "일정·알림 언어");

  return (
    <SettingsCard
      id="ai"
      icon={<Bot size={18} aria-hidden />}
      title="AI"
      desc={tr("Model and language for itineraries and notification text.", "여행 일정과 알림 문구에 사용할 모델과 언어.")}
      footer={
        <>
          <SaveStatus saved={saver.saved} error={saver.error} />
          <Button
            variant="primary"
            icon={<Save size={16} aria-hidden />}
            busy={saver.busy}
            disabled={Boolean(modelError)}
            onClick={() => void saver.save({ llm_model: model.trim() || null, llm_effort: effort, language })}
          >
            {tr("Save", "저장")}
          </Button>
        </>
      }
    >
      <div className="form-grid">
        <Field
          label={tr("Claude model", "Claude 모델")}
          className="span-6"
          error={modelError}
          hint={tr("Leave blank to reset to the .env default (claude-opus-5-5).", "비워 두면 .env 기본값(claude-opus-5-5)으로 돌아가요.")}
        >
          {({ id, describedBy, invalid }) => (
            <input id={id} className="input mono" value={model} placeholder="claude-opus-5-5" aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => setModel(e.target.value)} />
          )}
        </Field>
        <Field
          label={tr("Effort", "추론 강도")}
          className="span-3"
          hint={tr("Higher = more thorough, slower, costlier", "높을수록 더 꼼꼼하지만 느리고 비용이 늘어요")}
        >
          {({ id, describedBy }) => (
            <select id={id} className="select" value={effort} aria-describedby={describedBy} onChange={(e) => setEffort(e.target.value as LlmEffort)}>
              {LLM_EFFORTS.map((e) => (
                <option key={e} value={e}>
                  {effortLabel(e)}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field
          label={languageLabel}
          className="span-3"
          hint={tr(
            "For generated itineraries and notifications (Telegram, email…). The page language is set in the menu at the top.",
            "생성되는 여행 일정과 알림(텔레그램, 이메일 등)의 언어예요. 화면 언어는 상단 메뉴에서 바꿔요.",
          )}
        >
          {() => (
            <Segmented
              label={languageLabel}
              value={language}
              onChange={setLanguage}
              options={[
                { value: "ko", label: "한국어" },
                { value: "en", label: "English" },
              ]}
            />
          )}
        </Field>
      </div>
    </SettingsCard>
  );
}

/** Browser channel/headless + max search combinations. */
export function BrowserSection({ settings, onSettings }: { settings: RuntimeSettingsOut; onSettings: (s: RuntimeSettingsOut) => void }) {
  const saver = useSettingsSaver(onSettings);
  const channelValue = (BROWSER_CHANNELS as readonly string[]).includes(settings.browser_channel)
    ? (settings.browser_channel as BrowserChannel)
    : "msedge";
  const [channel, setChannel] = useState<BrowserChannel>(channelValue);
  const [headless, setHeadless] = useState(settings.browser_headless);
  const [maxCombos, setMaxCombos] = useState(String(settings.max_search_combinations));
  useEffect(() => {
    setChannel(channelValue);
    setHeadless(settings.browser_headless);
    setMaxCombos(String(settings.max_search_combinations));
  }, [channelValue, settings.browser_headless, settings.max_search_combinations]);
  const combos = Number(maxCombos);
  const combosError =
    !Number.isInteger(combos) || combos < 1 || combos > 60
      ? tr("Whole number between 1 and 60", "1~60 사이의 정수를 입력하세요")
      : null;

  return (
    <SettingsCard
      id="browser"
      icon={<Globe2 size={18} aria-hidden />}
      title={tr("Browser & search", "브라우저 및 검색")}
      desc={tr(
        "Browser automation for sources without an API, and how widely flexible searches expand.",
        "API가 없는 검색 소스를 위한 브라우저 자동화와, 유연한 검색을 얼마나 넓게 펼칠지 설정해요.",
      )}
      footer={
        <>
          <SaveStatus saved={saver.saved} error={saver.error} />
          <Button
            variant="primary"
            icon={<Save size={16} aria-hidden />}
            busy={saver.busy}
            disabled={Boolean(combosError)}
            onClick={() => void saver.save({ browser_channel: channel, browser_headless: headless, max_search_combinations: combos })}
          >
            {tr("Save", "저장")}
          </Button>
        </>
      }
    >
      <div className="form-grid">
        <Field
          label={tr("Browser", "브라우저")}
          className="span-4"
          hint={tr(
            "Microsoft Edge is used first; Chrome or bundled Chromium as fallback.",
            "Microsoft Edge를 먼저 사용하고, 안 되면 Chrome 또는 내장 Chromium을 사용해요.",
          )}
        >
          {({ id, describedBy }) => (
            <select id={id} className="select" value={channel} aria-describedby={describedBy} onChange={(e) => setChannel(e.target.value as BrowserChannel)}>
              <option value="msedge">Microsoft Edge (msedge)</option>
              <option value="chrome">Google Chrome (chrome)</option>
              <option value="chromium">{tr("Bundled Chromium (chromium)", "내장 Chromium (chromium)")}</option>
            </select>
          )}
        </Field>
        <div className="span-4 field" style={{ justifyContent: "center" }}>
          <Switch
            checked={headless}
            onChange={setHeadless}
            label={tr("Run headless (no visible window)", "헤드리스 실행 (창을 띄우지 않음)")}
          />
        </div>
        <Field
          label={tr("Max search combinations per check", "확인당 최대 검색 조합 수")}
          className="span-4"
          error={combosError}
          hint={tr("Caps flexible dates × airports (1–60).", "유연한 날짜 × 공항 조합의 상한이에요 (1–60).")}
        >
          {({ id, describedBy, invalid }) => (
            <input id={id} className="input input--num" inputMode="numeric" value={maxCombos} aria-describedby={describedBy} aria-invalid={invalid || undefined} onChange={(e) => setMaxCombos(e.target.value)} />
          )}
        </Field>
      </div>
    </SettingsCard>
  );
}
