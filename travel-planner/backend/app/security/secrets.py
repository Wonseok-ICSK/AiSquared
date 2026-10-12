"""SecretBox: Fernet encryption for secrets and session files (DESIGN §8).

Key storage, in order of preference:
1. Windows Credential Manager via ``keyring`` (service ``AiSquaredTravel``, user ``secretbox-key``).
2. ``data/secretbox.key.dpapi`` — the key protected with Windows DPAPI (current user), via ctypes.
3. ``data/secretbox.key`` — plain key file, last resort, always with a logged WARNING.

An existing key is always reused before a new one is generated, so data encrypted under a
fallback location stays readable. Key material and plaintexts are never logged.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "AiSquaredTravel"
KEYRING_USER = "secretbox-key"
TOKEN_PREFIX = "enc:v1:"
DPAPI_KEY_FILENAME = "secretbox.key.dpapi"
PLAIN_KEY_FILENAME = "secretbox.key"
_DPAPI_ENTROPY = b"AiSquaredTravel/secretbox"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretBoxError(Exception):
    """A token could not be decrypted (wrong key, corrupted or not a SecretBox token)."""


class SecretBox:
    """Symmetric encryption helper; ``SecretBox(key=...)`` injects a key (tests)."""

    def __init__(self, key: bytes | str | None = None) -> None:
        raw = key if key is not None else load_or_create_key(get_settings().data_dir)
        self._fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)

    @staticmethod
    def is_encrypted(value: object) -> bool:
        """True if ``value`` is a string produced by :meth:`encrypt_str`."""
        return isinstance(value, str) and value.startswith(TOKEN_PREFIX)

    def encrypt_str(self, plaintext: str) -> str:
        """Encrypt text to ``"enc:v1:<fernet token>"``."""
        return TOKEN_PREFIX + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt_str(self, token: str) -> str:
        """Decrypt a value produced by :meth:`encrypt_str`."""
        if not self.is_encrypted(token):
            raise SecretBoxError("value is not an enc:v1 token")
        return self.decrypt_bytes(token[len(TOKEN_PREFIX) :].encode("ascii")).decode("utf-8")

    def encrypt_bytes(self, data: bytes) -> bytes:
        """Encrypt bytes to a Fernet token (e.g. browser ``storage_state`` files)."""
        return self._fernet.encrypt(data)

    def decrypt_bytes(self, token: bytes) -> bytes:
        """Decrypt a Fernet token from :meth:`encrypt_bytes`."""
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise SecretBoxError("invalid token or wrong key") from exc


# --------------------------------------------------------------------------- DPAPI (ctypes)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_available() -> bool:
    """True on Windows, where DPAPI (crypt32) exists."""
    return sys.platform == "win32"


def _dpapi_call(func_name: str, data: bytes) -> bytes:
    from ctypes import wintypes

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func = getattr(crypt32, func_name)
    func.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    func.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_buf = ctypes.create_string_buffer(data, len(data))
    ent_buf = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    in_blob = _DataBlob(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    ent_blob = _DataBlob(
        len(_DPAPI_ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_char))
    )
    out_blob = _DataBlob()
    ok = func(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(ent_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{func_name} failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def dpapi_protect(data: bytes) -> bytes:
    """Encrypt ``data`` for the current Windows user (CryptProtectData)."""
    return _dpapi_call("CryptProtectData", data)


def dpapi_unprotect(blob: bytes) -> bytes:
    """Decrypt a blob produced by :func:`dpapi_protect` (CryptUnprotectData)."""
    return _dpapi_call("CryptUnprotectData", blob)


# --------------------------------------------------------------------------- key resolution


def _keyring_get() -> bytes | None:
    try:
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception as exc:  # noqa: BLE001 - any backend failure means "unavailable"
        logger.info("Credential Manager unavailable (%s); using file fallback", type(exc).__name__)
        return None
    return value.encode("ascii") if value else None


def _keyring_store(key: bytes) -> bool:
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.decode("ascii"))
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) == key.decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not store key in Credential Manager (%s)", type(exc).__name__)
        return False


def _read_key_file(data_dir: Path) -> bytes | None:
    dpapi_path = data_dir / DPAPI_KEY_FILENAME
    if dpapi_path.exists() and dpapi_available():
        try:
            return dpapi_unprotect(dpapi_path.read_bytes())
        except OSError as exc:
            raise SecretBoxError(
                f"cannot unprotect {dpapi_path} (created by another Windows user?)"
            ) from exc
    plain_path = data_dir / PLAIN_KEY_FILENAME
    if plain_path.exists():
        logger.warning("Using UNPROTECTED secret key file %s", plain_path)
        return plain_path.read_bytes().strip()
    return None


def _write_key_file(data_dir: Path, key: bytes) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    if dpapi_available():
        try:
            (data_dir / DPAPI_KEY_FILENAME).write_bytes(dpapi_protect(key))
            logger.info("Secret key stored DPAPI-protected in %s", data_dir / DPAPI_KEY_FILENAME)
            return
        except OSError as exc:
            logger.warning("DPAPI protection failed (%s)", exc)
    plain_path = data_dir / PLAIN_KEY_FILENAME
    plain_path.write_bytes(key)
    logger.warning(
        "Secret key stored in an UNPROTECTED file %s (no Credential Manager or DPAPI). "
        "Anyone who can read this file can decrypt stored secrets.",
        plain_path,
    )


def load_or_create_key(data_dir: Path) -> bytes:
    """Return the Fernet key, creating and storing a new one on first use."""
    key = _keyring_get() or _read_key_file(data_dir)
    if key:
        return key
    key = Fernet.generate_key()
    if _keyring_store(key):
        logger.info("Generated new secret key in Windows Credential Manager")
    else:
        _write_key_file(data_dir, key)
    return key


# --------------------------------------------------------------------------- singleton

_box: SecretBox | None = None
_box_lock = threading.Lock()


def get_secretbox() -> SecretBox:
    """Process-wide SecretBox (key resolved on first use)."""
    global _box
    if _box is None:
        with _box_lock:
            if _box is None:
                _box = SecretBox()
    return _box


def set_secretbox(box: SecretBox | None) -> None:
    """Replace (or reset with ``None``) the singleton — used by tests."""
    global _box
    with _box_lock:
        _box = box
