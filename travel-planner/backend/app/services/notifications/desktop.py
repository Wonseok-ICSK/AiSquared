"""Windows toast notifications via ``windows-toasts`` (DESIGN §13.3).

Clicking the toast (or its button) opens ``message.url`` through protocol activation, which
works even after this process has exited (Windows opens the default browser).

AUMID: unpackaged Python has no AppUserModelID, so toasts would be attributed to
"Command Prompt" and ``ToastNotifier.setting`` raises "Element not found". We therefore
register our own AUMID :data:`AUMID` once per process under
``HKCU\\Software\\Classes\\AppUserModelId\\AiSquared.Travel`` with
``DisplayName = "AiSquared Travel"`` (what the package's ``register_hkey_aumid`` script
does). AUMIDs may not contain spaces, hence the dotted id. The key is per-user, needs no
admin rights and can be removed with
``reg delete "HKCU\\Software\\Classes\\AppUserModelId\\AiSquared.Travel" /f``.

Toasts only appear when the backend runs in the logged-in user's interactive session (not
as a service / "run whether user is logged on or not" task); Focus Assist hides banners.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path

from app.services.events import NotificationMessage
from app.services.notifications.base import (
    ChannelError,
    NotificationChannel,
    is_http_url,
    register_channel,
    summary_text,
    truncate_text,
)
from app.settings_store import RuntimeSettings

logger = logging.getLogger(__name__)

AUMID = "AiSquared.Travel"
APP_DISPLAY_NAME = "AiSquared Travel"
AUMID_REGISTRY_PATH = rf"SOFTWARE\Classes\AppUserModelId\{AUMID}"
TOAST_GROUP = "aisquared-travel"
BODY_LIMIT = 250

_register_lock = threading.Lock()
_registered = False


def _is_windows() -> bool:
    return sys.platform == "win32"


def toast_support() -> tuple[bool, str]:
    """``(True, "")`` when toasts can be shown on this machine, else ``(False, why)``."""
    if not _is_windows():
        return False, "desktop toasts are only available on Windows"
    if importlib.util.find_spec("windows_toasts") is None:
        return False, "the 'windows-toasts' package is not installed"
    return True, ""


def _write_aumid_key(display_name: str, icon_path: Path | None) -> None:
    import winreg  # Windows-only stdlib module

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUMID_REGISTRY_PATH) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
        if icon_path is not None and icon_path.is_file():
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon_path.resolve()))


def register_aumid(icon_path: Path | None = None) -> bool:
    """Register :data:`AUMID` under HKCU (once per process). ``False`` on non-Windows/failure."""
    global _registered
    if not _is_windows():
        return False
    with _register_lock:
        if _registered:
            return True
        try:
            _write_aumid_key(APP_DISPLAY_NAME, icon_path)
        except OSError as exc:
            logger.warning("Could not register toast AUMID %s: %s", AUMID, exc)
            return False
        _registered = True
        logger.info("Registered toast AUMID %s (%s)", AUMID, APP_DISPLAY_NAME)
        return True


def _show_toast(message: NotificationMessage) -> None:
    from windows_toasts import InteractableWindowsToaster, Toast, ToastButton

    url = message.url if is_http_url(message.url) else None
    body = truncate_text(summary_text(message), BODY_LIMIT)
    toast = Toast(
        text_fields=[message.title, body],
        group=TOAST_GROUP,
        launch_action=url,
        actions=[ToastButton(content=message.url_label, launch=url)] if url else (),
    )
    InteractableWindowsToaster(APP_DISPLAY_NAME, notifierAUMID=AUMID).show_toast(toast)


class DesktopChannel(NotificationChannel):
    """Windows 10/11 toast; click opens the booking page."""

    key = "desktop"
    display_name = "Desktop (Windows toast)"
    description = "Windows notification on this PC; click opens the booking page."

    def is_configured(self, rs: RuntimeSettings) -> tuple[bool, str]:
        """Configured whenever toasts are supported on this machine."""
        return toast_support()

    def send(self, message: NotificationMessage, rs: RuntimeSettings) -> None:
        """Show a toast; any toast/WinRT error becomes a :class:`ChannelError`."""
        supported, why = toast_support()
        if not supported:
            raise ChannelError(why)
        register_aumid()
        try:
            _show_toast(message)
        except Exception as exc:  # noqa: BLE001 - WinRT raises plain OSError/RuntimeError
            logger.warning("Desktop toast failed: %s: %s", type(exc).__name__, exc)
            raise ChannelError(f"toast failed: {type(exc).__name__}: {exc}") from exc


register_channel(DesktopChannel())
