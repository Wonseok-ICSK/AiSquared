"""Drive interactive UI states against the mock API and screenshot them (dev-only helper).

Usage (from frontend/): ..\\.venv\\Scripts\\python.exe dev-mock\\interact.py [base_url]
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parent / "screenshots"


def shot(page: Page, name: str, full: bool = False) -> None:
    """Save a viewport (or full-page) screenshot."""
    page.screenshot(path=str(OUT / f"ix-{name}.png"), full_page=full)
    print(f"ix-{name}.png")


def run(base: str) -> None:
    """Exercise the main interactions."""
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1366, "height": 900}, locale="ko-KR", timezone_id="Asia/Seoul")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # chart hover tooltip
        page.goto(base + "/trips/1?tab=history", wait_until="networkidle")
        box = page.locator(".chart__plot svg").bounding_box()
        assert box
        page.mouse.move(box["x"] + box["width"] * 0.62, box["y"] + box["height"] * 0.5)
        page.wait_for_timeout(300)
        page.locator(".chart").screenshot(path=str(OUT / "ix-chart-hover.png"))
        print("ix-chart-hover.png")
        # keyboard navigation + legend toggle
        page.locator(".chart__legend button", has_text="Naver Flights").click()
        page.locator(".chart__plot svg").focus()
        page.keyboard.press("End")
        page.wait_for_timeout(200)
        page.locator(".chart").screenshot(path=str(OUT / "ix-chart-keyboard.png"))
        print("ix-chart-keyboard.png")

        # offer history popover
        page.goto(base + "/trips/1", wait_until="networkidle")
        page.locator("button[aria-label='Price history of this flight']").first.click()
        page.wait_for_selector(".popover table")
        page.wait_for_timeout(200)
        shot(page, "offer-popover")

        # non-matching toggle
        page.get_by_label("Show non-matching").check()
        page.wait_for_timeout(600)
        shot(page, "offers-all", full=True)

        # check now -> progress -> finished
        page.goto(base + "/trips/1", wait_until="networkidle")
        page.get_by_role("button", name="Check now").click()
        page.wait_for_timeout(2500)
        shot(page, "check-running")
        page.wait_for_selector("text=Check finished", timeout=20000)
        shot(page, "check-finished")

        # trip form: autocomplete + validation
        page.goto(base + "/trips/new", wait_until="networkidle")
        page.locator("input[role='combobox']").first.fill("tok")
        page.wait_for_selector(".combo__list [role='option']")
        page.wait_for_timeout(200)
        shot(page, "autocomplete")
        page.keyboard.press("Enter")
        page.get_by_role("button", name="Create trip").click()
        page.wait_for_timeout(400)
        shot(page, "form-validation")
        # one-way hides return fields; Naver requires acknowledgement
        page.get_by_role("radio", name="One way").click()
        page.locator(".provider-pick", has_text="Naver Flights").click()
        page.wait_for_timeout(200)
        page.locator(".provider-picks").scroll_into_view_if_needed()
        shot(page, "form-oneway-naver")

        # itinerary generation progress
        page.goto(base + "/trips/1?tab=itineraries", wait_until="networkidle")
        page.get_by_role("button", name="Generate itinerary").click()
        page.wait_for_timeout(3000)
        shot(page, "itinerary-generating")

        # settings: provider test + telegram detect + notification test
        page.goto(base + "/settings", wait_until="networkidle")
        page.get_by_role("button", name="Test search").first.click()
        page.wait_for_selector("text=Test search OK", timeout=15000)
        page.locator("#providers").screenshot(path=str(OUT / "ix-provider-test.png"))
        print("ix-provider-test.png")
        page.get_by_role("button", name="Detect chat ID").click()
        page.wait_for_selector("text=Found chat")
        tg = page.locator(".channel", has_text="Telegram")
        tg.get_by_role("button", name="Send test").click()
        page.wait_for_timeout(500)
        tg.screenshot(path=str(OUT / "ix-telegram.png"))
        print("ix-telegram.png")
        em = page.locator(".channel", has_text="E-mail (SMTP)")
        em.get_by_role("button", name="Send test").click()
        page.wait_for_timeout(500)
        em.screenshot(path=str(OUT / "ix-email.png"))
        print("ix-email.png")
        page.locator(".channel", has_text="KakaoTalk").screenshot(path=str(OUT / "ix-kakao.png"))
        print("ix-kakao.png")

        # notifications: mark all read updates the bell
        page.goto(base + "/notifications", wait_until="networkidle")
        mark_all = page.get_by_role("button", name="Mark all read")
        if mark_all.is_enabled():
            mark_all.click()
            page.wait_for_timeout(800)
        shot(page, "notifications-read")

        for e in errors:
            print("console:", e[:300])
        browser.close()


if __name__ == "__main__":
    run(next((a for a in sys.argv[1:] if a.startswith("http")), "http://localhost:5175"))
