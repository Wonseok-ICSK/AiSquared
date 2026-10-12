/**
 * Flight-provider info that the backend sends in English (GET /api/providers, provider
 * tests, login sessions), shown in the current UI language. In English the backend text is
 * returned as is; in Korean known keys and message shapes are translated and anything
 * unrecognised falls back to the backend text unchanged.
 */

import type { ProviderInfo } from "../api/types";
import { getLang } from "./i18n";

function isKorean(): boolean {
  return getLang() === "ko";
}

const NAVER_TOS_KO =
  "네이버 이용약관은 허가 없이 매크로·봇·스크래퍼 등으로 자동 수집하는 것을 금지해요. " +
  "개인적인 소량 이용에만 사용하고, 그에 따른 위험은 본인이 부담해요.";

const NAME: Record<string, string> = {
  google_flights: "Google Flights",
  serpapi: "Google Flights (SerpApi)",
  naver: "네이버 항공권 (선택 사용)",
  mock: "Mock (데모 데이터)",
};

const DESCRIPTION: Record<string, string> = {
  google_flights:
    "API 키 없이 Google Flights 검색 결과 페이지를 읽어요. 기본으로 설치된 Edge(헤드리스)를 사용하고, " +
    "설정에서 일반 HTTP로 바꿀 수 있어요. 가격은 Google에 표시되는 요금으로, 모든 탑승객의 왕복 총액이에요. " +
    "개인적인 소량 이용 전용이며, 차단이나 캡차가 나타나면 바로 멈춰요.",
  serpapi:
    "SerpApi의 JSON API로 Google Flights 결과를 가져와요. SerpApi 키가 필요해요 " +
    "(무료 요금제: 월 250회·시간당 50회, 날짜/공항 조합마다 검색 1회). " +
    "가격은 Google에 표시되는 요금으로, 모든 탑승객의 왕복 총액이에요.",
  naver:
    "네이버 항공권 검색 결과(국내 여행사 및 항공사 직판 채널, 원화)를 화면 밖에 띄운 실제 Microsoft Edge 창에서 읽어요. " +
    `기본으로 꺼져 있어요. 경고: ${NAVER_TOS_KO} ` +
    "확인 한 번에 최대 4회 검색하며 검색 간격은 15–30초예요. 검색이 거부되면 이 소스를 24시간 동안 일시 중지해요. " +
    "가격은 '모든 결제수단'(A01) 기준 성인 1인 요금(세금·유류할증료 포함)이며, 카드사 조건부 할인은 반영하지 않아요. " +
    "네이버 데이터는 AI 플래너로 절대 보내지 않도록 표시돼요(네이버는 AI/RAG 이용을 금지해요).",
  mock: "개발과 테스트용으로 항상 같은 가짜 항공권을 만들어요. 실제 가격이 아니에요.",
};

/** Display name for a provider (Korean when known and the UI is Korean). */
export function providerName(provider: Pick<ProviderInfo, "key" | "display_name">): string {
  return (isKorean() ? NAME[provider.key] : undefined) ?? provider.display_name;
}

/** Description for a provider (Korean when known and the UI is Korean). */
export function providerDescription(provider: Pick<ProviderInfo, "key" | "description">): string {
  return (isKorean() ? DESCRIPTION[provider.key] : undefined) ?? provider.description;
}

type Rule = [RegExp, (...groups: string[]) => string];

const SERPAPI_REASON: Rule[] = [
  [/^hourly search limit reached \(HTTP 429\)$/, () => "시간당 검색 한도에 도달했어요 (HTTP 429)"],
  [/^search limit reached \(HTTP 429\)$/, () => "검색 한도에 도달했어요 (HTTP 429)"],
  [
    /^no SerpApi searches left this month \(HTTP 429(?:; plan renews (\S+))?\)$/,
    (renews) => `이번 달 SerpApi 검색 횟수를 모두 썼어요 (HTTP 429${renews ? ` · ${renews}에 갱신` : ""})`,
  ],
];

