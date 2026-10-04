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
profile directory is still there; the quarantine directories, listed by this
module's own reader; and Chromium's ``Last Version``. Together with what the
row's caller was shown, the user action the row recorded as confirmed, and one
post-quit observation of the session in use, it derives one of five outcomes:

* ``retained``: the same generation, the staged ``li_at`` still usable on disk,
  the profile present, no new quarantine, *and* the post-quit observation saw
  the origin accept the session.
* ``cleared-by-user``: the row recorded the user's confirmed logout, *and* the
  after-reading shows what a logout leaves: no generation, no cookie file, no
  profile, no quarantine. Either alone is not enough: a confirmation over a
  session that is still there, or only partly gone, is no clear.
* ``lost-announced``: gone, and during the row a line told the row's own caller
  that the session needs signing in again (``announced_to_caller``).
* ``lost-silent``: gone, and nothing said so to that caller.
* ``uncertain``: a reading failed or was malformed, there was no session to
  lose, or the post-quit observation could not be made.

What a row expects, what it observed, and what the user authorized are three
separate things: the expectation is the row's declaration, the outcome is read
from the artefacts, and the authorization is only ever a recorded confirmation.

**A replacement is evidence beside the outcome, never one of them**
(``replacement_lineage``). The five outcomes read the original generation;
a session the synthetic origin issued later, found on disk, is read apart:
it counts only when an authorization the row recorded came first, a
confirmed ``--login`` or import, or the harness's own rejection of the
staged session at the origin, and the session read right after that record
was still the original. Every issued session is recorded by digest, with
when and in which phase it was issued and whether the origin then accepted
it from a request. A loss of the original before the authorization, a
session issued before it, or an issued one gone again is unauthorized,
whatever the replacement achieved: a successful sign-in afterwards never
waives an earlier loss.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from linkedin_mcp_server.browser_import.extract import LinkedInCookie
from linkedin_mcp_server.common_utils import secure_write_text
from linkedin_mcp_server.session_state import (
    QUARANTINE_PREFIX,
    canonical,
    portable_cookie_path,
    source_state_path,
    write_source_state,
)

RETAINED = "retained"
CLEARED_BY_USER = "cleared-by-user"
LOST_ANNOUNCED = "lost-announced"
LOST_SILENT = "lost-silent"
UNCERTAIN = "uncertain"
OUTCOMES = (RETAINED, CLEARED_BY_USER, LOST_ANNOUNCED, LOST_SILENT, UNCERTAIN)

#: Two expectations that are no outcome: the original generation ends lost,
#: and the lineage (``replacement_lineage``) shows an authorization the row
#: recorded before it was invalidated, with an authorized replacement in use
#: (``REPLACED_AFTER_AUTHORIZATION``) or none at all
#: (``LOST_AFTER_AUTHORIZATION``). Neither holds on the string alone.
REPLACED_AFTER_AUTHORIZATION = "replaced-after-authorization"
LOST_AFTER_AUTHORIZATION = "lost-after-authorization"

#: What a row may declare it expects. Never a bare loss: no expectation waives
#: one, and a loss the row brings about on purpose is an authorization the
#: row records, not a string it declares; the two lineage expectations hold
#: only with that record. Never ``uncertain``, which is a reading that failed.
EXPECTABLE = (
    RETAINED,
    CLEARED_BY_USER,
    REPLACED_AFTER_AUTHORIZATION,
    LOST_AFTER_AUTHORIZATION,
)

#: A user action a row records once the user confirmed it: ``--logout``.
LOGOUT = "logout"
#: A confirmed ``--login`` and a confirmed ``--import-from-browser``.
LOGIN = "login"
IMPORT = "import"
#: Not the user's: the harness's own recorded rejection of the staged session
#: at the synthetic origin (``SyntheticOrigin.reject_sessions``), which is
#: what a real expiry looks like from the product's side.
ORIGIN_REJECTED = "origin-rejected"
#: The authorizations a replacement of the session may follow.
REPLACING = (LOGIN, IMPORT, ORIGIN_REJECTED)

#: What ``replacement_lineage`` reads. ``none``: nothing replacing was
#: authorized and nothing was issued. ``replaced``: authorized first, and the
#: one issued session on disk in a new generation and accepted in use.
#: ``lost``: authorized first, and nothing issued. ``unauthorized``: a loss or
#: an issue the authorization does not cover. ``uncertain``: a reading failed
#: or the replacement's evidence is incomplete.
LINEAGE_NONE = "none"
LINEAGE_REPLACED = "replaced"
LINEAGE_LOST = "lost"
LINEAGE_UNAUTHORIZED = "unauthorized"
LINEAGE_UNCERTAIN = "uncertain"

