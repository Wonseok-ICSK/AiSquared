"""FastAPI application factory.

Run from ``backend/``:
    ..\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.convertors import Convertor, register_url_convertor

from app import __version__
from app.api import API_PREFIX, include_routers
from app.config import FRONTEND_DIST, Settings, ensure_dirs, get_settings
from app.db import init_db, session_scope
from app.schemas import HealthOut
from app.services import monitoring
from app.services.scheduler import get_scheduler
from app.settings_store import load_runtime_settings

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FILE_MAX_BYTES = 2_000_000
LOG_FILE_BACKUPS = 5
BROWSER_SHUTDOWN_WAIT_S = 10.0
"""How long shutdown waits for a running browser job (it is cancelled first)."""
QUIET_LOGGERS = ("httpx", "httpcore")
_API_SEGMENT = API_PREFIX.strip("/")


class NonApiPathConvertor(Convertor[str]):
    """Like ``path`` but never matches ``api`` or ``api/...``.

    The SPA catch-all uses it so it does not even partially match API paths: unknown
    ``/api/*`` paths then get 404 for every method (instead of 405 from a GET-only
    catch-all), and known API paths keep their normal 405 for a wrong method.
    """

    regex = rf"(?!{re.escape(_API_SEGMENT)}(?:/|$)).*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("spa_path", NonApiPathConvertor())


def configure_logging(settings: Settings) -> None:
    """Send app logs (INFO+) to the console and ``data/logs/app.log`` (once per process)."""
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(
        settings.logs_dir / "app.log",
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8",
    )
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    # httpx logs full request URLs at INFO, which can include API keys in query strings.
    for noisy in QUIET_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run_hook(what: str, hook: Callable[[], object]) -> None:
    """Run one startup/shutdown step; a failure is logged and never stops the others."""
    try:
        hook()
    except Exception:  # noqa: BLE001 - the app must start/stop even if one step fails
        logger.exception("Could not %s", what)


def _recover_itineraries() -> None:
    from app.services.itinerary.service import recover_stale_itineraries

    recover_stale_itineraries()


def _configure_browser() -> None:
    """Apply the runtime browser settings (channel, headless) to the shared BrowserManager."""
    from app.browser.manager import get_browser_manager

    with session_scope() as db:
        runtime = load_runtime_settings(db)
    get_browser_manager().configure(
        channel=runtime.browser_channel, headless=runtime.browser_headless
    )


def _shutdown_browser() -> None:
    from app.browser.manager import shutdown_browser_manager

    shutdown_browser_manager(wait=True, timeout=BROWSER_SHUTDOWN_WAIT_S)


def start_background_services() -> None:
    """Startup hooks after the DB is ready: mark runs/itineraries left ``running`` by a
    previous process as interrupted, configure the browser from the runtime settings and
    start the monitoring scheduler (a no-op when ``SCHEDULER_ENABLED=false``)."""
    _run_hook("recover interrupted runs", monitoring.recover_stale_runs)
    _run_hook("recover interrupted itineraries", _recover_itineraries)
    _run_hook("configure the browser", _configure_browser)
    _run_hook("start the scheduler", lambda: get_scheduler().start())


def stop_background_services() -> None:
    """Shutdown hooks: stop the scheduler (and checks in progress), then the browser."""
    _run_hook("stop the scheduler", lambda: get_scheduler().shutdown())
    _run_hook("shut down the browser", _shutdown_browser)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: data dirs, logging, DB schema, background services; shutdown: stop them."""
    settings = get_settings()
    ensure_dirs(settings)
    configure_logging(settings)
    init_db()
    start_background_services()
    logger.info("AiSquared Travel %s started", __version__)
    try:
        yield
    finally:
        stop_background_services()
        logger.info("AiSquared Travel %s stopped", __version__)


def mount_spa(app: FastAPI, dist: Path) -> bool:
    """Serve the built SPA from ``dist`` at ``/`` (index.html for unknown non-API paths)."""
    index = dist / "index.html"
    if not index.is_file():
        logger.info("Frontend build not found at %s; serving API only", dist)
        return False
    root = dist.resolve()

    @app.get("/{full_path:spa_path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)

    return True


def create_app(frontend_dist: Path = FRONTEND_DIST) -> FastAPI:
    """Build the FastAPI app: health, API routers, then the SPA catch-all."""
    app = FastAPI(title="AiSquared Travel", version=__version__, lifespan=lifespan)

    @app.get(f"{API_PREFIX}/health", response_model=HealthOut, tags=["meta"])
    def health() -> HealthOut:
        return HealthOut(version=__version__)

    include_routers(app)  # <- routers are discovered in app/api/__init__.py
    mount_spa(app, frontend_dist)  # must stay last: catch-all route
    return app


app = create_app()