const STATUS_PART: Rule[] = [
  [/^API key configured$/, () => "API 키 설정됨"],
  [/^API key '(.+)' is not set$/, (name) => `API 키 '${name}'이(가) 설정되지 않았어요`],
  [
    /^No API key needed; fetch mode: (plain HTTP|browser \(headless Edge\))(?:; paused after a Google block until (.+))?$/,
    (mode, until) =>
      `API 키 필요 없음 · 가져오기 방식: ${mode === "plain HTTP" ? "일반 HTTP" : "브라우저(헤드리스 Edge)"}` +
      (until ? ` · Google 차단으로 ${until}까지 일시 중지` : ""),
  ],
  [
    /^Browser automation is not available \(needs Microsoft Edge \+ Playwright\)$/,
    () => "브라우저 자동화를 사용할 수 없어요 (Microsoft Edge와 Playwright가 필요해요)",
  ],
  [/^Opt-in source\. Naver's terms of service forbid/, () => `선택해서 사용하는 소스예요. ${NAVER_TOS_KO}`],
  [
    /^Naver Flights paused until (.+?) \(or an app restart\): (.*)$/,
    (until, reason) => `네이버 항공권은 ${until}까지(또는 앱을 다시 시작할 때까지) 일시 중지됐어요 · 사유: ${reason}`,
  ],
  [
    /^SerpApi API key is not set \(Settings → API keys; free plan 250 searches\/month\)$/,
    () => "SerpApi API 키가 설정되지 않았어요 (설정 → API 키 · 무료 요금제 월 250회 검색)",
  ],
  [
    /^SerpApi paused until (.+?): (.*)$/,
    (until, reason) => `SerpApi는 ${until}까지 일시 중지됐어요 · 사유: ${translate(reason, SERPAPI_REASON)}`,
  ],
  [/^Login window open: log in there, then close the window\.$/, () => "로그인 창이 열려 있어요. 그 창에서 로그인한 뒤 창을 닫으세요."],
  [/^Last login attempt: (.*)$/, (what) => `마지막 로그인 시도: ${what}`],
  [/^could not be initialised: (.*)$/, (why) => `초기화하지 못했어요: ${why}`],
];

function translate(text: string, rules: Rule[]): string {
  for (const [re, fn] of rules) {
    const m = re.exec(text);
    if (m) return fn(...m.slice(1).map((g) => g ?? ""));
  }
  return text;
}

/** Provider status line ("a · b" parts joined by the backend), translated part by part. */
export function providerMessage(message: string): string {
  if (!message || !isKorean()) return message;
  // The ToS warning itself contains no " · ", so splitting on it is safe.
  return message
    .split(" · ")
    .map((part) => translate(part, STATUS_PART))
    .join(" · ");
}

/** "ICN → NRT, 2026-11-07 – 2026-11-11, 1 adult" (the test query). */
function testQuery(text: string): string {
  return translate(text, [
    [
      /^([A-Z]{3}) → ([A-Z]{3}), (\d{4}-\d{2}-\d{2})(?: – (\d{4}-\d{2}-\d{2}))?, (\d+) adults?$/,
      (from, to, depart, ret, adults) => `${from} → ${to}, ${depart}${ret ? ` – ${ret}` : ""}, 성인 ${adults}명`,
    ],
  ]);
}

/** Message of a provider test result (running / ok / error). */
export function providerTestMessage(message: string): string {
  if (!message || !isKorean()) return message;
  return translate(message, [
    [/^The search worked but returned no offers \((.*)\)$/, (q) => `검색은 됐지만 결과가 없어요 (${testQuery(q)})`],
    [/^Test search did not finish within (\d+) minutes$/, (n) => `테스트 검색이 ${n}분 안에 끝나지 않았어요`],
    [/^Not configured: (.*)$/, (why) => `설정 안 됨: ${why === "missing configuration" ? "설정이 빠져 있어요" : providerMessage(why)}`],
    [/^Blocked by the site: (.*)$/, (why) => `사이트에서 차단됨: ${why}`],
    [/^could not be initialised: (.*)$/, (why) => `초기화하지 못했어요: ${why}`],
    [/^([A-Z]{3}) → ([A-Z]{3}), .*adults?$/, () => testQuery(message)],
  ]);
}

/** Message returned by the login / forget-session endpoints. */
export function providerSessionMessage(message: string): string {
  if (!message || !isKorean()) return message;
  return translate(message, [
    [
      /^A browser window is opening for (.+)\. Log in there, then close the window; the session is saved automatically \(the window closes after (\d+) minutes\)\.$/,
      (name, minutes) =>
        `${name} 로그인용 브라우저 창이 열리고 있어요. 그 창에서 로그인한 뒤 창을 닫으면 세션이 자동으로 저장돼요 (${minutes}분 뒤 창이 자동으로 닫혀요).`,
    ],
    [
      /^A login window for (.+) is already open\. Log in there, then close the window\.$/,
      (name) => `${name} 로그인 창이 이미 열려 있어요. 그 창에서 로그인한 뒤 창을 닫으세요.`,
    ],
    [/^Saved (.+) login session removed\.$/, (name) => `저장된 ${name} 로그인 세션을 삭제했어요.`],
    [
      /^Session forgotten, but some browser profile files could not be deleted/,
      () => "세션은 삭제했지만 일부 브라우저 프로필 파일을 지우지 못했어요 (열려 있는 브라우저 창을 닫고 다시 시도하세요).",
    ],
  ]);
}
