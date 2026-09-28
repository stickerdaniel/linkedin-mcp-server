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
than from the first-run download, which R1 is not about. That is the whole of
it: this is not a native test of the import command's discovery, rotation or
lease handling.

**The staged value is the session.** ``StagedSession`` holds the ``li_at`` value
in memory only; the synthetic origin is told it, so it can say per request
whether the browser sent *this* session, and the snapshot compares the file's
value against its digest. Neither ever writes the value anywhere. A refresh
would count only if the origin had issued it, and it issues none, so a
different value is a different session, not a refreshed one.

**R17 reads the artefacts and never records a cookie value.** The source
generation; the cookie file's hash and names; whether it holds a ``li_at`` with
the staged value, on ``.linkedin.com``, not expired; whether the browser
profile directory is still there; the quarantine directories; and Chromium's
``Last Version``. Together with what the user was shown and one post-quit
observation of the session in use, it derives one of five outcomes:

* ``retained``: the same generation, the staged ``li_at`` still usable on disk,
  the profile present, no new quarantine, *and* the post-quit observation saw
  the origin accept the session.
* ``cleared-by-user``: gone, and the row says the user asked for that.
* ``lost-announced``: gone, and after the last line that reported a successful
  sign-in, some line told the user the session needs signing in again
  (``announces_loss``).
* ``lost-silent``: gone, and nothing said so.
* ``uncertain``: a reading failed or was malformed, there was no session to
  lose, or the post-quit observation could not be made.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
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

#: The domain the product stores LinkedIn's cookies under
#: (``BrowserManager._normalize_cookie_domain``).
SESSION_DOMAIN = ".linkedin.com"

#: Chromium's own record of which version last wrote the profile.
LAST_VERSION_FILE = "Last Version"

#: What tells the user the session needs signing in again. English, and
#: knowingly so: the server's own messages are English, and a row that needs
#: another language has to extend these tables rather than widen them to "any
#: line", which would read every log line as an announcement.
_LOSS = re.compile(
    r"--login"
    r"|\bsign(?:ed)?[ -]?in\b"
    r"|\blog(?:ged)?[ -]?in\b"
    r"|\bre-?authenticat"
    r"|\bsession (?:expired|is invalid|invalid|was lost|is no longer)",
    re.IGNORECASE,
)

#: A report of a successful sign-in. It matches the loss table's words, and it
#: is the opposite of a loss notice, so it is judged first.
_SUCCESS = re.compile(
    r"\bsuccessfully (?:signed|logged) in\b"
    r"|\b(?:signed|logged) in successfully\b"
    r"|\bsession is valid\b"
    r"|\bimported and validated\b",
    re.IGNORECASE,
)


class StagingError(RuntimeError):
    """The synthetic session could not be established; the row cannot start."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class StagedSession:
    """The row's session. The value stays in this process's memory."""

    li_at: str = field(repr=False)

    @property
    def li_at_digest(self) -> str:
        return digest(self.li_at)


def synthetic_cookies(
    *, now: float | None = None, li_at: str | None = None
) -> list[LinkedInCookie]:
    """A fresh signed-in cookie set with random values that mean nothing."""
    expires = (time.time() if now is None else now) + 30 * 24 * 60 * 60
    token = secrets.token_urlsafe(24)
    return [
        LinkedInCookie(
            name=name,
            value=(
                li_at
                if name == "li_at" and li_at is not None
                else f"synthetic-{name}-{token}"
            ),
            domain=SESSION_DOMAIN,
            path="/",
            expires=expires,
            secure=True,
            http_only=name in ("li_at", "bscookie"),
            same_site="None",
        )
        for name in SYNTHETIC_COOKIE_NAMES
    ]


def write_synthetic_cookie_file(cookie_path: Path) -> StagedSession:
    """Stage the cookie file the way the import path stages a real one."""
    cookies = synthetic_cookies()
    payload = json.dumps([c.to_playwright() for c in cookies], indent=2)
    secure_write_text(cookie_path, payload, mode=0o600)
    (li_at,) = [c.value for c in cookies if c.name == "li_at"]
    return StagedSession(li_at)


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


async def stage_signed_in_session(
    profile: Path, *, accept: Any | None = None
) -> StagedSession:
    """Leave *profile* signed in to the synthetic origin, as an import would.

    The caller has configured the process: ``USER_DATA_DIR`` naming *profile*,
    ``PROXY_SERVER`` naming the fixture proxy, ``PLAYWRIGHT_BROWSERS_PATH``
    naming the installed browser. *accept*, when given, is called with the
    staged session before the product validates it, so the origin can judge
    the staging requests too.
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
    staged = write_synthetic_cookie_file(cookie_path)
    if accept is not None:
        accept(staged)
    if not await validate_imported_cookies(cookie_path, profile):
        raise StagingError(
            "the product's own import validation rejected the synthetic "
            "session, so the synthetic /feed/ does not pass its auth check"
        )
    write_source_state(profile)
    return staged


