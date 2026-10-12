"""Screenshot the SPA (served by `vite preview` + dev-mock/server.mjs) at desktop and phone widths.

Usage (from frontend/):
    ..\\.venv\\Scripts\\python.exe dev-mock\\screenshot.py [base_url] [--dark] [--only name,...]

Writes PNGs to dev-mock/screenshots/. Dev-only helper; not part of the build.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "screenshots"
PAGES = {
    "dashboard": "/",
    "trip-new": "/trips/new",
    "trip-edit": "/trips/1/edit",
    "trip-flights": "/trips/1",
    "trip-history": "/trips/1?tab=history",
    "trip-alerts": "/trips/1?tab=alerts",
    "trip-plan": "/trips/1?tab=plan",
    "trip-itineraries": "/trips/1?tab=itineraries",
    "itinerary": "/itineraries/31",
    "notifications": "/notifications",
    "settings": "/settings",
}
VIEWPORTS = {"desktop": (1366, 900), "phone": (390, 844)}


def main(argv: list[str]) -> int:
    """Capture every page at every viewport."""
    base = next((a for a in argv if a.startswith("http")), "http://127.0.0.1:5175")
    dark = "--dark" in argv
    only: set[str] = set()
    if "--only" in argv:
        only = set(argv[argv.index("--only") + 1].split(","))
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for vp_name, (w, h) in VIEWPORTS.items():
            ctx = browser.new_context(
                viewport={"width": w, "height": h},
                color_scheme="dark" if dark else "light",
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                device_scale_factor=1,
            )
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            for name, path in PAGES.items():
                if only and name not in only:
                    continue
                page.goto(base + path, wait_until="networkidle")
                page.wait_for_timeout(700)
                suffix = "-dark" if dark else ""
                file = OUT / f"{name}-{vp_name}{suffix}.png"
                page.screenshot(path=str(file), full_page=True)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth"
                )
                print(f"{file.name}{'  [HORIZONTAL OVERFLOW]' if overflow else ''}")
            for e in errors:
                print("  console:", e[:300])
            ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
