"""Third-party integrations: KakaoTalk OAuth (DESIGN §7, §13.3).

The user opens ``GET /integrations/kakao/authorize`` in a browser tab (302 to Kakao's consent
page, with a single-use ``state`` issued by :mod:`app.services.notifications.kakao`); Kakao
redirects back to ``GET /integrations/kakao/callback?code=&state=``, which exchanges the code
for tokens and answers with a small human-readable HTML page (the tab is not the SPA).
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.notifications import kakao
from app.services.notifications.kakao import KakaoError
from app.settings_store import load_runtime_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["integrations"])

SETTINGS_PATH = "/settings"
_RETRY_HINT = "Start again from Settings → Notifications → KakaoTalk → Connect KakaoTalk."

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · AiSquared Travel</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f7f9; --card: #ffffff; --text: #1d2330;
          --muted: #5b6475; --accent: {accent}; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #14171d; --card: #1d222b; --text: #e8ebf1; --muted: #a2abbb; }}
  }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
          box-sizing: border-box; background: var(--bg); color: var(--text);
          font: 16px/1.5 system-ui, "Segoe UI", "Malgun Gothic", sans-serif; }}
  main {{ max-width: 520px; width: 100%; background: var(--card); border-radius: 12px;
          padding: 28px; box-shadow: 0 2px 12px rgba(0,0,0,.08);
          border-top: 4px solid var(--accent); }}
  h1 {{ margin: 0 0 12px; font-size: 1.3rem; }}
  p {{ margin: 0 0 12px; overflow-wrap: anywhere; }}
  .muted {{ color: var(--muted); font-size: .9rem; }}
  a {{ color: inherit; }}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
{paragraphs}
<p class="muted"><a href="{settings}">Back to Settings</a></p>
</main>
</body>
</html>
"""


def _page(status_code: int, title: str, *lines: str) -> HTMLResponse:
    """Small self-contained HTML page; every text is escaped."""
    paragraphs = "\n".join(f"<p>{html.escape(line)}</p>" for line in lines if line)
    accent = "#2e9d5b" if status_code < 400 else "#d14343"
    body = _PAGE.format(
        title=html.escape(title), paragraphs=paragraphs, accent=accent, settings=SETTINGS_PATH
    )
    return HTMLResponse(body, status_code=status_code)


@router.get("/integrations/kakao/authorize", response_class=Response)
def kakao_authorize(db: Session = Depends(get_db)) -> Response:
    """302 to Kakao's consent page (scope ``talk_message``); 400 page when not set up."""
    try:
        url = kakao.authorize_url(load_runtime_settings(db))
    except KakaoError as exc:
        return _page(
            400,
            "KakaoTalk is not set up",
            str(exc),
            "Save the REST API key (and the client secret, if enabled) and the redirect URI "
            "in Settings → Notifications → KakaoTalk first, then click Connect KakaoTalk again.",
        )
    return RedirectResponse(url, status_code=302)


@router.get("/integrations/kakao/callback", response_class=HTMLResponse)
def kakao_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Exchange the authorization ``code`` for tokens and show the outcome."""
    if error:
        reason = error_description or error
        if error == "access_denied":
            reason = "the consent was cancelled or denied on Kakao's page"
        return _page(400, "KakaoTalk was not connected", f"Kakao reported: {reason}.", _RETRY_HINT)
    if not state:
        return _page(
            400,
            "KakaoTalk was not connected",
            "The callback is missing the OAuth 'state' parameter (open this page only "
            "through the Connect KakaoTalk button).",
            _RETRY_HINT,
        )
    if not code:
        return _page(
            400, "KakaoTalk was not connected", "The callback has no authorization code.", _RETRY_HINT
        )
    try:
        connection = kakao.exchange_code(db, code, state)
    except KakaoError as exc:  # incl. KakaoAuthError (bad/expired state, refused code)
        db.rollback()
        logger.warning("KakaoTalk connection failed: %s", exc)
        return _page(400, "KakaoTalk was not connected", str(exc), _RETRY_HINT)
    except Exception:  # noqa: BLE001 - always answer with a readable page
        db.rollback()
        logger.exception("KakaoTalk callback failed")
        return _page(
            500,
            "KakaoTalk was not connected",
            "An unexpected error occurred while saving the connection; see data/logs/app.log.",
            _RETRY_HINT,
        )
    lines = ["KakaoTalk connected — you can close this tab."]
    if not connection.talk_message:
        lines.append(
            "Warning: the 'talk_message' (카카오톡 메시지 전송) permission was not granted, so "
            "messages cannot be sent yet. Enable that consent item in the Kakao Developers "
            "console, then connect again."
        )
    lines.append("Messages are recorded in your 'chat with myself' without a push alert.")
    return _page(200, "KakaoTalk connected", *lines)
