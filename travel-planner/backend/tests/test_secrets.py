from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.security import secrets as secrets_module
from app.security.secrets import (
    DPAPI_KEY_FILENAME,
    PLAIN_KEY_FILENAME,
    SecretBox,
    SecretBoxError,
    dpapi_protect,
    dpapi_unprotect,
    get_secretbox,
    load_or_create_key,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


@pytest.fixture
def key_dir(tmp_path: Path) -> Path:
    """Empty directory for key files (separate from the test DATA_DIR)."""
    return tmp_path / "keys"


def test_str_roundtrip(secretbox: SecretBox) -> None:
    token = secretbox.encrypt_str("sk-ant-테스트")
    assert token.startswith("enc:v1:") and "sk-ant" not in token
    assert SecretBox.is_encrypted(token) and not SecretBox.is_encrypted("plain")
    assert secretbox.decrypt_str(token) == "sk-ant-테스트"


def test_bytes_roundtrip(secretbox: SecretBox) -> None:
    blob = secretbox.encrypt_bytes(b'{"cookies": []}')
    assert secretbox.decrypt_bytes(blob) == b'{"cookies": []}'


def test_wrong_key_and_tampering_fail(secretbox: SecretBox) -> None:
    token = secretbox.encrypt_str("secret")
    with pytest.raises(SecretBoxError):
        SecretBox(key=Fernet.generate_key()).decrypt_str(token)
    with pytest.raises(SecretBoxError):
        secretbox.decrypt_str(token[:-4] + "AAAA")
    with pytest.raises(SecretBoxError):
        secretbox.decrypt_str("not-encrypted")


def test_singleton_is_injected_box(secretbox: SecretBox) -> None:
    assert get_secretbox() is secretbox


@windows_only
def test_dpapi_roundtrip() -> None:
    protected = dpapi_protect(b"fernet-key-material")
    assert protected != b"fernet-key-material"
    assert dpapi_unprotect(protected) == b"fernet-key-material"


@windows_only
def test_key_falls_back_to_dpapi_file_and_is_reused(key_dir: Path) -> None:
    # keyring access raises (blocked by conftest) -> DPAPI-protected key file
    first = load_or_create_key(key_dir)
    assert (key_dir / DPAPI_KEY_FILENAME).exists()
    assert not (key_dir / PLAIN_KEY_FILENAME).exists()
    assert first not in (key_dir / DPAPI_KEY_FILENAME).read_bytes()
    assert load_or_create_key(key_dir) == first
    Fernet(first)  # valid key


def test_key_prefers_keyring(key_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        secrets_module.keyring, "get_password", lambda svc, user: vault.get((svc, user))
    )
    monkeypatch.setattr(
        secrets_module.keyring,
        "set_password",
        lambda svc, user, value: vault.__setitem__((svc, user), value),
    )
    key = load_or_create_key(key_dir)
    assert vault[("AiSquaredTravel", "secretbox-key")] == key.decode()
    assert not key_dir.exists()  # nothing written to disk
    assert load_or_create_key(key_dir) == key


def test_plain_key_file_only_with_warning(
    key_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(secrets_module, "dpapi_available", lambda: False)
    with caplog.at_level("WARNING"):
        key = load_or_create_key(key_dir)
    assert (key_dir / PLAIN_KEY_FILENAME).read_bytes() == key
    assert "UNPROTECTED" in caplog.text
    assert key.decode() not in caplog.text
