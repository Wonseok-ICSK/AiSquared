"""Browser regression checks for reviewed UI bugs, against the preview + mock API (dev-only).

Usage (from frontend/, mock + preview running, fresh mock state):
    node dev-mock/server.mjs 8817
    API_TARGET=http://127.0.0.1:8817 npx vite preview --port 5193 --strictPort
    ..\\.venv\\Scripts\\python.exe dev-mock\\regressions.py http://localhost:5193

Each check prints PASS/FAIL; the exit code is the number of failures. Screenshots of the
fixed states go to dev-mock/screenshots/fix-*.png.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, Route, sync_playwright

OUT = Path(__file__).resolve().parent / "screenshots"
DESKTOP = {"width": 1366, "height": 900}
PHONE = {"width": 390, "height": 844}


def new_page(browser: Browser, viewport: dict[str, int] = DESKTOP) -> Page:
    """A fresh page in Seoul time with console errors collected on ``page.errors``."""
    ctx = browser.new_context(viewport=viewport, locale="ko-KR", timezone_id="Asia/Seoul")
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.errors = errors  # type: ignore[attr-defined]
    return page


def shot(page: Page, name: str, full: bool = False) -> None:
    """Save a screenshot of the current state."""
    page.screenshot(path=str(OUT / f"fix-{name}.png"), full_page=full)


def capture_bodies(page: Page, method: str, path: str) -> list[Any]:
    """Collect JSON bodies of requests ``method path`` made by the page."""
    bodies: list[Any] = []

    def on_request(req: Any) -> None:
        if req.method == method and req.url.split("?")[0].endswith(path):
            bodies.append(json.loads(req.post_data or "null"))

    page.on("request", on_request)
    return bodies


# --------------------------------------------------------------------------- checks


def check_airport_blur(browser: Browser, base: str) -> None:
    """Typed city text is committed on Tab (or reverted), never silently dropped; clearing a
    custom trip name sends name=""."""
    page = new_page(browser)
    # start from the fixture state (custom name, ICN) so the check can be re-run
    page.request.put(base + "/api/trips/1", data={"origin": "ICN", "name": "Tokyo food trip"})
    puts = capture_bodies(page, "PUT", "/api/trips/1")
    page.goto(base + "/trips/1/edit", wait_until="networkidle")
    origin = page.get_by_role("combobox", name="From")
    # 1) suggestions loaded, then Tab -> highlighted option (OSA)
    origin.fill("Osaka")
    page.wait_for_selector(f'[id="{origin.get_attribute("id")}-list"] li[role=option]')
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)
    assert origin.input_value() == "OSA", origin.input_value()
    # 2) typed faster than suggestions load -> looked up on blur
    origin.fill("Seoul")
    page.keyboard.press("Tab")
    page.wait_for_timeout(900)
    assert origin.input_value() == "SEL", origin.input_value()
    # 3) nothing matches -> reverted to the stored code with a note
    origin.fill("Xyzzy")
    page.keyboard.press("Tab")
    page.wait_for_timeout(900)
    assert origin.input_value() == "SEL", origin.input_value()
    note = page.locator(".combo__selected--warn").first.inner_text()
    assert "No airport matches" in note, note
    shot(page, "airport-revert")
    origin.fill("Osaka")
    page.wait_for_selector(f'[id="{origin.get_attribute("id")}-list"] li[role=option]')
    page.keyboard.press("Tab")
    page.wait_for_timeout(200)
    # clear the custom name too, then save
    page.get_by_label("Trip name", exact=True).fill("")
    page.get_by_role("button", name="Save changes").click()
    page.wait_for_url("**/trips/1")
    page.wait_for_load_state("networkidle")
    assert puts, "no PUT sent"
    assert puts[-1].get("origin") == "OSA", puts[-1]
    assert puts[-1].get("name") == "", puts[-1]
    heading = page.locator("h1").first.inner_text()
    assert heading == "OSA → TYO", heading
    assert not page.errors, page.errors  # type: ignore[attr-defined]


def check_email_draft(browser: Browser, base: str) -> None:
    """Unsaved SMTP edits survive another section's save and the visibility reload."""
    page = new_page(browser)
    page.goto(base + "/settings", wait_until="networkidle")
    email = page.locator("li.channel", has_text="E-mail (SMTP)")
    email.get_by_label("SMTP server").fill("smtp.example.org")
    email.get_by_label("Username").fill("someone@example.org")
    page.locator("#ai").get_by_role("button", name="Save").click()
    page.wait_for_timeout(700)
    assert email.get_by_label("SMTP server").input_value() == "smtp.example.org"
    assert email.get_by_label("Username").input_value() == "someone@example.org"
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(700)
    assert email.get_by_label("SMTP server").input_value() == "smtp.example.org"
    assert email.get_by_label("Username").input_value() == "someone@example.org"


