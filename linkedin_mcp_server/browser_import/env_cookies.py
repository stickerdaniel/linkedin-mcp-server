"""Read a LinkedIn session handed over as cookies through the environment.

The local browser import (``discovery`` + ``extract``) only works where the
browser that holds the session runs on the same machine. A server hosted in a
cloud MCP gateway (Obot, a Kubernetes pod, a CI runner) has neither that
browser nor a person who can complete an interactive login, so the session has
to arrive from outside. This module turns that outside input into the same
:class:`LinkedInCookie` list the browser import produces, so the rest of the
pipeline (stage -> validate against ``/feed/`` -> persist) is shared.

Two variables are read, the file one first:

``LINKEDIN_COOKIES_FILE``
    Path to a file holding any of the formats below. Suited to secret mounts.

``LINKEDIN_COOKIES``
    The cookie text itself. Accepted formats:

    * a JSON array exported by a cookie extension (Cookie-Editor,
      EditThisCookie) or in Playwright's ``add_cookies`` shape;
    * a Playwright ``storage_state`` object (``{"cookies": [...]}``);
    * a ``Cookie:`` header string, ``li_at=...; JSESSIONID="ajax:..."; ...``;
    * a bare ``li_at`` value;
    * any of the above base64-encoded behind a ``base64:`` prefix, for
      environment editors that mangle quotes or newlines.

Cookie values are never logged and never placed in exception messages.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

from linkedin_mcp_server.browser_import.extract import LinkedInCookie
from linkedin_mcp_server.exceptions import InvalidCookieInputError

logger = logging.getLogger(__name__)

COOKIES_ENV = "LINKEDIN_COOKIES"
COOKIES_FILE_ENV = "LINKEDIN_COOKIES_FILE"

_DEFAULT_DOMAIN = ".linkedin.com"
_BASE64_PREFIX = "base64:"

# Browser-extension spellings of SameSite mapped to Playwright's three values.
# "unspecified" is what Chrome reports when the site set no attribute, and
# Chrome then treats the cookie as Lax.
_SAME_SITE = {
    "none": "None",
    "no_restriction": "None",
    "lax": "Lax",
    "unspecified": "Lax",
    "strict": "Strict",
}


def env_cookie_input_configured() -> bool:
    """Whether either cookie variable is set to something non-blank."""
    return bool(
        (os.environ.get(COOKIES_FILE_ENV) or "").strip()
        or (os.environ.get(COOKIES_ENV) or "").strip()
    )


def load_env_cookies() -> list[LinkedInCookie] | None:
    """Return the cookies from the environment, or ``None`` when none are set.

    Raises :class:`InvalidCookieInputError` when a variable is set but its
    content cannot produce a usable, unexpired ``li_at``.
    """
    file_value = (os.environ.get(COOKIES_FILE_ENV) or "").strip()
    if file_value:
        path = Path(file_value).expanduser()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InvalidCookieInputError(
                f"{COOKIES_FILE_ENV} points to {path}, which could not be read: "
                f"{exc.strerror or exc.__class__.__name__}."
            ) from None
        return parse_cookie_input(raw, source=COOKIES_FILE_ENV)

    env_value = (os.environ.get(COOKIES_ENV) or "").strip()
    if env_value:
        return parse_cookie_input(env_value, source=COOKIES_ENV)
    return None


def parse_cookie_input(raw: str, *, source: str = COOKIES_ENV) -> list[LinkedInCookie]:
    """Parse any accepted cookie format into LinkedIn cookies.

    The result always contains exactly one ``li_at``, which is live. Cookies for
    other sites are dropped. Later duplicates of a name+domain replace earlier
    ones, matching how a browser would store them.
    """
    text = raw.strip().lstrip("﻿")
    if not text:
        raise InvalidCookieInputError(f"{source} is empty.")

    if text[: len(_BASE64_PREFIX)].lower() == _BASE64_PREFIX:
        text = _decode_base64(text[len(_BASE64_PREFIX) :], source)

    if text[0] in "[{":
        cookies = _parse_json(text, source)
    elif "=" in text:
        cookies = _parse_cookie_header(text)
    elif any(ch.isspace() for ch in text):
        raise InvalidCookieInputError(
            f"{source} is not JSON, a Cookie header, or a single li_at value."
        )
    else:
        cookies = [_make_cookie("li_at", text)]

    return _finalize(cookies, source)


def cookie_fingerprint(cookies: list[LinkedInCookie]) -> str:
    """A stable, non-reversible id for the session the cookies carry.

    Keyed on ``li_at`` alone: the other cookies rotate on every page load while
    ``li_at`` identifies the login, so this changes exactly when the user hands
    over a different session.
    """
    li_at = next(c.value for c in cookies if c.name == "li_at")
    return hashlib.sha256(li_at.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Format parsers
# ---------------------------------------------------------------------------


def _decode_base64(payload: str, source: str) -> str:
    compact = "".join(payload.split())
    compact += "=" * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(compact, altchars=b"-_", validate=False)
        text = decoded.decode("utf-8").strip().lstrip("﻿")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise InvalidCookieInputError(
            f"{source} starts with '{_BASE64_PREFIX}' but is not valid base64 text."
        ) from None
    if not text:
        raise InvalidCookieInputError(f"{source} decodes to empty text.")
    return text


def _parse_json(text: str, source: str) -> list[LinkedInCookie]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidCookieInputError(
            f"{source} looks like JSON but does not parse "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from None

    if isinstance(data, Mapping):
        # Playwright storage_state, or a single cookie object.
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        elif "name" in data and "value" in data:
            data = [data]
        else:
            raise InvalidCookieInputError(
                f"{source} is a JSON object without a 'cookies' list."
            )
    if not isinstance(data, list):
        raise InvalidCookieInputError(f"{source} must be a JSON array of cookies.")

    cookies: list[LinkedInCookie] = []
    for index, item in enumerate(data):
        if not isinstance(item, Mapping):
            raise InvalidCookieInputError(
                f"{source}: entry {index} is not a cookie object."
            )
        cookie = _cookie_from_mapping(item)
        if cookie is not None:
            cookies.append(cookie)
    return cookies


def _cookie_from_mapping(item: Mapping[str, Any]) -> LinkedInCookie | None:
    name = item.get("name")
    value = item.get("value")
    if not isinstance(name, str) or not name or not isinstance(value, str):
        return None

    domain = item.get("domain") or item.get("host") or _DEFAULT_DOMAIN
    if not isinstance(domain, str) or not _is_linkedin_domain(domain):
        return None

    path = item.get("path") or "/"
    if not isinstance(path, str):
        path = "/"

    if item.get("session") is True:
        expires = -1.0
    else:
        expires = _as_expiry(
            item.get("expires", item.get("expirationDate", item.get("expiry")))
        )

    secure = bool(item.get("secure", True))
    http_only = bool(item.get("httpOnly", item.get("http_only", False)))
    same_site = _SAME_SITE.get(
        str(item.get("sameSite", item.get("same_site", "")) or "").strip().lower(),
        "Lax",
    )
    return _make_cookie(
        name,
        value,
        domain=domain,
        path=path,
        expires=expires,
        secure=secure,
        http_only=http_only,
        same_site=same_site,
    )


def _parse_cookie_header(text: str) -> list[LinkedInCookie]:
    if text[:7].lower() == "cookie:":
        text = text[7:]
    cookies: list[LinkedInCookie] = []
    for part in text.replace("\n", ";").split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        value = value.strip()
        # LinkedIn's JSESSIONID is stored quoted ("ajax:123"); keep the quotes,
        # the browser sends them back verbatim and the CSRF token includes them.
        cookies.append(_make_cookie(name, value))
    return cookies


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cookie(
    name: str,
    value: str,
    *,
    domain: str = _DEFAULT_DOMAIN,
    path: str = "/",
    expires: float = -1.0,
    secure: bool = True,
    http_only: bool = False,
    same_site: str = "Lax",
) -> LinkedInCookie:
    if name == "li_at":
        # LinkedIn sets it HttpOnly + SameSite=None; matching that keeps the
        # injected cookie indistinguishable from the one a login would leave.
        http_only = True
        same_site = "None"
        secure = True
    if same_site == "None" and not secure:
        # Chromium rejects SameSite=None without Secure.
        same_site = "Lax"
    if not domain.startswith("."):
        domain = "." + domain
    return LinkedInCookie(
        name=name,
        value=value,
        domain=domain,
        path=path,
        expires=expires,
        secure=secure,
        http_only=http_only,
        same_site=same_site,
    )


def _is_linkedin_domain(domain: str) -> bool:
    host = domain.strip().lstrip(".").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _as_expiry(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return -1.0
    try:
        value = float(raw)
    except ValueError:
        return -1.0
    if value != value or value in (float("inf"), float("-inf")):
        return -1.0
    if value <= 0:
        return -1.0
    # Some exporters write milliseconds.
    if value > 1e11:
        value /= 1000.0
    return value


def _finalize(cookies: list[LinkedInCookie], source: str) -> list[LinkedInCookie]:
    now = time.time()
    by_key: dict[tuple[str, str], LinkedInCookie] = {}
    expired_li_at = False
    for cookie in cookies:
        if cookie.expires != -1.0 and cookie.expires <= now:
            expired_li_at = expired_li_at or cookie.name == "li_at"
            continue
        if cookie.name == "li_at" and not cookie.value.strip():
            continue
        by_key[(cookie.name, cookie.domain.lstrip(".").lower())] = cookie

    result = list(by_key.values())
    li_ats = [c for c in result if c.name == "li_at"]
    if not li_ats:
        if expired_li_at:
            raise InvalidCookieInputError(
                f"The li_at cookie in {source} has expired. Sign in to LinkedIn "
                "in your browser again and export fresh cookies."
            )
        raise InvalidCookieInputError(
            f"{source} has no li_at cookie, which is the one that carries the "
            "LinkedIn login. Export the cookies of a signed-in linkedin.com tab."
        )
    if len(li_ats) > 1:
        # One login per server; keep the cookie on the registrable domain, which
        # is where LinkedIn sets it.
        keep = next(
            (c for c in li_ats if c.domain.lstrip(".").lower() == "linkedin.com"),
            li_ats[0],
        )
        result = [c for c in result if c.name != "li_at" or c is keep]

    logger.info(
        "Read %d LinkedIn cookie(s) from %s: %s",
        len(result),
        source,
        ", ".join(sorted({c.name for c in result})),
    )
    return result