#: When a line was shown: during the row, or in the preservation check after it.
ROW = "row"
PRESERVATION = "preservation"
#: Who it was shown to: the row's own caller. Anyone else is named by the row,
#: such as the preservation probe or a second host.
CALLER = "caller"
PROBE = "probe"

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
#: is the opposite of a loss notice, so it is judged first. The later entries
#: are the product's own: ``daemon_auth`` after a repair (``The sign-in
#: finished``, ``Signed in; ...``), a peer's session found in place
#: (``daemon_auth``, ``setup``, ``session_state``, ``error_handler``,
#: ``browser_import``), and ``core.auth`` once a manual login has its cookie.
_SUCCESS = re.compile(
    r"\bsuccessfully (?:signed|logged) in\b"
    r"|\b(?:signed|logged) in successfully\b"
    r"|\bsession is valid\b"
    r"|\bimported and validated\b"
    r"|\bthe sign-in finished\b"
    r"|\bsigned in; (?:running|not repeating)\b"
    r"|\balready signed in\b"
    r"|\blogin completed successfully\b",
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
    #: The digest of every ``li_at`` value in the file, never the value: which
    #: session is on disk, the staged one or one the origin issued.
    li_at_digests: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return (
            self.generation is not None and self.li_at_usable and self.profile_present
        )

    @property
    def cleared(self) -> bool:
        """What ``session_state.clear_auth_state`` leaves, every part read.

        No generation, no cookie file, no browser profile and no quarantine.
        An artefact that exists and does not read is ``unreadable``, which is
        never cleared, though it leaves no hash or generation behind either.
        """
        return (
            not self.unreadable
            and self.generation is None
            and self.cookies_sha256 is None
            and not self.profile_present
            and not self.quarantine
        )

    def as_event_fields(self) -> dict[str, Any]:
        fields = asdict(self)
        for name in ("cookie_names", "quarantine", "unreadable", "li_at_digests"):
            fields[name] = list(fields[name])
        fields["usable"] = self.usable
        fields["cleared"] = self.cleared
        return fields


def read_quarantine(profile: Path) -> tuple[tuple[str, ...], list[str]]:
    """The quarantine directories beside *profile*, and every listing failure.

    The product's ``quarantine_dirs`` globs, and a glob reads a directory it
    could not list as an empty one, which here would read as "nothing was
    quarantined". This reader says so instead. The same selection otherwise:
    the prefix, and a directory or a link to one. A missing auth root has
    nothing in it, which is a reading, not a failure.
    """
    root = canonical(profile).parent
    names: list[str] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.startswith(QUARANTINE_PREFIX) and entry.is_dir():
                    names.append(entry.name)
    except FileNotFoundError:
        return (), []
    except OSError as exc:
        return (), [f"quarantine: {type(exc).__name__}"]
    return tuple(sorted(names)), []


def _judge_li_at(
    entries: Sequence[Any], expected_digest: str | None, now: float
) -> tuple[dict[str, Any], list[str]]:
    malformed: list[str] = []
    digests: list[str] = []
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
        digests.append(digest(value))
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
            "li_at_digests": tuple(sorted(set(digests))),
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

    quarantine, listing = read_quarantine(profile)
    unreadable += listing

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


@dataclass(frozen=True)
class Shown:
    """One line of output, with when it was shown and to whom."""

    phase: str
    recipient: str
    line: str


def shown(phase: str, recipient: str, lines: Iterable[str]) -> list[Shown]:
    return [Shown(phase, recipient, line) for line in lines]


def announces_loss(lines: Iterable[str]) -> bool:
    """Whether some line tells the user the session needs signing in again.

    A success line is never itself a notice, and it takes no earlier notice
    back: a sign-in that repaired the session afterwards does not undo the
    user having been told it was lost.
    """
    return any(not _SUCCESS.search(line) and _LOSS.search(line) for line in lines)


def announced_to_caller(output: Iterable[Shown | str]) -> bool:
    """Whether the row's own caller was told of a loss during the row.

    Only that recipient in that phase. The preservation probe runs after the
    row on the harness's behalf, and a notice it prints says the session was
    gone by then, not that anyone told the user; another host's output reached
    someone else. A bare string is a line the caller was shown during the row.
    """
    lines = []
    for item in output:
        if isinstance(item, str):
            lines.append(item)
        elif item.phase == ROW and item.recipient == CALLER:
            lines.append(item.line)
    return announces_loss(lines)


def r17_outcome(
    before: ProfileSnapshot,
    after: ProfileSnapshot,
    user_output: Iterable[Shown | str],
    *,
    post_quit: bool | None,
    user_cleared: bool = False,
) -> str:
    """What became of the session between two readings. See the module doc.

    *post_quit* is whether a session started after the row found the origin
    accepting the staged session: True, False, or None when that observation
    could not be made. *user_cleared* is the row's record that the user
    confirmed a logout; the clear itself is read from *after*.
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
    if user_cleared and after.cleared:
        return CLEARED_BY_USER
    return LOST_ANNOUNCED if announced_to_caller(user_output) else LOST_SILENT


@dataclass(frozen=True)
class Authorization:
    """An authorization the row recorded, and the session read right after.

    *at_ns* is when the row recorded it, on the harness's monotonic clock,
    and *snapshot* was read after that: a reading that still shows the
    original generation intact proves anything that invalidated it came
    later, which is what "the authorization preceded the invalidation" needs.
    """

    kind: str
    at_ns: int
    snapshot: ProfileSnapshot


@dataclass(frozen=True)
class ReplacementLineage:
    """What became of the session beyond its original generation."""

    reading: str
    problems: tuple[str, ...] = ()
    #: The replacement's digest, generation, when and in which phase it was
    #: issued, and the first accepted request that carried it; None without
    #: one. Never a value.
    replacement: dict[str, Any] | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "reading": self.reading,
            "problems": list(self.problems),
            "replacement": dict(self.replacement) if self.replacement else None,
        }


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def replacement_lineage(
    before: ProfileSnapshot,
    after: ProfileSnapshot | None,
    authorization: Authorization | None,
    issued: Sequence[Mapping[str, Any]],
    requests: Iterable[Mapping[str, Any]],
) -> ReplacementLineage:
    """Whether a session beyond the original was authorized, and in use.

    *issued* is the origin's record of every session it issued (``digest``,
    ``issued_ns``, ``phase``); *requests* the row's origin requests with
    ``session_digests``, ``session_valid`` and ``monotonic_ns``. The original
    generation's own fate is ``r17_outcome``'s and stays beside this, never
    replaced by it.
    """
    if after is None or before.unreadable or after.unreadable or not before.usable:
        return ReplacementLineage(
            LINEAGE_UNCERTAIN, ("a reading of the session failed",)
        )
    issued_digests = [str(item.get("digest")) for item in issued]
    if authorization is None or authorization.kind not in REPLACING:
        if issued_digests:
            return ReplacementLineage(
                LINEAGE_UNAUTHORIZED,
                (
                    f"the origin issued {len(issued_digests)} session(s) and no "
                    f"authorization that covers a replacement was recorded",
                ),
            )
        return ReplacementLineage(LINEAGE_NONE)
    at = authorization.snapshot
    if at.unreadable:
        return ReplacementLineage(
            LINEAGE_UNCERTAIN, ("the reading after the authorization failed",)
        )
    problems: list[str] = []
    intact = (
        at.usable
        and at.li_at_staged is True
        and at.generation == before.generation
        and set(at.quarantine) <= set(before.quarantine)
    )
    if not intact:
        problems.append(
            "the original generation was already lost when the authorization "
            "was recorded"
        )
    early = [
        item
        for item in issued
        if (_int(item.get("issued_ns")) or 0) <= authorization.at_ns
    ]
    if early:
        problems.append(f"{len(early)} session(s) were issued before the authorization")
    gone = [value for value in issued_digests if value not in after.li_at_digests]
    if gone:
        problems.append(
            f"{len(gone)} session(s) the origin issued are no longer on disk"
        )
    unknown = set(after.li_at_digests) - set(before.li_at_digests) - set(issued_digests)
    if unknown:
        problems.append("a session on disk is neither the staged one nor issued")
    if problems:
        return ReplacementLineage(LINEAGE_UNAUTHORIZED, tuple(problems))
    if not issued_digests:
        return ReplacementLineage(LINEAGE_LOST)
    if len(issued) != 1:
        return ReplacementLineage(
            LINEAGE_UNCERTAIN,
            (f"{len(issued)} issued sessions are on disk at once",),
        )
    (item,) = issued
    value = issued_digests[0]
    issued_ns = _int(item.get("issued_ns")) or 0
    replacement: dict[str, Any] = {
        "digest": value,
        "generation": after.generation,
        "issued_ns": issued_ns,
        "phase": item.get("phase"),
        "used_ns": None,
    }
    if after.generation is None or after.generation == before.generation:
        problems.append("the replacement is on disk without a new generation")
    if not (after.li_at_on_domain and after.li_at_unexpired and after.profile_present):
        problems.append("the replacement on disk is not usable")
    for request in requests:
        seen = _int(request.get("monotonic_ns"))
        if (
            request.get("session_valid") is True
            and value in (request.get("session_digests") or ())
            and seen is not None
            and seen > issued_ns
        ):
            replacement["used_ns"] = seen
            break
    if replacement["used_ns"] is None:
        problems.append(
            "no request after the issue carried the replacement and was accepted"
        )
    if problems:
        return ReplacementLineage(LINEAGE_UNCERTAIN, tuple(problems), replacement)
    return ReplacementLineage(LINEAGE_REPLACED, (), replacement)
