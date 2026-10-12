/** App shell: sidebar navigation (icon rail on narrow screens), top bar with breadcrumb,
 * language menu, notification bell and theme toggle, main outlet, footer. */

import { Suspense, useEffect, useId, useRef, useState } from "react";
import { NavLink, Outlet, Link, useLocation } from "react-router-dom";
import {
  ArrowUpRight,
  Bell,
  Check,
  ChevronDown,
  ChevronRight,
  Compass,
  Globe,
  Languages,
  Monitor,
  Moon,
  Plane,
  PlusCircle,
  Send,
  Settings,
  Sun,
} from "lucide-react";
import { useApp } from "../lib/appContext";
import { LANGS, tr, useLang } from "../lib/i18n";
import type { Lang } from "../lib/i18n";
import { useTheme } from "../lib/theme";
import { SkeletonBlock } from "./ui";

const LANG_NAME: Record<Lang, string> = { ko: "한국어", en: "English" };

function themeLabel(choice: "system" | "light" | "dark"): string {
  if (choice === "light") return tr("Light theme", "라이트 테마");
  if (choice === "dark") return tr("Dark theme", "다크 테마");
  return tr("System theme", "시스템 테마");
}

function ThemeToggle() {
  const { choice, cycle } = useTheme();
  const Icon = choice === "dark" ? Moon : choice === "light" ? Sun : Monitor;
  const label = themeLabel(choice);
  return (
    <button
      type="button"
      className="btn btn--ghost btn--icon"
      onClick={cycle}
      title={tr(`${label} (click to change)`, `${label} (클릭해서 변경)`)}
      aria-label={tr(`${label}. Change theme`, `${label}. 테마 변경`)}
    >
      <Icon size={18} aria-hidden />
    </button>
  );
}

