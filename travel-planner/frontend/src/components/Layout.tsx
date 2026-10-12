/** App shell: a sidebar with the "new trip" button, main navigation and the watched trips
 * (collapsible to an icon rail on desktop, a slide-in drawer on phones), the page outlet and
 * a quiet footer. Language and theme controls live at the bottom of the sidebar. */

import { Suspense, useEffect, useId, useRef, useState } from "react";
import { NavLink, Outlet, Link, useLocation } from "react-router-dom";
import { Bell, Check, ChevronUp, Languages, Menu, Monitor, Moon, PanelLeft, Plane, Plus, Settings, Sun, X } from "lucide-react";
import { api } from "../api/endpoints";
import type { TripOut } from "../api/types";
import { useApp } from "../lib/appContext";
import { useAsync } from "../lib/hooks";
import { LANGS, tr, useLang } from "../lib/i18n";
import type { Lang } from "../lib/i18n";
import { useTheme } from "../lib/theme";
import { BrandMark } from "./BrandMark";
import { SkeletonBlock } from "./ui";

const LANG_NAME: Record<Lang, string> = { ko: "한국어", en: "English" };
const NAV_KEY = "ast-nav";

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

/** Sidebar menu switching the UI between Korean and English (opens upwards). */
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
        <ChevronUp size={14} className="lang-menu__chev" aria-hidden />
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

