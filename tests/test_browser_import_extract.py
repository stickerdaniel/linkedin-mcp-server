"""Tests for cookie extraction and cross-platform decryption.

The OS keystore is mocked at the single accessor per OS. Encrypted fixtures are
built with the same primitives the production code uses so each round-trip is
self-consistent.
"""

import hashlib
import logging
import os
import sqlite3
import subprocess
import sys
import uuid

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from linkedin_mcp_server.browser_import import extract
from linkedin_mcp_server.browser_import.discovery import BrowserProfile
from linkedin_mcp_server.browser_import.extract import (
    _CBC_IV,
    _SAMESITE_MAP,
    LiAtMeta,
    _chromium_utc_to_unix,
    _derive_cbc_key,
    _expires_to_unix,
    extract_linkedin_cookies,
    has_undecryptable_li_at,
    read_li_at_meta,
)
from linkedin_mcp_server.exceptions import KeystoreUnavailableError

_MAC_PASSWORD = b"dGVzdHBhc3N3b3Jk"  # base64-looking string, used verbatim
_HOST = ".linkedin.com"


def _cbc_blob(plaintext: bytes, key: bytes, *, host_key: str, store_version: int):
    payload = (
        hashlib.sha256(host_key.encode()).digest() + plaintext
        if store_version >= 24
        else plaintext
    )
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(payload) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(_CBC_IV)).encryptor()
    return b"v10" + enc.update(padded) + enc.finalize()


def _gcm_blob(
    plaintext: bytes, master_key: bytes, *, host_key: str, store_version: int, nonce
):
    """Build a Windows v10 AES-256-GCM blob matching the real v24+ layout.

    Store version >= 24 prepends a 32-byte ``SHA256(host_key)`` digest to the
    plaintext on Windows too, exactly like the CBC path.
    """
    payload = (
        hashlib.sha256(host_key.encode()).digest() + plaintext
        if store_version >= 24
        else plaintext
    )
    ciphertext = AESGCM(master_key).encrypt(nonce, payload, None)
    return b"v10" + nonce + ciphertext


