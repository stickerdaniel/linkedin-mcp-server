"""Copy a browser's Cookies database and decrypt its LinkedIn cookies.

Owns the locked-DB copy (with WAL/SHM sidecars), the SQLite read with column
drift, the version branch, OS keystore access (one injectable accessor per OS),
the PBKDF2 iteration count, and the platform-aware v20 app-bound skip.

Cryptographic constants are fixed by Chromium's cookie format:
- salt ``saltysalt``; AES-128-CBC for macOS/Linux; IV = 16 space bytes.
- PBKDF2-HMAC-SHA1, 1003 iterations on macOS, 1 iteration on Linux, dklen 16.
- v10/v11 prefixes are 3 bytes. Store version >= 24 prepends a 32-byte
  ``SHA256(host_key)`` digest inside the plaintext on every platform (decrypt
  -> unpad -> strip-32 for CBC; decrypt -> strip-32 for Windows GCM).
- Windows v10 cookies are AES-256-GCM under a DPAPI-protected master key.
- v20 is Chrome 127+ app-bound encryption and needs OS elevation; we skip it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from linkedin_mcp_server.browser_import.discovery import (
    SUPPORTED_BROWSERS,
    BrowserProfile,
)
from linkedin_mcp_server.exceptions import (
    KeystoreUnavailableError,
    V20EncryptedError,
)

logger = logging.getLogger(__name__)

_SALT = b"saltysalt"
_CBC_IV = b" " * 16
_KEY_LENGTH = 16
_MACOS_ITERATIONS = 1003
_LINUX_ITERATIONS = 1
_HOST_KEY_PREFIX_LEN = 32  # SHA256(host_key) prepended for store version >= 24
_HOST_KEY_PREFIX_MIN_VERSION = 24
_LINUX_FALLBACK_PASSWORD = b"peanuts"

# SQLite samesite int -> Playwright string. -1 (UNSPECIFIED) maps to Chromium's
# default of Lax. Documented and unit-tested.
_SAMESITE_MAP = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}

# Windows epoch offset: Chromium stores expires_utc as microseconds since
# 1601-01-01; subtract this many seconds to reach the unix epoch.
_WINDOWS_EPOCH_OFFSET_SECONDS = 11_644_473_600


@dataclass(frozen=True)
class LinkedInCookie:
    """A single decrypted LinkedIn cookie ready for Playwright injection."""

    name: str
    value: str  # decrypted plaintext (NEVER logged)
    domain: str
    path: str
    expires: float  # unix seconds; -1 for session cookies (Playwright sentinel)
    secure: bool
    http_only: bool
    same_site: str  # "Strict" | "Lax" | "None"

    def to_playwright(self) -> dict[str, object]:
        """Return the Playwright ``add_cookies`` shape.

        Domain is normalized so the existing ``_normalize_cookie_domain`` pass is
        a no-op. ``sameSite`` is always one of {"Strict", "Lax", "None"};
        ``expires`` is a float (or the -1 session sentinel).
        """
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "expires": self.expires,
            "secure": self.secure,
            "httpOnly": self.http_only,
            "sameSite": self.same_site,
        }


def _derive_cbc_key(password: bytes, *, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA1(password, salt='saltysalt', iterations, dklen=16).

    macOS callers pass ``iterations=1003``; Linux callers pass ``iterations=1``.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=_KEY_LENGTH,
        salt=_SALT,
        iterations=iterations,
    )
    return kdf.derive(password)


def _macos_safe_storage_password(account: str, service: str) -> bytes:
    """Read the macOS Safe Storage password from the login keychain.

    Queries by ACCOUNT first (``-a <account>``): the account stays the bare
    product name even when a Chromium fork renames the keychain SERVICE (Helium's
    account is "Helium" but its service is "Helium Storage Key"). Falls back to
    the precise account+service pair when the account-only match is absent. The
    returned base64-looking string is used VERBATIM as the PBKDF2 password (it is
    NOT base64-decoded). Raises :class:`KeystoreUnavailableError` only when both
    queries fail. Logs only the tokens, never the password.
    """
    queries = (
        ["security", "find-generic-password", "-a", account, "-w"],
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
    )
    last_returncode: int | None = None
    for argv in queries:
        try:
            result = subprocess.run(
                argv, capture_output=True, check=False, timeout=10.0
            )
        except subprocess.TimeoutExpired as exc:
            # macOS Tahoe can hang the keychain CLI indefinitely when the process
            # lost SecurityAgent context; check=False guards a non-zero exit, not
            # a hang. Bound it so the server never stalls on the first tool call.
            raise KeystoreUnavailableError(
                f"macOS keychain read for account {account!r} timed out"
            ) from exc
        except OSError as exc:
            raise KeystoreUnavailableError(
                f"Could not run the macOS security tool for {account!r}: {exc}"
            ) from exc
        if result.returncode == 0:
            # The keychain value is the base64 string itself, used as-is.
            return result.stdout.rstrip(b"\n")
        last_returncode = result.returncode
    raise KeystoreUnavailableError(
        f"macOS keychain has no Safe Storage key for account {account!r} / "
        f"service {service!r} (exit {last_returncode}; the browser may not have "
        "created it yet)."
    )


def _linux_safe_storage_password(app_token: str) -> bytes:
    """Read the Linux Secret Service password for ``app_token``, else ``peanuts``.

    ``app_token`` is the registry ``linux_app_token`` (e.g. "chrome", "chromium",
    "microsoft-edge"). A real keyring value yields v11 blobs; the ``peanuts``
    fallback yields v10.
    """
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", app_token],
            capture_output=True,
            check=False,
            timeout=10.0,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except subprocess.TimeoutExpired:
        # An absent gnome-keyring or an unresponsive D-Bus session can hang
        # secret-tool forever; bound it like the macOS keychain read so the
        # import never stalls the server, then fall back to peanuts.
        logger.debug("secret-tool timed out; using peanuts fallback")
    except OSError:
        logger.debug("secret-tool unavailable; using peanuts fallback")
    return _LINUX_FALLBACK_PASSWORD


def _windows_master_key(local_state_path: Path) -> bytes:  # pragma: no cover
    """Decrypt the Windows DPAPI-protected AES-256 master key from Local State.

    Reads ``os_crypt.encrypted_key`` (base64), strips the 5-byte ``DPAPI``
    prefix, then ``CryptUnprotectData`` via ctypes. Untested on CI (the dev/CI
    host is macOS); see the module docstring. Exercised only via mocked unit
    tests (constraint 6).
    """
    import ctypes
    import ctypes.wintypes
    import json

    payload = json.loads(local_state_path.read_text())
    encrypted_key = base64.b64decode(payload["os_crypt"]["encrypted_key"])
    if encrypted_key[:5] != b"DPAPI":
        raise KeystoreUnavailableError("Local State key lacks the DPAPI prefix")
    blob_in = encrypted_key[5:]

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    buffer_in = ctypes.create_string_buffer(blob_in, len(blob_in))
    blob_in_struct = DATA_BLOB(len(blob_in), buffer_in)
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32  # ty: ignore[unresolved-attribute]
    kernel32 = ctypes.windll.kernel32  # ty: ignore[unresolved-attribute]
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in_struct),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise KeystoreUnavailableError("CryptUnprotectData failed for the master key")
    try:
        key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
    return key


def _decrypt_cbc(blob: bytes, key: bytes, *, store_version: int) -> str:
    """Decrypt a v10/v11 macOS/Linux cookie blob.

    Strips the 3-byte tag, AES-128-CBC decrypts (IV = 16 spaces), PKCS7-unpads,
    then strips the leading 32-byte ``SHA256(host_key)`` digest when
    ``store_version >= 24`` (verified ordering: decrypt -> unpad -> strip-32).
    """
    ciphertext = blob[3:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_CBC_IV)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    if store_version >= _HOST_KEY_PREFIX_MIN_VERSION:
        plaintext = plaintext[_HOST_KEY_PREFIX_LEN:]
    return plaintext.decode("utf-8", errors="replace")


def _decrypt_gcm_v10(blob: bytes, master_key: bytes, *, store_version: int) -> str:
    """Decrypt a Windows v10 AES-256-GCM cookie blob.

    Layout: ``b'v10' || nonce(12) || ciphertext || tag(16)``. Store version >= 24
    (Chrome ~130+) prepends a 32-byte ``SHA256(host_key)`` digest inside the
    decrypted plaintext on Windows too, so strip it like the CBC path does.
    Untested on CI (macOS host); mocked-only.
    """
    nonce = blob[3:15]
    ciphertext = blob[15:]
    plaintext = AESGCM(master_key).decrypt(nonce, ciphertext, None)
    if store_version >= _HOST_KEY_PREFIX_MIN_VERSION:
        plaintext = plaintext[_HOST_KEY_PREFIX_LEN:]
    return plaintext.decode("utf-8", errors="replace")


def _verify_host_key_prefix(blob: bytes, key: bytes, host_key: str) -> bool:
    """Return whether a v10/v11 CBC blob decrypts to the expected host-key digest.

    Used to detect a wrong Safe Storage password (e.g. the Linux ``peanuts``
    fallback against a keyring-encrypted store): on mismatch the caller skips
    the cookie instead of emitting garbage.
    """
    try:
        ciphertext = blob[3:]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(_CBC_IV)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        return False
    expected = hashlib.sha256(host_key.encode("utf-8")).digest()
    return plaintext[:_HOST_KEY_PREFIX_LEN] == expected


def _decrypt_value(
    blob: bytes,
    plaintext_value: str,
    *,
    cbc_key: bytes | None,
    win_master_key: bytes | None,
    store_version: int,
    is_macos_or_linux: bool,
) -> str:
    """Decrypt one cookie value, branching on the 3-byte prefix.

    If ``plaintext_value`` is non-empty the cookie was never encrypted; return
    it. Otherwise: ``v10``/``v11`` -> CBC (mac/linux) or GCM (windows); ``v20``
    -> raise :class:`V20EncryptedError`. The v20 message is platform-aware and
    never asserts "Windows" on a macOS/Linux blob.
    """
    if plaintext_value:
        return plaintext_value
    if not blob:
        return ""
    prefix = blob[:3]
    if prefix == b"v20":
        if is_macos_or_linux:
            raise V20EncryptedError(
                "Cookie uses app-bound encryption (v20); decryption requires OS "
                "elevation and is not supported."
            )
        raise V20EncryptedError(
            "Cookie uses Chrome 127+ app-bound encryption (v20); decryption "
            "requires OS elevation and is not supported."
        )
    if prefix in (b"v10", b"v11"):
        if is_macos_or_linux:
            if cbc_key is None:
                raise KeystoreUnavailableError(
                    "No Safe Storage key available for CBC decryption"
                )
            return _decrypt_cbc(blob, cbc_key, store_version=store_version)
        if win_master_key is None:
            raise KeystoreUnavailableError(
                "No DPAPI master key available for GCM decryption"
            )
        return _decrypt_gcm_v10(blob, win_master_key, store_version=store_version)
    # Unknown prefix: treat as undecryptable rather than emit garbage.
    raise V20EncryptedError(f"Cookie uses an unsupported encryption prefix {prefix!r}")


def _cookie_columns(connection: sqlite3.Connection) -> dict[str, str]:
    """Resolve secure/httponly column names across SQLite schema drift."""
    cursor = connection.execute("PRAGMA table_info(cookies)")
    columns = {row[1] for row in cursor.fetchall()}
    secure = "is_secure" if "is_secure" in columns else "secure"
    http_only = "is_httponly" if "is_httponly" in columns else "httponly"
    return {"secure": secure, "http_only": http_only}


def _meta_version(connection: sqlite3.Connection) -> int:
    try:
        cursor = connection.execute("SELECT value FROM meta WHERE key='version'")
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except (sqlite3.Error, ValueError):
        return 0


def _chromium_utc_to_unix(value: int) -> float:
    """Convert a Chromium microseconds-since-1601 timestamp to unix seconds.

    Returns 0.0 for a 0 input (the "never" sentinel for ``last_access_utc`` and
    friends). Callers that need the session-cookie semantics for ``expires_utc``
    use :func:`_expires_to_unix` instead.
    """
    if value == 0:
        return 0.0
    return value / 1_000_000 - _WINDOWS_EPOCH_OFFSET_SECONDS


def _expires_to_unix(expires_utc: int) -> float:
    """Convert Chromium ``expires_utc`` microseconds to unix seconds.

    A value of 0 marks a session cookie -> the Playwright -1 sentinel. The 0
    value must not be run through the offset (that yields an already-expired
    cookie Playwright drops).
    """
    if expires_utc == 0:
        return -1.0
    return _chromium_utc_to_unix(expires_utc)


def _copy_cookies_db(cookies_db: Path) -> tuple[Path, Path]:
    """Copy the Cookies DB and its WAL/SHM sidecars into a hardened temp dir.

    Returns ``(temp_dir, db_copy)``. The caller removes ``temp_dir`` in a
    ``finally`` block. WAL/SHM are copied so a just-issued ``li_at`` that is
    committed but not yet checkpointed is visible. The live DB is never opened.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="linkedin-cookie-import-"))
    try:
        try:
            os.chmod(temp_dir, 0o700)
        except OSError:
            pass
        db_copy = temp_dir / "Cookies"
        shutil.copy2(cookies_db, db_copy)
        os.chmod(db_copy, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = cookies_db.with_name(cookies_db.name + suffix)
            if sidecar.is_file():
                sidecar_copy = temp_dir / (db_copy.name + suffix)
                shutil.copy2(sidecar, sidecar_copy)
                os.chmod(sidecar_copy, 0o600)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return temp_dir, db_copy


def _current_os() -> str:
    """Return ``"macos"``, ``"windows"`` or ``"linux"`` for the running host.

    Single OS-detection seam so tests can select a decryption path without
    monkeypatching ``os.name`` (which would break ``pathlib`` on the dev host).
    """
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def _resolve_keystore(
    profile: BrowserProfile,
) -> tuple[bytes | None, bytes | None, bool]:
    """Return ``(cbc_key, win_master_key, is_mac_or_linux)``."""
    current = _current_os()
    if current == "macos":
        account = profile.mac_keychain_account or profile.safe_storage_label
        service = (
            profile.mac_keychain_service or f"{profile.safe_storage_label} Safe Storage"
        )
        password = _macos_safe_storage_password(account, service)
        return _derive_cbc_key(password, iterations=_MACOS_ITERATIONS), None, True
    if current == "windows":
        return None, _windows_master_key(profile.local_state_path), False
    spec = SUPPORTED_BROWSERS.get(profile.browser, {})
    app_token = (
        str(spec.get("linux_app_token", "")) or profile.safe_storage_label.lower()
    )
    password = _linux_safe_storage_password(app_token)
    return _derive_cbc_key(password, iterations=_LINUX_ITERATIONS), None, True


def extract_linkedin_cookies(profile: BrowserProfile) -> list[LinkedInCookie]:
    """Copy *profile*'s Cookies DB and return its decrypted LinkedIn cookies.

    Copies the DB (+ WAL/SHM) to a ``0o600``-hardened temp dir, opens it
    read-only (``mode=ro``, not ``immutable``, so the copied WAL is applied and a
    just-issued ``li_at`` still in the WAL is visible), reads ``meta.version`` and
    the secure/httponly columns,
    filters ``host_key`` in Python with the repo's ``"linkedin.com" in host_key``
    convention (no SQL ``LIKE`` interpolation), and decrypts each value.
    Skip-and-warn (by count) on :class:`V20EncryptedError` and on wrong-key
    ``SHA256(host_key)`` mismatch. Returns the FULL LinkedIn cookie set.

    Raises :class:`KeystoreUnavailableError` when the OS keystore is unavailable.
    """
    cbc_key, win_master_key, is_macos_or_linux = _resolve_keystore(profile)

    temp_dir, db_copy = _copy_cookies_db(profile.cookies_db)
    cookies: list[LinkedInCookie] = []
    skipped_app_bound = 0
    skipped_wrong_key = 0
    try:
        uri = f"file:{db_copy}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.row_factory = sqlite3.Row
            store_version = _meta_version(connection)
            cols = _cookie_columns(connection)
            query = (
                "SELECT host_key, name, encrypted_value, value, path, expires_utc, "
                f"{cols['secure']} AS secure_col, {cols['http_only']} AS httponly_col, "
                "samesite FROM cookies"
            )
            rows = connection.execute(query).fetchall()
        finally:
            connection.close()

        for row in rows:
            host_key = row["host_key"] or ""
            if "linkedin.com" not in host_key:
                continue
            blob = row["encrypted_value"] or b""
            plaintext_value = row["value"] or ""
            if (
                is_macos_or_linux
                and not plaintext_value
                and blob[:3] in (b"v10", b"v11")
                and store_version >= _HOST_KEY_PREFIX_MIN_VERSION
                and cbc_key is not None
                and not _verify_host_key_prefix(blob, cbc_key, host_key)
            ):
                skipped_wrong_key += 1
                continue
            try:
                value = _decrypt_value(
                    blob,
                    plaintext_value,
                    cbc_key=cbc_key,
                    win_master_key=win_master_key,
                    store_version=store_version,
                    is_macos_or_linux=is_macos_or_linux,
                )
            except V20EncryptedError:
                skipped_app_bound += 1
                continue
            except ValueError:
                # Wrong key / corrupt blob: PKCS7 unpad or GCM auth fails. On a
                # pre-v24 store the host-key precheck above does not run, so this
                # is the only guard. Skip the cookie instead of aborting.
                skipped_wrong_key += 1
                continue
            cookies.append(
                LinkedInCookie(
                    name=row["name"],
                    value=value,
                    domain=host_key,
                    path=row["path"] or "/",
                    expires=_expires_to_unix(row["expires_utc"] or 0),
                    secure=bool(row["secure_col"]),
                    http_only=bool(row["httponly_col"]),
                    same_site=_SAMESITE_MAP.get(row["samesite"], "Lax"),
                )
            )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    logger.info(
        "Extracted %d LinkedIn cookies from %s/%s (skipped %d app-bound, %d wrong-key)",
        len(cookies),
        profile.browser,
        profile.profile_dir_name,
        skipped_app_bound,
        skipped_wrong_key,
    )
    return cookies


@dataclass(frozen=True)
class LiAtMeta:
    """Keychain-free metadata about a profile's ``li_at`` cookie.

    Read from the plaintext SQLite columns only (no value decryption, so no OS
    keystore access and no keychain prompt). Used to filter expired/logged-out
    sessions and to rank live ones by recency *before* paying for decryption.
    """

    expires: float  # unix seconds; -1.0 for a session cookie (no expiry)
    last_access: float  # unix seconds; 0.0 if never sent
    app_bound: bool  # encrypted_value is v20 (undecryptable without OS elevation)


def read_li_at_meta(profile: BrowserProfile) -> LiAtMeta | None:
    """Return *profile*'s ``li_at`` metadata, or ``None`` when there is no ``li_at``.

    Copies the Cookies DB the same way :func:`extract_linkedin_cookies` does, but
    reads only the plaintext columns (``expires_utc``, ``last_access_utc``) plus
    the encryption prefix. It never derives a key, so it works -- and stays
    silent on the keychain -- even when the keystore is unavailable.
    """
    temp_dir, db_copy = _copy_cookies_db(profile.cookies_db)
    try:
        connection = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT host_key, name, encrypted_value, value, expires_utc, "
                "last_access_utc FROM cookies"
            ).fetchall()
        finally:
            connection.close()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    for row in rows:
        host_key = row["host_key"] or ""
        if "linkedin.com" not in host_key or row["name"] != "li_at":
            continue
        app_bound = not row["value"] and (row["encrypted_value"] or b"")[:3] == b"v20"
        return LiAtMeta(
            expires=_expires_to_unix(row["expires_utc"] or 0),
            last_access=_chromium_utc_to_unix(row["last_access_utc"] or 0),
            app_bound=app_bound,
        )
    return None


def has_undecryptable_li_at(profile: BrowserProfile) -> bool:
    """Return whether *profile* holds an ``li_at`` cookie that is app-bound (v20).

    Used to distinguish "logged in but undecryptable" from "no li_at".
    """
    meta = read_li_at_meta(profile)
    return meta is not None and meta.app_bound
