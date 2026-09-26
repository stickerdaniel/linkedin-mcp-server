"""A synthetic signed-in session, and the R17 reading of what became of it.

**Staging goes through the product's own import path.** The cookie file is
written the way ``--import-from-browser`` stages one (``LinkedInCookie`` in
Playwright's shape, owner-only), validated by ``validate_imported_cookies`` —
the same launch, injection and ``/feed/`` check the import commits on — and
then committed with ``write_source_state``. So the profile the rows start from
is one the product itself accepted as signed in, against the synthetic origin,
with a ``li_at`` whose value is random and has never been anywhere near
LinkedIn. The browser install is recorded with the metadata writer the real
setup calls after it finishes, so a row starts from a finished install rather
than from the first-run download, which R1 is not about.

**R17 reads five artefacts and never a cookie value.** The source generation,
the cookie file's hash and the *names* in it, the quarantine directories beside
the profile, and Chromium's ``Last Version``. From two readings and what the
user was shown it derives one of five outcomes:

* ``retained``: the same generation, a ``li_at`` still on disk, and no new
  quarantine. A cookie file whose hash changed is still retained, because the
  close exports the store and a legitimate refresh rewrites it.
* ``cleared-by-user``: gone, and the row says the user asked for that.
* ``lost-announced``: gone, and some line the user saw names the session or the
  sign-in. Any line is not enough (``announces_session``).
* ``lost-silent``: gone, and nothing said so.
* ``uncertain``: a reading failed, or there was no session to lose.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from linkedin_mcp_server.browser_import.extract import LinkedInCookie
from linkedin_mcp_server.common_utils import secure_write_text
from linkedin_mcp_server.session_state import (
    canonical,
    portable_cookie_path,
    quarantine_dirs,
    source_state_path,
    write_source_state,
)

RETAINED = "retained"
CLEARED_BY_USER = "cleared-by-user"
LOST_ANNOUNCED = "lost-announced"
LOST_SILENT = "lost-silent"
UNCERTAIN = "uncertain"
OUTCOMES = (RETAINED, CLEARED_BY_USER, LOST_ANNOUNCED, LOST_SILENT, UNCERTAIN)

#: The ``auth_minimal`` bridge preset, which is also inside ``bridge_core``.
SYNTHETIC_COOKIE_NAMES = ("li_at", "JSESSIONID", "bcookie", "bscookie", "lidc")

#: Chromium's own record of which version last wrote the profile.
LAST_VERSION_FILE = "Last Version"

#: What counts as naming the session or the sign-in to the user. English, and
#: knowingly so: the server's own messages are English, and a row that needs
#: another language has to extend this table rather than widen it to "any
#: line", which would read every log line as an announcement.
_ANNOUNCEMENT = re.compile(
    r"--login"
    r"|\bsign(?:ed)?[ -]?in\b"
    r"|\blog(?:ged)?[ -]?in\b"
    r"|\bre-?authenticat"
    r"|\bsession (?:expired|is invalid|invalid|was lost|is no longer)",
    re.IGNORECASE,
)


class StagingError(RuntimeError):
    """The synthetic session could not be established; the row cannot start."""


def synthetic_cookies(*, now: float | None = None) -> list[LinkedInCookie]:
    """A fresh signed-in cookie set with random values that mean nothing."""
    expires = (time.time() if now is None else now) + 30 * 24 * 60 * 60
    token = secrets.token_urlsafe(24)
    return [
        LinkedInCookie(
            name=name,
            value=f"synthetic-{name}-{token}",
            domain=".linkedin.com",
            path="/",
            expires=expires,
            secure=True,
            http_only=name in ("li_at", "bscookie"),
            same_site="None",
        )
        for name in SYNTHETIC_COOKIE_NAMES
    ]


def write_synthetic_cookie_file(cookie_path: Path) -> None:
    """Stage the cookie file the way the import path stages a real one."""
    payload = json.dumps([c.to_playwright() for c in synthetic_cookies()], indent=2)
    secure_write_text(cookie_path, payload, mode=0o600)


def stage_installed_browser() -> Path:
    """Record the finished install the real setup would have left behind.

    Refuses when the browser is not actually in the cache: the metadata says
    the install is complete, and writing it over a missing browser would send
    every row into a launch failure that reads like a product defect.
    """
    from linkedin_mcp_server import bootstrap

    browsers = bootstrap.configure_browser_environment()
    targets = bootstrap._patchright_install_targets() or {}
    revision = targets.get(bootstrap._FULL_DIR_PREFIX)
    if revision is None or not bootstrap._has_install_for(
        browsers, bootstrap._FULL_DIR_PREFIX, revision
    ):
        raise StagingError(
            f"no installed browser at {browsers} for revision {revision}; the "
            f"CI step installs one before the rows run"
        )
    # The arguments ``_run_browser_setup`` passes after a completed install.
    bootstrap._write_install_metadata(
        browsers,
        {bootstrap._SHELL_DIR_PREFIX: False, bootstrap._FULL_DIR_PREFIX: True},
    )
    if not bootstrap.browser_ready():
        raise StagingError("the recorded install does not read as ready")
    return browsers


async def stage_signed_in_session(profile: Path) -> None:
    """Leave *profile* signed in to the synthetic origin, as an import would.

    The caller has configured the process: ``USER_DATA_DIR`` naming *profile*,
    ``PROXY_SERVER`` naming the fixture proxy, ``PLAYWRIGHT_BROWSERS_PATH``
    naming the installed browser.
    """
    from linkedin_mcp_server.drivers.browser import (
        get_profile_dir,
        validate_imported_cookies,
    )

    if canonical(get_profile_dir()) != canonical(profile):
        raise StagingError(
            f"the process is configured for {get_profile_dir()}, not {profile}; "
            f"staging would write one profile and the actors would read another"
        )
    stage_installed_browser()
    cookie_path = portable_cookie_path(profile)
    write_synthetic_cookie_file(cookie_path)
    if not await validate_imported_cookies(cookie_path, profile):
        raise StagingError(
            "the product's own import validation rejected the synthetic "
            "session, so the synthetic /feed/ does not pass its auth check"
        )
    write_source_state(profile)


@dataclass(frozen=True)
class ProfileSnapshot:
    generation: str | None
    cookies_sha256: str | None
    #: Names only. The values are synthetic, but a reading that never copies a
    #: cookie value is one that stays safe if it is ever pointed elsewhere.
    cookie_names: tuple[str, ...]
    quarantine: tuple[str, ...]
    last_version: str | None
    #: Every artefact that exists but could not be read, and why.
    unreadable: tuple[str, ...] = ()

    @property
    def has_session(self) -> bool:
        return self.generation is not None and "li_at" in self.cookie_names

    def as_event_fields(self) -> dict[str, Any]:
        fields = asdict(self)
        for name in ("cookie_names", "quarantine", "unreadable"):
            fields[name] = list(fields[name])
        return fields


def snapshot(profile: Path) -> ProfileSnapshot:
    """Read the four R17 artefacts of *profile*. Never raises on their content."""
    unreadable: list[str] = []

    generation: str | None = None
    state_file = source_state_path(profile)
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            value = data.get("login_generation") if isinstance(data, dict) else None
            if not isinstance(value, str) or not value:
                unreadable.append(f"{state_file.name}: no login_generation")
            else:
                generation = value
        except (OSError, ValueError) as exc:
            unreadable.append(f"{state_file.name}: {type(exc).__name__}")

    cookies_sha256: str | None = None
    cookie_names: tuple[str, ...] = ()
    cookie_file = portable_cookie_path(profile)
    if cookie_file.exists():
        try:
            raw = cookie_file.read_bytes()
            cookies_sha256 = hashlib.sha256(raw).hexdigest()
            entries = json.loads(raw)
            if not isinstance(entries, list):
                raise ValueError("not a list")
            cookie_names = tuple(
                sorted(
                    {
                        entry["name"]
                        for entry in entries
                        if isinstance(entry, dict)
                        and isinstance(entry.get("name"), str)
                    }
                )
            )
        except (OSError, ValueError) as exc:
            unreadable.append(f"{cookie_file.name}: {type(exc).__name__}")

    quarantine = tuple(path.name for path in quarantine_dirs(profile))

    last_version: str | None = None
    version_file = canonical(profile) / LAST_VERSION_FILE
    if version_file.exists():
        try:
            last_version = version_file.read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{LAST_VERSION_FILE}: {type(exc).__name__}")

    return ProfileSnapshot(
        generation=generation,
        cookies_sha256=cookies_sha256,
        cookie_names=cookie_names,
        quarantine=quarantine,
        last_version=last_version,
        unreadable=tuple(unreadable),
    )


def announces_session(lines: Iterable[str]) -> bool:
    return any(_ANNOUNCEMENT.search(line) for line in lines)


def r17_outcome(
    before: ProfileSnapshot,
    after: ProfileSnapshot,
    user_output: Iterable[str],
    *,
    user_cleared: bool = False,
) -> str:
    """What became of the session between two readings. See the module doc."""
    if before.unreadable or after.unreadable or not before.has_session:
        return UNCERTAIN
    kept = (
        after.has_session
        and after.generation == before.generation
        and set(after.quarantine) <= set(before.quarantine)
    )
    if kept:
        return RETAINED
    if user_cleared:
        return CLEARED_BY_USER
    return LOST_ANNOUNCED if announces_session(user_output) else LOST_SILENT