def _build_cookies_db(tmp_path, rows, *, version=24, legacy_columns=False):
    """Create a Cookies SQLite DB and return its path.

    ``rows`` is a list of dicts with keys: host_key, name, encrypted_value,
    value, path, expires_utc, last_access_utc, secure, httponly, samesite.
    """
    db_path = tmp_path / "Cookies"
    connection = sqlite3.connect(db_path)
    secure_col = "secure" if legacy_columns else "is_secure"
    httponly_col = "httponly" if legacy_columns else "is_httponly"
    connection.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('version', ?)", (str(version),)
    )
    connection.execute(
        f"""
        CREATE TABLE cookies (
            host_key TEXT, name TEXT, encrypted_value BLOB, value TEXT,
            path TEXT, expires_utc INTEGER, last_access_utc INTEGER,
            {secure_col} INTEGER, {httponly_col} INTEGER, samesite INTEGER
        )
        """
    )
    for row in rows:
        connection.execute(
            f"""
            INSERT INTO cookies
            (host_key, name, encrypted_value, value, path, expires_utc,
             last_access_utc, {secure_col}, {httponly_col}, samesite)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("host_key", _HOST),
                row["name"],
                row.get("encrypted_value", b""),
                row.get("value", ""),
                row.get("path", "/"),
                row.get("expires_utc", 0),
                row.get("last_access_utc", 0),
                row.get("secure", 1),
                row.get("httponly", 1),
                row.get("samesite", 2),
            ),
        )
    connection.commit()
    connection.close()
    return db_path


def _profile(
    cookies_db,
    *,
    browser="chrome",
    safe_storage="Chrome",
    keychain_service="",
    keychain_account="",
):
    return BrowserProfile(
        browser=browser,
        browser_label="Google Chrome",
        safe_storage_label=safe_storage,
        profile_dir_name="Default",
        display_name="Personal",
        user_data_root=cookies_db.parent,
        profile_path=cookies_db.parent,
        cookies_db=cookies_db,
        local_state_path=cookies_db.parent / "Local State",
        mac_keychain_service=keychain_service,
        mac_keychain_account=keychain_account,
    )


# ---------------------------------------------------------------------------
# macOS path (mocked)
# ---------------------------------------------------------------------------


def test_macos_extracts_and_strips_host_key_prefix(tmp_path, monkeypatch, caplog):
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    secret = b"AQED_li_at_secret"
    blob = _cbc_blob(secret, key, host_key=_HOST, store_version=24)
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": blob, "expires_utc": 0}],
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    with caplog.at_level(logging.INFO):
        cookies = extract_linkedin_cookies(_profile(db))

    assert len(cookies) == 1
    assert cookies[0].name == "li_at"
    assert cookies[0].value == secret.decode()
    # The decrypted value and the password must never appear in logs.
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert secret.decode() not in joined
    assert _MAC_PASSWORD.decode() not in joined


def test_macos_legacy_store_no_host_key_prefix(tmp_path, monkeypatch):
    # A pre-v24 store does NOT prepend the SHA256(host_key) digest; decryption
    # must return the value verbatim with no 32-byte prefix leakage.
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    secret = b"legacy_li_at_secret"
    blob = _cbc_blob(secret, key, host_key=_HOST, store_version=23)
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": blob}],
        version=23,
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    cookies = extract_linkedin_cookies(_profile(db))

    assert len(cookies) == 1
    assert cookies[0].value == secret.decode()


def test_macos_keystore_unavailable_propagates(tmp_path, monkeypatch):
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "value": "x"}])

    def _raise(account, service):
        raise KeystoreUnavailableError("no item")

    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(extract, "_macos_safe_storage_password", _raise)

    with pytest.raises(KeystoreUnavailableError):
        extract_linkedin_cookies(_profile(db))


def test_macos_safe_storage_password_runs_security(monkeypatch):
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")

        class R:
            returncode = 0
            stdout = b"bas64value\n"

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = extract._macos_safe_storage_password("Brave", "Brave Safe Storage")
    assert out == b"bas64value"
    # The primary query keys on the ACCOUNT (-a) alone; the account stays the
    # bare product name even when a fork renames the service.
    assert captured["cmd"] == [
        "security",
        "find-generic-password",
        "-a",
        "Brave",
        "-w",
    ]
    # A timeout is always passed so the keychain read can never hang the server.
    assert captured["timeout"] is not None


def test_macos_uses_default_safe_storage_service(tmp_path, monkeypatch):
    # No custom keychain service -> the default "<label> Safe Storage" pattern,
    # and the account defaults to the bare label.
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "value": "x"}])
    captured = {}

    def fake(account, service):
        captured["account"] = account
        captured["service"] = service
        return _MAC_PASSWORD

    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(extract, "_macos_safe_storage_password", fake)
    extract.extract_linkedin_cookies(_profile(db, safe_storage="Brave"))
    assert captured["account"] == "Brave"
    assert captured["service"] == "Brave Safe Storage"


def test_macos_uses_custom_keychain_service(tmp_path, monkeypatch):
    # Helium renames the keychain item: service "Helium Storage Key", not the
    # "... Safe Storage" suffix. The account stays the bare "Helium" (the
    # account-first primary key); the renamed service is the fallback.
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "value": "x"}])
    captured = {}

    def fake(account, service):
        captured["account"] = account
        captured["service"] = service
        return _MAC_PASSWORD

    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(extract, "_macos_safe_storage_password", fake)
    extract.extract_linkedin_cookies(
        _profile(
            db,
            browser="helium",
            safe_storage="Helium",
            keychain_service="Helium Storage Key",
        )
    )
    assert captured["account"] == "Helium"
    assert captured["service"] == "Helium Storage Key"


def test_macos_safe_storage_password_missing_item_raises(monkeypatch):
    class R:
        returncode = 44
        stdout = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    with pytest.raises(KeystoreUnavailableError):
        extract._macos_safe_storage_password(
            "Microsoft Edge", "Microsoft Edge Safe Storage"
        )


def test_macos_safe_storage_password_timeout_raises(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=10.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(KeystoreUnavailableError, match="timed out"):
        extract._macos_safe_storage_password("Chrome", "Chrome Safe Storage")


def test_macos_account_only_primary_then_service_fallback(monkeypatch):
    # The account-only primary query misses (fork without the account attribute);
    # the precise account+service pair then resolves it. Both are tried in order.
    attempts = []

    def fake_run(cmd, *args, **kwargs):
        attempts.append(cmd)

        class R:
            # Account-only (first attempt) misses; account+service (second) hits.
            returncode = 0 if "-s" in cmd else 44
            stdout = b"fallbackvalue\n" if "-s" in cmd else b""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = extract._macos_safe_storage_password("Helium", "Helium Storage Key")
    assert out == b"fallbackvalue"
    # Account-only first, then the account+service pair.
    assert attempts == [
        ["security", "find-generic-password", "-a", "Helium", "-w"],
        [
            "security",
            "find-generic-password",
            "-a",
            "Helium",
            "-s",
            "Helium Storage Key",
            "-w",
        ],
    ]


def test_macos_account_used_over_label_for_fork(tmp_path, monkeypatch):
    # Helium: the account ("Helium") is the primary key, the renamed service
    # ("Helium Storage Key") is the fallback the resolver passes through.
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "value": "x"}])
    captured = {}

    def fake(account, service):
        captured["account"] = account
        captured["service"] = service
        return _MAC_PASSWORD

    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(extract, "_macos_safe_storage_password", fake)
    extract.extract_linkedin_cookies(
        _profile(
            db,
            browser="helium",
            safe_storage="Helium",
            keychain_account="Helium",
            keychain_service="Helium Storage Key",
        )
    )
    assert captured["account"] == "Helium"
    assert captured["service"] == "Helium Storage Key"


def test_macos_account_defaults_to_safe_storage(tmp_path, monkeypatch):
    # An empty mac_keychain_account defaults the account to the bare label.
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "value": "x"}])
    captured = {}

    def fake(account, service):
        captured["account"] = account
        return _MAC_PASSWORD

    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(extract, "_macos_safe_storage_password", fake)
    extract.extract_linkedin_cookies(_profile(db, safe_storage="Brave"))
    assert captured["account"] == "Brave"


# ---------------------------------------------------------------------------
# Linux path (mocked): keyring v11 value vs peanuts v10 fallback + wrong key
# ---------------------------------------------------------------------------


def test_linux_secret_service_value_used(monkeypatch):
    class R:
        returncode = 0
        stdout = b"keyring-secret"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    # The accessor takes the resolved Secret Service token, not the label.
    assert extract._linux_safe_storage_password("chrome") == b"keyring-secret"


def test_linux_falls_back_to_peanuts(monkeypatch):
    class R:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert extract._linux_safe_storage_password("chrome") == b"peanuts"


def test_linux_resolver_uses_registry_token(tmp_path, monkeypatch):
    # The resolver must source the Secret Service token from the registry
    # ("microsoft-edge"), never derive a space-containing default from the label
    # ("microsoft edge").
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "value": "plain"}])
    captured = {}

    def fake(app_token):
        captured["app_token"] = app_token
        return b"peanuts"

    monkeypatch.setattr(extract, "_current_os", lambda: "linux")
    monkeypatch.setattr(extract, "_linux_safe_storage_password", fake)

    extract.extract_linkedin_cookies(
        _profile(db, browser="edge", safe_storage="Microsoft Edge")
    )

    assert captured["app_token"] == "microsoft-edge"


def test_linux_peanuts_decrypts_v10(tmp_path, monkeypatch):
    key = _derive_cbc_key(b"peanuts", iterations=1)
    secret = b"linux_li_at"
    blob = _cbc_blob(secret, key, host_key=_HOST, store_version=24)
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "encrypted_value": blob}])
    monkeypatch.setattr(extract, "_current_os", lambda: "linux")
    monkeypatch.setattr(
        extract, "_linux_safe_storage_password", lambda label: b"peanuts"
    )

    cookies = extract_linkedin_cookies(_profile(db))

    assert [c.name for c in cookies] == ["li_at"]
    assert cookies[0].value == secret.decode()


def test_linux_wrong_key_skips_with_host_key_mismatch(tmp_path, monkeypatch):
    real_key = _derive_cbc_key(b"keyring-secret", iterations=1)
    secret = b"linux_li_at"
    blob = _cbc_blob(secret, real_key, host_key=_HOST, store_version=24)
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "encrypted_value": blob}])
    monkeypatch.setattr(extract, "_current_os", lambda: "linux")
    # Wrong password: the peanuts fallback against a keyring-encrypted store.
    monkeypatch.setattr(
        extract, "_linux_safe_storage_password", lambda label: b"peanuts"
    )

    cookies = extract_linkedin_cookies(_profile(db))

    assert cookies == []  # skipped on SHA256(host_key) mismatch, no garbage


def test_wrong_key_on_legacy_store_skips_without_raising(tmp_path, monkeypatch):
    # Pre-v24 store: the host-key precheck is skipped (no digest is written), so
    # a wrong key trips the PKCS7 unpadder. The cookie must be skipped, not abort
    # the whole import with an uncaught ValueError.
    real_key = _derive_cbc_key(b"keyring-secret", iterations=1)
    secret = b"linux_li_at"
    blob = _cbc_blob(secret, real_key, host_key=_HOST, store_version=23)
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": blob}],
        version=23,
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "linux")
    monkeypatch.setattr(
        extract, "_linux_safe_storage_password", lambda label: b"peanuts"
    )

    cookies = extract_linkedin_cookies(_profile(db))

    assert cookies == []  # degraded gracefully, no traceback


# ---------------------------------------------------------------------------
# Windows path (mocked): GCM v10 with the v24+ SHA256(host_key) prefix
# ---------------------------------------------------------------------------


def test_windows_gcm_v10_decrypts(tmp_path, monkeypatch):
    master_key = b"\x11" * 32
    secret = b"win_li_at"
    nonce = b"\x02" * 12
    # store_version 24 (Chrome ~130+) prepends SHA256(host_key) to the plaintext.
    blob = _gcm_blob(secret, master_key, host_key=_HOST, store_version=24, nonce=nonce)
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "encrypted_value": blob}])
    monkeypatch.setattr(extract, "_current_os", lambda: "windows")
    monkeypatch.setattr(extract, "_windows_master_key", lambda path: master_key)

    cookies = extract_linkedin_cookies(_profile(db))

    assert [c.name for c in cookies] == ["li_at"]
    assert cookies[0].value == secret.decode()


def test_windows_gcm_v10_legacy_store_has_no_prefix(tmp_path, monkeypatch):
    # A pre-v24 Windows store does NOT prepend the host-key digest, so nothing
    # must be stripped after decrypt.
    master_key = b"\x33" * 32
    secret = b"win_legacy_li_at"
    nonce = b"\x04" * 12
    blob = _gcm_blob(secret, master_key, host_key=_HOST, store_version=23, nonce=nonce)
    db = _build_cookies_db(
        tmp_path, [{"name": "li_at", "encrypted_value": blob}], version=23
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "windows")
    monkeypatch.setattr(extract, "_windows_master_key", lambda path: master_key)

    cookies = extract_linkedin_cookies(_profile(db))

    assert [c.name for c in cookies] == ["li_at"]
    assert cookies[0].value == secret.decode()


def test_windows_master_key_accessor_is_mockable(monkeypatch, tmp_path):
    """The DPAPI binding is replaced wholesale; no OS service is hit."""
    monkeypatch.setattr(extract, "_windows_master_key", lambda path: b"\x00" * 32)
    assert extract._windows_master_key(tmp_path / "Local State") == b"\x00" * 32


# ---------------------------------------------------------------------------
# v20 degradation
# ---------------------------------------------------------------------------


def test_v20_cookie_is_counted_not_fatal(tmp_path, monkeypatch, caplog):
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    good = _cbc_blob(b"good_session", key, host_key=_HOST, store_version=24)
    db = _build_cookies_db(
        tmp_path,
        [
            {"name": "li_at", "encrypted_value": good},
            {"name": "JSESSIONID", "encrypted_value": b"v20" + b"\x00" * 40},
        ],
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    with caplog.at_level(logging.INFO):
        cookies = extract_linkedin_cookies(_profile(db))

    assert [c.name for c in cookies] == ["li_at"]
    assert "skipped 1 app-bound" in " ".join(r.getMessage() for r in caplog.records)


def test_unknown_prefix_is_counted_not_fatal(tmp_path, monkeypatch, caplog):
    # A corrupt or future-scheme prefix (here b"v99") must be skipped and counted,
    # never decrypted as garbage and never fatal to the import.
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    good = _cbc_blob(b"good_session", key, host_key=_HOST, store_version=24)
    db = _build_cookies_db(
        tmp_path,
        [
            {"name": "li_at", "encrypted_value": good},
            {"name": "bcookie", "encrypted_value": b"v99" + b"\x00" * 40},
        ],
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    with caplog.at_level(logging.INFO):
        cookies = extract_linkedin_cookies(_profile(db))

    assert [c.name for c in cookies] == ["li_at"]
    assert "skipped 1 app-bound" in " ".join(r.getMessage() for r in caplog.records)


def test_has_undecryptable_li_at_detects_v20(tmp_path):
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": b"v20" + b"\x00" * 40}],
    )
    assert has_undecryptable_li_at(_profile(db)) is True


def test_has_undecryptable_li_at_false_when_v10(tmp_path):
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": b"v10" + b"\x00" * 20}],
    )
    assert has_undecryptable_li_at(_profile(db)) is False


# ---------------------------------------------------------------------------
# read_li_at_meta: keychain-free expires/last_access/app_bound from the DB
# ---------------------------------------------------------------------------


def test_read_li_at_meta_future_expiry_and_last_access(tmp_path):
    # A live li_at: future expires_utc, a known last_access_utc. No keystore is
    # mocked because read_li_at_meta derives no key.
    expires_utc = 13_400_000_000 * 1_000_000
    last_access_utc = 13_300_000_000 * 1_000_000
    db = _build_cookies_db(
        tmp_path,
        [
            {
                "name": "li_at",
                "encrypted_value": b"v10" + b"\x00" * 20,
                "expires_utc": expires_utc,
                "last_access_utc": last_access_utc,
            }
        ],
    )

    meta = read_li_at_meta(_profile(db))

    assert meta is not None
    assert meta.expires == _expires_to_unix(expires_utc)
    assert meta.last_access == _chromium_utc_to_unix(last_access_utc)
    assert meta.app_bound is False


def test_read_li_at_meta_past_expiry_reflected(tmp_path):
    # A past expires_utc must be reported verbatim so the orchestrator can drop it.
    past_utc = 13_000_000_000 * 1_000_000
    db = _build_cookies_db(
        tmp_path,
        [
            {
                "name": "li_at",
                "encrypted_value": b"v10" + b"\x00" * 20,
                "expires_utc": past_utc,
            }
        ],
    )

    meta = read_li_at_meta(_profile(db))

    assert meta is not None
    assert meta.expires == _expires_to_unix(past_utc)


def test_read_li_at_meta_session_cookie_sentinel(tmp_path):
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": b"v10" + b"\x00" * 20, "expires_utc": 0}],
    )

    meta = read_li_at_meta(_profile(db))

    assert meta is not None
    assert meta.expires == -1.0


def test_read_li_at_meta_detects_app_bound(tmp_path):
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": b"v20" + b"\x00" * 40}],
    )

    meta = read_li_at_meta(_profile(db))

    assert meta is not None
    assert meta.app_bound is True


def test_read_li_at_meta_none_when_no_li_at(tmp_path):
    # LinkedIn cookies present, but no li_at -> None (distinct from "logged in").
    db = _build_cookies_db(
        tmp_path,
        [{"name": "bcookie", "encrypted_value": b"v10" + b"\x00" * 20}],
    )

    assert read_li_at_meta(_profile(db)) is None


def test_read_li_at_meta_returns_liatmeta_type(tmp_path):
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": b"v10" + b"\x00" * 20}],
    )
    assert isinstance(read_li_at_meta(_profile(db)), LiAtMeta)


# ---------------------------------------------------------------------------
# sameSite map and expires sentinel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [(-1, "Lax"), (0, "None"), (1, "Lax"), (2, "Strict")],
)
def test_samesite_map(raw, expected):
    assert _SAMESITE_MAP[raw] == expected


def test_expires_session_sentinel():
    assert _expires_to_unix(0) == -1.0


def test_expires_offset_formula():
    # 13_000_000_000 seconds after 1601 epoch, in microseconds.
    micros = 13_300_000_000 * 1_000_000
    assert _expires_to_unix(micros) == pytest.approx(13_300_000_000 - 11_644_473_600)


def test_chromium_utc_zero_sentinel():
    # The 0 -> 0.0 "never sent" branch is unique to _chromium_utc_to_unix; the
    # expires path short-circuits 0 to -1.0 before reaching it.
    assert _chromium_utc_to_unix(0) == 0.0


def test_chromium_utc_known_epoch():
    micros = 13_300_000_000 * 1_000_000
    assert _chromium_utc_to_unix(micros) == pytest.approx(
        13_300_000_000 - 11_644_473_600
    )


def test_cookie_samesite_and_expires_end_to_end(tmp_path, monkeypatch):
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    blob = _cbc_blob(b"x", key, host_key=_HOST, store_version=24)
    micros = 13_300_000_000 * 1_000_000
    db = _build_cookies_db(
        tmp_path,
        [
            {
                "name": "li_at",
                "encrypted_value": blob,
                "samesite": 0,
                "expires_utc": micros,
            }
        ],
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    cookie = extract_linkedin_cookies(_profile(db))[0]
    assert cookie.same_site == "None"
    assert cookie.expires == pytest.approx(13_300_000_000 - 11_644_473_600)
    pw = cookie.to_playwright()
    assert pw["sameSite"] == "None"
    assert pw["httpOnly"] is True


# ---------------------------------------------------------------------------
# Locked-DB / WAL handling + temp dir hardening + cleanup
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not portable on Windows"
)
def test_copies_db_and_wal_shm_with_secure_perms(tmp_path, monkeypatch):
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    blob = _cbc_blob(b"s", key, host_key=_HOST, store_version=24)
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "encrypted_value": blob}])
    (tmp_path / "Cookies-wal").write_text("wal")
    (tmp_path / "Cookies-shm").write_text("shm")

    copied_paths = []
    real_copy = extract.shutil.copy2

    def spy_copy(src, dst):
        copied_paths.append(str(src))
        return real_copy(src, dst)

    captured_temp = {}
    real_mkdtemp = extract.tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        captured_temp["dir"] = d
        return d

    monkeypatch.setattr(extract.shutil, "copy2", spy_copy)
    monkeypatch.setattr(extract.tempfile, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    extract_linkedin_cookies(_profile(db))

    # All three files were copied.
    assert any(p.endswith("Cookies") for p in copied_paths)
    assert any(p.endswith("Cookies-wal") for p in copied_paths)
    assert any(p.endswith("Cookies-shm") for p in copied_paths)
    # The temp dir is removed after extraction.
    assert not os.path.exists(captured_temp["dir"])


def test_temp_dir_removed_even_when_connect_fails(tmp_path, monkeypatch):
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "value": "plain"}])
    captured = {}
    real_mkdtemp = extract.tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        captured["dir"] = d
        return d

    monkeypatch.setattr(extract.tempfile, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(extract.sqlite3, "connect", boom)

    with pytest.raises(sqlite3.OperationalError):
        extract_linkedin_cookies(_profile(db))

    assert not os.path.exists(captured["dir"])


def test_temp_dir_removed_when_copy_fails(tmp_path, monkeypatch):
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "value": "plain"}])
    captured = {}
    real_mkdtemp = extract.tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        captured["dir"] = d
        return d

    def boom(src, dst):
        raise OSError("source unreadable")

    monkeypatch.setattr(extract.tempfile, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(extract.shutil, "copy2", boom)
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    with pytest.raises(OSError):
        extract_linkedin_cookies(_profile(db))

    # The just-created temp dir must not be orphaned when the copy itself fails.
    assert not os.path.exists(captured["dir"])


# ---------------------------------------------------------------------------
# Column drift + plaintext (unencrypted) values + no-secret logging
# ---------------------------------------------------------------------------


def test_legacy_columns_still_read(tmp_path, monkeypatch):
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    blob = _cbc_blob(b"legacy", key, host_key=_HOST, store_version=24)
    db = _build_cookies_db(
        tmp_path,
        [{"name": "li_at", "encrypted_value": blob, "secure": 1, "httponly": 0}],
        legacy_columns=True,
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    cookies = extract_linkedin_cookies(_profile(db))
    assert cookies[0].value == "legacy"
    assert cookies[0].http_only is False


def test_non_linkedin_cookies_filtered(tmp_path, monkeypatch):
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    blob = _cbc_blob(b"x", key, host_key=_HOST, store_version=24)
    other = _cbc_blob(b"y", key, host_key=".example.com", store_version=24)
    db = _build_cookies_db(
        tmp_path,
        [
            {"name": "li_at", "encrypted_value": blob, "host_key": _HOST},
            {"name": "sid", "encrypted_value": other, "host_key": ".example.com"},
        ],
    )
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    cookies = extract_linkedin_cookies(_profile(db))
    assert {c.name for c in cookies} == {"li_at"}


def test_no_pii_in_logs(tmp_path, monkeypatch, caplog):
    key = _derive_cbc_key(_MAC_PASSWORD, iterations=1003)
    blob = _cbc_blob(b"secret_value", key, host_key=_HOST, store_version=24)
    db = _build_cookies_db(tmp_path, [{"name": "li_at", "encrypted_value": blob}])
    profile = _profile(db)
    object.__setattr__(profile, "display_name", "alice@example.com")
    monkeypatch.setattr(extract, "_current_os", lambda: "macos")
    monkeypatch.setattr(
        extract, "_macos_safe_storage_password", lambda account, service: _MAC_PASSWORD
    )

    with caplog.at_level(logging.DEBUG):
        extract_linkedin_cookies(profile)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "secret_value" not in joined
    assert "alice@example.com" not in joined


# ---------------------------------------------------------------------------
# Live macOS keychain test (throwaway item; never reads the user's real key)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS keychain only")
def test_live_macos_keychain_throwaway_item(tmp_path):
    """Create and read a throwaway keychain item; never touch the real Chrome key."""
    service = f"LinkedInMCP-Test-{uuid.uuid4().hex[:8]} Safe Storage"
    password = "dGVzdGtleWNoYWlu"
    add = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-a",
            "linkedin-mcp-test",
            "-s",
            service,
            "-w",
            password,
        ],
        capture_output=True,
    )
    if add.returncode != 0:
        pytest.skip("could not create throwaway keychain item")
    try:
        # The account-only primary query (-a) matches the throwaway's account,
        # exercising the live keychain path without touching the real keys.
        out = extract._macos_safe_storage_password("linkedin-mcp-test", service)
        assert out == password.encode()
        key = _derive_cbc_key(out, iterations=1003)
        assert len(key) == 16
    finally:
        subprocess.run(
            ["security", "delete-generic-password", "-s", service],
            capture_output=True,
        )