def _stale_trip_routes(page: Page) -> None:
    """Trip 1's latest run found nothing: field null, newest history point null."""

    def trips(route: Route) -> None:
        resp = route.fetch()
        data = resp.json()
        for t in data if isinstance(data, list) else [data]:
            if t["id"] == 1:
                t["latest_min_price"] = None
                t["latest_min_price_basis_value"] = None
        route.fulfill(response=resp, json=data)

    def history(route: Route) -> None:
        resp = route.fetch()
        data = resp.json()
        last = data["points"][-1]
        data["points"].append({**last, "run_id": last["run_id"] + 1, "min_total": None, "min_per_person": None, "by_provider": {}})
        route.fulfill(response=resp, json=data)

    page.route("**/api/trips", trips)
    page.route("**/api/trips/1", trips)
    page.route("**/api/trips/1/price-history", history)


def check_dashboard_stale(browser: Browser, base: str) -> None:
    """No old price as "Lowest now", no delta / below-target badge for it."""
    for name, viewport in (("desktop", DESKTOP), ("phone", PHONE)):
        page = new_page(browser, viewport)
        _stale_trip_routes(page)
        page.goto(base + "/", wait_until="networkidle")
        card = page.locator("article.trip-card", has=page.locator("h3 a[href='/trips/1']"))
        text = card.locator(".trip-card__price").inner_text()
        assert "—" in text, text
        assert "No matching offers" in text, text
        assert "Last found" in text and "278,000" in text, text
        assert "Below target" not in text, text
        assert card.locator(".delta").count() == 0
        card.scroll_into_view_if_needed()
        card.screenshot(path=str(OUT / f"fix-dashboard-stale-{name}.png"))
        page.goto(base + "/trips/1", wait_until="networkidle")
        detail = page.locator(".stat-strip .stat").first.inner_text()
        assert "—" in detail, detail


def check_chart_hover(browser: Browser, base: str) -> None:
    """Hovering exactly on a point's x selects that point."""
    page = new_page(browser)
    page.goto(base + "/trips/1?tab=history", wait_until="networkidle")
    svg = page.locator(".chart__plot svg")
    box = svg.bounding_box()
    assert box
    cross = page.locator(".chart__plot svg g[pointer-events=none] line")
    seen: list[float] = []
    for dx in range(70, int(box["width"]) - 10, 2):
        page.mouse.move(box["x"] + dx, box["y"] + box["height"] / 2)
        if cross.count():
            x1 = float(cross.first.get_attribute("x1") or "nan")
            if not seen or abs(seen[-1] - x1) > 0.01:
                seen.append(x1)
    assert len(seen) >= 3, seen
    for x in seen:
        page.mouse.move(box["x"] + x, box["y"] + box["height"] / 2)
        got = float(cross.first.get_attribute("x1") or "nan")
        assert abs(got - x) < 0.01, (x, got)
    # midpoint between the first two switches to the second just after the middle
    a, b = seen[0], seen[1]
    page.mouse.move(box["x"] + (a + b) / 2 - 1.5, box["y"] + box["height"] / 2)
    assert abs(float(cross.first.get_attribute("x1") or "nan") - a) < 0.01


def check_last_default(browser: Browser, base: str) -> None:
    """The last default source cannot be switched off (never sends default_providers: [])."""
    page = new_page(browser)
    puts = capture_bodies(page, "PUT", "/api/settings")
    page.goto(base + "/settings", wait_until="networkidle")
    switches = page.locator(".provider-card input[role=switch]")
    for _ in range(4):
        on = [i for i in range(switches.count()) if switches.nth(i).is_checked() and switches.nth(i).is_enabled()]
        if not on:
            break
        switches.nth(on[0]).click()
        page.wait_for_timeout(700)
    checked = [i for i in range(switches.count()) if switches.nth(i).is_checked()]
    assert len(checked) == 1, checked
    assert switches.nth(checked[0]).is_disabled()
    assert all(b.get("default_providers") != [] for b in puts), puts
    hint = page.locator(".provider-card__default .xsmall").first.inner_text()
    assert "At least one default source" in hint, hint
    page.locator(".provider-card").nth(checked[0]).scroll_into_view_if_needed()
    shot(page, "providers-last-default")


def check_provider_test_unknown(browser: Browser, base: str) -> None:
    """Without GET /providers/{key}/test the UI says the result is unknown."""
    page = new_page(browser)
    page.route("**/api/providers/*/test", lambda r: r.fulfill(status=404, json={"detail": "Not Found"}) if r.request.method == "GET" else r.continue_())
    page.goto(base + "/settings", wait_until="networkidle")
    card = page.locator(".provider-card").first
    card.get_by_role("button", name="Test search").click()
    page.wait_for_timeout(2800)
    text = card.inner_text()
    assert "result unknown" in text, text
    assert "backend log" not in text, text


