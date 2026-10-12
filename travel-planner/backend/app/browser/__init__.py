"""Browser automation layer (DESIGN §9, §13.2).

* :mod:`app.browser.manager` — :class:`BrowserManager` (one Playwright worker thread, sync
  API, fresh ``sync_playwright()`` per job) and the per-job :class:`BrowserHandle`.
* :mod:`app.browser.sessions` — dedicated login profiles, encrypted ``storage_state`` and
  the interactive :func:`start_login` flow.
"""

from __future__ import annotations

from app.browser.manager import (
    OFFSCREEN_ARGS,
    BrowserError,
    BrowserHandle,
    BrowserJobCancelled,
    BrowserManager,
    BrowserProfileLockedError,
    BrowserUnavailableError,
    get_browser_manager,
    shutdown_browser_manager,
)
from app.browser.sessions import (
    delete_session,
    filter_storage_state,
    load_storage_state,
    profile_dir,
    save_storage_state,
    session_status,
    start_login,
)

__all__ = [
    "OFFSCREEN_ARGS",
    "BrowserError",
    "BrowserHandle",
    "BrowserJobCancelled",
    "BrowserManager",
    "BrowserProfileLockedError",
    "BrowserUnavailableError",
    "delete_session",
    "filter_storage_state",
    "get_browser_manager",
    "load_storage_state",
    "profile_dir",
    "save_storage_state",
    "session_status",
    "shutdown_browser_manager",
    "start_login",
]