/** Current page name (phone top bar). */
function pageName(pathname: string): string {
  if (pathname === "/") return tr("Trips", "내 여행");
  if (pathname === "/trips/new") return tr("New trip", "새 여행");
  if (/^\/trips\/\d+\/edit$/.test(pathname)) return tr("Edit trip", "여행 수정");
  if (/^\/trips\/\d+\/book\//.test(pathname)) return tr("Booking", "예약 준비");
  if (/^\/trips\/\d+/.test(pathname)) return tr("Trip details", "여행 상세");
  if (pathname.startsWith("/itineraries/")) return tr("Itinerary", "여행 일정");
  if (pathname.startsWith("/notifications")) return tr("Notifications", "알림");
  if (pathname.startsWith("/settings")) return tr("Settings", "설정");
  return tr("Page not found", "페이지 없음");
}

type TripTone = "good" | "bad" | "off" | "watch";

function tripTone(trip: TripOut): TripTone {
  if (!trip.active) return "off";
  if (trip.last_status === "error") return "bad";
  const value = trip.latest_min_price_basis_value;
  if (value != null && trip.target_price != null && value <= trip.target_price) return "good";
  return "watch";
}

function toneLabel(tone: TripTone): string {
  if (tone === "good") return tr("At or below target", "목표가 도달");
  if (tone === "bad") return tr("Last check failed", "마지막 확인 실패");
  if (tone === "off") return tr("Paused", "일시 중지");
  return tr("Watching", "지켜보는 중");
}

/** The watched trips, like a list of recent conversations. */
function RecentTrips({ pathname }: { pathname: string }) {
  const { data } = useAsync(() => api.listTrips(), [pathname]);
  if (!data || data.length === 0) return null;
  return (
    <section className="recents" aria-labelledby="recents-title">
      <h2 className="recents__title" id="recents-title">
        {tr("Watched trips", "지켜보는 여행")}
      </h2>
      <ul className="recents__list">
        {data.map((trip) => {
          const tone = tripTone(trip);
          const active = pathname === `/trips/${trip.id}` || pathname.startsWith(`/trips/${trip.id}/`);
          return (
            <li key={trip.id}>
              <Link to={`/trips/${trip.id}`} className="recents__item" aria-current={active ? "page" : undefined} title={trip.name}>
                <i className={`recents__dot recents__dot--${tone}`} aria-hidden />
                <span className="recents__name">{trip.name}</span>
                <span className="recents__route">
                  {trip.origin}–{trip.destination}
                </span>
                <span className="sr-only">, {toneLabel(tone)}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(NAV_KEY) === "rail";
  } catch {
    return false;
  }
}

function Sidebar({ collapsed, onToggle, onNavigate }: { collapsed: boolean; onToggle: () => void; onNavigate: () => void }) {
  const { meta, unread } = useApp();
  const { pathname } = useLocation();
  // "Trips" stays highlighted on a trip's pages and its itineraries (not on "New trip").
  const tripsActive = pathname === "/" || /^\/trips\/\d+/.test(pathname) || pathname.startsWith("/itineraries/");
  return (
    <aside className="sidebar" id="app-sidebar" aria-label={tr("Sidebar", "사이드바")}>
      <div className="sidebar__top">
        <Link to="/" className="brand" aria-label={tr("AiSquared Travel home", "AiSquared Travel 홈")} onClick={onNavigate}>
          <BrandMark size={26} className="brand__mark" />
          <span className="brand__text">
            AiSquared <em>Travel</em>
          </span>
        </Link>
        <button
          type="button"
          className="btn btn--ghost btn--icon sidebar__toggle"
          onClick={onToggle}
          aria-controls="app-sidebar"
          aria-expanded={!collapsed}
          title={collapsed ? tr("Expand sidebar", "사이드바 펼치기") : tr("Collapse sidebar", "사이드바 접기")}
          aria-label={collapsed ? tr("Expand sidebar", "사이드바 펼치기") : tr("Collapse sidebar", "사이드바 접기")}
        >
          <PanelLeft size={18} aria-hidden />
        </button>
      </div>

      <Link to="/trips/new" className="new-trip" title={tr("New trip", "새 여행")} onClick={onNavigate}>
        <span className="new-trip__icon" aria-hidden>
          <Plus size={16} strokeWidth={2.4} />
        </span>
        <span className="new-trip__label">{tr("New trip", "새 여행")}</span>
      </Link>

      <nav className="side-nav" aria-label={tr("Main", "주 메뉴")} onClick={onNavigate}>
        <Link to="/" title={tr("Trips", "내 여행")} aria-current={tripsActive ? "page" : undefined}>
          <Plane size={18} aria-hidden />
          <span className="side-nav__label">{tr("Trips", "내 여행")}</span>
        </Link>
        <NavLink to="/notifications" title={tr("Notifications", "알림")}>
          <Bell size={18} aria-hidden />
          <span className="side-nav__label">{tr("Notifications", "알림")}</span>
          {unread > 0 && (
            <span className="side-nav__count" aria-label={tr(`${unread} unread`, `읽지 않음 ${unread}개`)}>
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </NavLink>
        <NavLink to="/settings" title={tr("Settings", "설정")}>
          <Settings size={18} aria-hidden />
          <span className="side-nav__label">{tr("Settings", "설정")}</span>
        </NavLink>
      </nav>

      <div className="sidebar__scroll" onClick={onNavigate}>
        <RecentTrips pathname={pathname} />
      </div>

      <div className="sidebar__bottom">
        <div
          className={`side-status${meta ? "" : " side-status--off"}`}
          title={meta ? tr("Price checks run while this PC is on", "이 PC가 켜져 있는 동안 가격을 확인해요") : tr("Backend not reachable", "서버에 연결할 수 없어요")}
        >
          <i aria-hidden />
          <span>
            {meta ? tr("Watching fares on this PC", "이 PC에서 가격을 지켜보는 중") : tr("Server offline", "서버 연결 안 됨")}
            {meta && <small> · v{meta.version}</small>}
          </span>
        </div>
        <div className="side-controls">
          <LanguageMenu />
          <ThemeToggle />
        </div>
      </div>
    </aside>
  );
}

/** Layout wrapper rendered around every route. */
export function Layout() {
  const location = useLocation();
  const { unread } = useApp();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawer, setDrawer] = useState(false);

  useEffect(() => setDrawer(false), [location.pathname]);
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawer(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawer]);

  const toggleCollapsed = () => {
    if (drawer) {
      setDrawer(false);
      return;
    }
    setCollapsed((v) => {
      try {
        localStorage.setItem(NAV_KEY, v ? "full" : "rail");
      } catch {
        /* storage unavailable: lasts for this session */
      }
      return !v;
    });
  };

  const bellLabel = unread > 0 ? tr(`Notifications, ${unread} unread`, `알림, 읽지 않음 ${unread}개`) : tr("Notifications", "알림");
  return (
    <div className={`app-shell${collapsed ? " is-rail" : ""}${drawer ? " is-drawer" : ""}`}>
      <a className="skip-link" href="#main">
        {tr("Skip to content", "본문으로 건너뛰기")}
      </a>
      <Sidebar collapsed={collapsed} onToggle={toggleCollapsed} onNavigate={() => setDrawer(false)} />
      {drawer && <div className="drawer-scrim" onClick={() => setDrawer(false)} aria-hidden />}
      <div className="main-shell">
        <header className="mobilebar">
          <button
            type="button"
            className="btn btn--ghost btn--icon"
            onClick={() => setDrawer((v) => !v)}
            aria-controls="app-sidebar"
            aria-expanded={drawer}
            aria-label={drawer ? tr("Close menu", "메뉴 닫기") : tr("Open menu", "메뉴 열기")}
          >
            {drawer ? <X size={20} aria-hidden /> : <Menu size={20} aria-hidden />}
          </button>
          <Link to="/" className="mobilebar__brand" aria-label={tr("AiSquared Travel home", "AiSquared Travel 홈")}>
            <BrandMark size={22} />
          </Link>
          <span className="mobilebar__title">{pageName(location.pathname)}</span>
          <Link to="/notifications" className="btn btn--ghost btn--icon bell" aria-label={bellLabel} title={bellLabel}>
            <Bell size={18} aria-hidden />
            {unread > 0 && (
              <span className="bell__count" aria-hidden>
                {unread > 99 ? "99+" : unread}
              </span>
            )}
          </Link>
          <Link to="/trips/new" className="btn btn--primary btn--icon btn--sm" aria-label={tr("New trip", "새 여행")} title={tr("New trip", "새 여행")}>
            <Plus size={16} aria-hidden />
          </Link>
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
            <BrandMark size={14} /> {tr("Plan lightly, travel happily.", "계획은 가볍게, 여행은 즐겁게.")}
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