/** Header menu switching the UI between Korean and English. */
function LanguageMenu() {
  const [lang, setLang] = useLang();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const listId = useId();

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    rootRef.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="lang-menu" ref={rootRef}>
      <button
        type="button"
        className="lang-menu__button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={listId}
        title={tr("Language", "언어")}
        onClick={() => setOpen((v) => !v)}
      >
        <Languages size={16} aria-hidden />
        <span className="lang-menu__label">{LANG_NAME[lang]}</span>
        <span className="sr-only">{tr("Change language", "언어 변경")}</span>
        <ChevronDown size={14} aria-hidden />
      </button>
      {open && (
        <ul className="lang-menu__list" role="menu" id={listId} aria-label={tr("Language", "언어")}>
          {LANGS.map((code) => (
            <li key={code} role="none">
              <button
                type="button"
                role="menuitemradio"
                aria-checked={code === lang}
                lang={code}
                className="lang-menu__item"
                onClick={() => {
                  setOpen(false);
                  setLang(code);
                }}
              >
                {LANG_NAME[code]}
                {code === lang && <Check size={15} aria-hidden />}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NotificationBell() {
  const { unread } = useApp();
  const label = unread > 0 ? tr(`Notifications, ${unread} unread`, `알림, 읽지 않음 ${unread}개`) : tr("Notifications", "알림");
  return (
    <Link to="/notifications" className="btn btn--ghost btn--icon bell" aria-label={label} title={label}>
      <Bell size={18} aria-hidden />
      {unread > 0 && (
        <span className="bell__count" aria-hidden>
          {unread > 99 ? "99+" : unread}
        </span>
      )}
    </Link>
  );
}

/** Current page name for the top-bar breadcrumb. */
function pageName(pathname: string): string {
  if (pathname === "/") return tr("Trips", "내 여행");
  if (pathname === "/trips/new") return tr("New trip", "새 여행");
  if (/^\/trips\/\d+\/edit$/.test(pathname)) return tr("Edit trip", "여행 수정");
  if (/^\/trips\/\d+/.test(pathname)) return tr("Trip details", "여행 상세");
  if (pathname.startsWith("/itineraries/")) return tr("Itinerary", "여행 일정");
  if (pathname.startsWith("/notifications")) return tr("Notifications", "알림");
  if (pathname.startsWith("/settings")) return tr("Settings", "설정");
  return tr("Page not found", "페이지 없음");
}

function Sidebar() {
  const { meta, unread } = useApp();
  const { pathname } = useLocation();
  // "Trips" stays highlighted on a trip's pages and its itineraries (not on "New trip").
  const tripsActive =
    pathname === "/" || (/^\/trips\/\d+/.test(pathname)) || pathname.startsWith("/itineraries/");
  return (
    <aside className="sidebar">
      <Link to="/" className="brand" aria-label={tr("AiSquared Travel home", "AiSquared Travel 홈")}>
        <span className="brand__mark" aria-hidden>
          <Send size={24} strokeWidth={2.2} />
        </span>
        <span className="brand__text">
          aisquared<span className="brand__dot">.</span>
        </span>
      </Link>
      <div className="sidebar__label">{tr("My travel space", "나의 여행 공간")}</div>
      <nav className="side-nav" aria-label={tr("Main", "주 메뉴")}>
        <Link to="/" title={tr("Trips", "내 여행")} aria-current={tripsActive ? "page" : undefined}>
          <Plane size={19} aria-hidden />
          <span className="side-nav__label">{tr("Trips", "내 여행")}</span>
        </Link>
        <NavLink to="/trips/new" title={tr("New trip", "새 여행")}>
          <PlusCircle size={19} aria-hidden />
          <span className="side-nav__label">{tr("New trip", "새 여행")}</span>
        </NavLink>
        <NavLink to="/notifications" title={tr("Notifications", "알림")}>
          <Bell size={19} aria-hidden />
          <span className="side-nav__label">{tr("Notifications", "알림")}</span>
          {unread > 0 && <span className="side-nav__count">{unread > 99 ? "99+" : unread}</span>}
        </NavLink>
        <NavLink to="/settings" title={tr("Settings", "설정")}>
          <Settings size={19} aria-hidden />
          <span className="side-nav__label">{tr("Settings", "설정")}</span>
        </NavLink>
      </nav>
      <section className="side-promo" aria-label={tr("Tip", "도움말")}>
        <Compass size={26} className="side-promo__icon" aria-hidden />
        <h2>{tr("Travel lighter, plan smarter.", "설레는 여행, 가볍게 준비하세요.")}</h2>
        <p>
          {tr(
            "We keep an eye on fares for you. Just pick where you want to go.",
            "항공권 가격은 저희가 살필게요. 어떤 여행을 떠날지 골라보세요.",
          )}
        </p>
        <Link to="/trips/new">
          {tr("Plan a new trip", "새 여행 준비하기")} <ArrowUpRight size={14} aria-hidden />
        </Link>
      </section>
      <div className="sidebar__bottom">
        <div className={`side-status${meta ? "" : " side-status--off"}`} title={meta ? undefined : tr("Backend not reachable", "서버에 연결할 수 없어요")}>
          <i aria-hidden />
          <span>{meta ? tr("Price monitoring on this PC", "이 PC에서 가격 모니터링 중") : tr("Server offline", "서버 연결 안 됨")}</span>
        </div>
        <div className="side-foot">
          AiSquared Travel{meta ? ` v${meta.version}` : ""}
          <br />
          {tr("Runs locally on this PC", "이 PC에서 로컬로 실행 중")}
        </div>
      </div>
    </aside>
  );
}

/** Layout wrapper rendered around every route. */
export function Layout() {
  const location = useLocation();
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        {tr("Skip to content", "본문으로 건너뛰기")}
      </a>
      <Sidebar />
      <div className="main-shell">
        <header className="topbar">
          <nav className="crumbs" aria-label={tr("Breadcrumb", "현재 위치")}>
            <span className="crumbs__root">{tr("My travel space", "나의 여행 공간")}</span>
            <ChevronRight size={14} className="crumbs__sep" aria-hidden />
            <span className="crumbs__current" aria-current="page">
              {pageName(location.pathname)}
            </span>
          </nav>
          <div className="top-actions">
            <span className="tz-chip" title="Asia/Seoul">
              <Globe size={14} aria-hidden /> {tr("Seoul · KST", "서울 · 한국 시간")}
            </span>
            <LanguageMenu />
            <NotificationBell />
            <ThemeToggle />
          </div>
        </header>
        <main id="main" className="app-main" tabIndex={-1}>
          <Suspense
            key={location.pathname}
            fallback={
              <div className="card">
                <div className="card__body">
                  <SkeletonBlock lines={6} label={tr("Loading page", "페이지 불러오는 중")} />
                </div>
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </main>
        <footer className="app-footer">
          <span>
            <Send size={13} aria-hidden /> {tr("Plan lightly, travel happily.", "계획은 가볍게, 여행은 즐겁게.")}
          </span>
          <span>
            {tr(
              "Times in Seoul time (Asia/Seoul); flight times are airport-local.",
              "시간은 한국 시간(Asia/Seoul) 기준, 항공편 시각은 현지 공항 시각입니다.",
            )}
          </span>
        </footer>
      </div>
    </div>
  );
}