@dataclass(frozen=True)
class ProfileSnapshot:
    generation: str | None
    cookies_sha256: str | None
    #: Names only. No reading ever copies a cookie value.
    cookie_names: tuple[str, ...]
    #: A ``li_at`` entry of any kind is in the file.
    li_at_present: bool
    #: Some ``li_at`` carries the staged value (by digest); None without one.
    li_at_staged: bool | None
    #: Some ``li_at`` is on ``.linkedin.com``.
    li_at_on_domain: bool
    #: Some ``li_at`` has an expiry in the future (a session-only one does not).
    li_at_unexpired: bool
    #: One ``li_at`` satisfies all three at once.
    li_at_usable: bool
    #: The browser profile directory exists and is not empty.
    profile_present: bool
    quarantine: tuple[str, ...]
    last_version: str | None
    #: Every artefact that exists but could not be read or is malformed.
    unreadable: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return (
            self.generation is not None and self.li_at_usable and self.profile_present
        )

    def as_event_fields(self) -> dict[str, Any]:
        fields = asdict(self)
        for name in ("cookie_names", "quarantine", "unreadable"):
            fields[name] = list(fields[name])
        fields["usable"] = self.usable
        return fields


def _judge_li_at(
    entries: Sequence[Any], expected_digest: str | None, now: float
) -> tuple[dict[str, Any], list[str]]:
    malformed: list[str] = []
    found = staged = on_domain = unexpired = usable = False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("name") != "li_at":
            continue
        found = True
        value, domain, expires = (
            entry.get("value"),
            entry.get("domain"),
            entry.get("expires"),
        )
        if (
            not isinstance(value, str)
            or not isinstance(domain, str)
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
        ):
            malformed.append("cookies.json: a li_at entry is malformed")
            continue
        this_staged = expected_digest is not None and digest(value) == expected_digest
        this_domain = domain == SESSION_DOMAIN
        # -1 is Playwright's session-cookie sentinel: gone with the browser.
        this_unexpired = expires > now
        staged |= this_staged
        on_domain |= this_domain
        unexpired |= this_unexpired
        usable |= this_staged and this_domain and this_unexpired
    return (
        {
            "li_at_present": found,
            "li_at_staged": staged if expected_digest is not None else None,
            "li_at_on_domain": on_domain,
            "li_at_unexpired": unexpired,
            "li_at_usable": usable,
        },
        malformed,
    )


def snapshot(
    profile: Path, *, expected_digest: str | None = None, now: float | None = None
) -> ProfileSnapshot:
    """Read the R17 artefacts of *profile*. Never raises on their content."""
    unreadable: list[str] = []
    now = time.time() if now is None else now

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
    li_at, _ = _judge_li_at([], expected_digest, now)
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
            li_at, malformed = _judge_li_at(entries, expected_digest, now)
            unreadable += malformed
        except (OSError, ValueError) as exc:
            unreadable.append(f"{cookie_file.name}: {type(exc).__name__}")

    directory = canonical(profile)
    try:
        profile_present = directory.is_dir() and any(directory.iterdir())
    except OSError as exc:
        profile_present = False
        unreadable.append(f"profile: {type(exc).__name__}")

    quarantine = tuple(path.name for path in quarantine_dirs(profile))

    last_version: str | None = None
    version_file = directory / LAST_VERSION_FILE
    if version_file.exists():
        try:
            last_version = version_file.read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{LAST_VERSION_FILE}: {type(exc).__name__}")

    return ProfileSnapshot(
        generation=generation,
        cookies_sha256=cookies_sha256,
        cookie_names=cookie_names,
        profile_present=profile_present,
        quarantine=quarantine,
        last_version=last_version,
        unreadable=tuple(unreadable),
        **li_at,
    )


def announces_loss(lines: Iterable[str]) -> bool:
    """Whether the user was told of a loss after the last reported sign-in.

    Order matters: a notice that was followed by a successful sign-in has been
    answered, and a success line is never itself a notice of loss.
    """
    announced = False
    for line in lines:
        if _SUCCESS.search(line):
            announced = False
        elif _LOSS.search(line):
            announced = True
    return announced


def r17_outcome(
    before: ProfileSnapshot,
    after: ProfileSnapshot,
    user_output: Iterable[str],
    *,
    post_quit: bool | None,
    user_cleared: bool = False,
) -> str:
    """What became of the session between two readings. See the module doc.

    *post_quit* is whether a session started after the row found the origin
    accepting the staged session: True, False, or None when that observation
    could not be made.
    """
    if before.unreadable or after.unreadable or not before.usable:
        return UNCERTAIN
    kept = (
        after.usable
        and after.generation == before.generation
        and set(after.quarantine) <= set(before.quarantine)
    )
    if kept and post_quit is True:
        return RETAINED
    if kept and post_quit is None:
        return UNCERTAIN
    if user_cleared:
        return CLEARED_BY_USER
    return LOST_ANNOUNCED if announces_loss(user_output) else LOST_SILENT