def check_alert_basis(browser: Browser, base: str) -> None:
    """Blank threshold locks the basis to the trip's; descriptions use the effective basis."""
    page = new_page(browser)
    posts = capture_bodies(page, "POST", "/api/trips/1/alerts")
    page.goto(base + "/trips/1?tab=alerts", wait_until="networkidle")
    form = page.get_by_role("form", name="New alert rule")
    basis = form.get_by_role("radiogroup", name="Price basis")
    assert basis.get_attribute("aria-disabled") == "true"
    assert basis.get_by_role("radio", name="Per person").get_attribute("aria-checked") == "true"
    shot(page, "alerts-basis-locked", full=True)
    form.get_by_label("Threshold").fill("500000")
    assert basis.get_attribute("aria-disabled") is None
    basis.get_by_role("radio", name="Total").click()
    form.get_by_label("Threshold").fill("")
    assert basis.get_by_role("radio", name="Per person").get_attribute("aria-checked") == "true"
    form.get_by_role("button", name="Add rule").click()
    page.wait_for_timeout(600)
    assert posts and posts[-1]["price_basis"] == "per_person", posts


def check_running_run_pickup(browser: Browser, base: str) -> None:
    """A scheduled run already running when the tab loads is polled until it finishes."""
    page = new_page(browser)
    state = {"polls": 0}
    running = {
        "id": 777, "trip_id": 1, "trigger": "scheduled", "started_at": "2026-10-06T08:30:00Z",
        "finished_at": None, "status": "running", "error": None, "combinations_total": 6,
        "combinations_searched": 2, "offers_found": 14, "offers_matching": 0, "min_price_total": None,
        "min_price_per_person": None, "currency": "KRW", "best_offer_id": None, "provider_stats": {}, "log": [],
    }

    def runs(route: Route) -> None:
        resp = route.fetch()
        data = resp.json()
        if "limit=1" in route.request.url and "limit=10" not in route.request.url:
            pass  # TripDetail's discovery sees nothing: only the history tab's list has it
        elif state["polls"] < 2:
            data = [running, *data]
        else:
            data = [{**running, "status": "ok", "finished_at": "2026-10-06T08:31:00Z"}, *data]
        route.fulfill(response=resp, json=data)

    def run_detail(route: Route) -> None:
        state["polls"] += 1
        done = state["polls"] >= 2
        route.fulfill(json={**running, "status": "ok" if done else "running", "finished_at": "2026-10-06T08:31:00Z" if done else None,
                            "combinations_searched": 6 if done else 4, "offers_matching": 9 if done else 0,
                            "min_price_per_person": 275000 if done else None, "min_price_total": 550000 if done else None})

    page.route("**/api/trips/1/runs*", runs)
    page.route("**/api/runs/777", run_detail)
    page.goto(base + "/trips/1?tab=history", wait_until="networkidle")
    page.wait_for_selector("[aria-label='Run in progress']", state="attached")
    page.wait_for_timeout(7000)
    assert state["polls"] >= 2, state
    assert page.locator("[aria-label='Run in progress']").count() == 0
    assert page.locator("text=Check finished").count() == 1


def check_itinerary_credits(browser: Browser, base: str) -> None:
    """Claude plans show the planner's OSM/Nominatim credits and per-person cost labels."""
    for name, viewport in (("desktop", DESKTOP), ("phone", PHONE)):
        page = new_page(browser, viewport)
        page.goto(base + "/itineraries/31", wait_until="networkidle")
        aside = page.locator(".itin-side").inner_text()
        assert "OpenStreetMap" in aside and "Nominatim" in aside and "Open-Meteo" in aside, aside
        body = page.locator("main").inner_text()
        assert "/ person" in body and "per person" in body, "cost basis labels missing"
        page.locator(".day").first.screenshot(path=str(OUT / f"fix-itinerary-day-{name}.png"))
        page.locator(".itin-side > p").last.scroll_into_view_if_needed()
        shot(page, f"itinerary-credits-{name}")


CHECKS: list[Callable[[Browser, str], None]] = [
    check_airport_blur,
    check_email_draft,
    check_dashboard_stale,
    check_chart_hover,
    check_last_default,
    check_provider_test_unknown,
    check_alert_basis,
    check_running_run_pickup,
    check_itinerary_credits,
]


def main(base: str) -> int:
    """Run every check; returns the number of failures."""
    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for check in CHECKS:
            try:
                check(browser, base)
                print(f"PASS {check.__name__}")
            except Exception:  # noqa: BLE001 - report and continue
                failures += 1
                print(f"FAIL {check.__name__}\n{traceback.format_exc()}")
        browser.close()
    print(f"{len(CHECKS) - failures} passed, {failures} failed")
    return failures


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5193"))
