"""A marker proving which auth root this server is allowed to destroy.

``USER_DATA_DIR`` accepts any path, and six operations in ``session_state``
move or recursively delete what it names. Three of them reach the *parent*
directory as well, because the portable cookies, the source metadata and the
derived runtime profiles are siblings of the profile rather than children of
it. So a mistyped path does not merely fail to find a session: it takes a
directory nobody meant to name, and the emptiness of the profile itself proves
nothing about what is next to it.

The rule is a whitelist of one, widened by an explicit marker. The canonical
default is always ours. Any other root is ours only once
``profile-claim.json`` sits in it saying so, and that file is written only when
the root is demonstrably free, inherited from a session already living there,
or claimed by hand.

A blacklist of dangerous shapes was the alternative and loses by construction:
it has to enumerate ``/``, ``$HOME``, every mount point and every path that
happens to hold somebody's documents, and the one it misses is the one that
matters.

The marker deliberately sits outside ``_auth_state_targets`` and outside the
``invalid-state-*`` glob. Rotation would otherwise carry it into quarantine and
logout would erase it, in both cases at the exact moment the next operation
needs it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from linkedin_mcp_server.common_utils import secure_mkdir, secure_write_text
from linkedin_mcp_server.config.schema import DEFAULT_USER_DATA_DIR
from linkedin_mcp_server.exceptions import ProfileRootRefusedError

logger = logging.getLogger(__name__)

#: Written into the auth root, beside ``cookies.json`` rather than inside the
#: profile: the profile is the thing that gets moved away, and a marker that
#: travels with it cannot answer whether the move was allowed.
CLAIM_FILE = "profile-claim.json"

CLAIM_VERSION = 1

#: Excludes other *claimers*, and nothing else. Kept apart from the browser
#: lease on purpose; see `_the_root_to_ourselves`.
CLAIM_LOCK_FILE = "profile-claim.lock"

#: How long to wait for another *claimer* before giving up. Short on purpose:
#: the work it guards is a read and a small write, so a peer holding it for more
#: than a moment is a peer that is stuck.
_CLAIM_WAIT_SECONDS = 5.0
_CLAIM_POLL_SECONDS = 0.02

#: Entries that say nothing about whether a root already belongs to somebody,
#: because *this* server wrote every one of them.
#:
#: Not a nicety. Several land in the auth root before the claim is ever
#: evaluated, on the very run meant to establish it: ``configure_logging`` runs
#: first in ``main()`` and creates ``trace-runs`` there, because trace capture
#: defaults to ``on_error`` rather than off, and the profile lease writes its
#: lock the moment anything asks about the root. Measured against the real entry
#: point with a genuinely empty custom directory: the claim was refused, and the
#: message told the user to point at an empty directory, which is what they had
#: done.
#:
#: Names rather than an import, because ``bootstrap`` reaches back into this
#: module's callers and the cycle is not worth closing for four strings.
#: ``tests/test_profile_claim.py`` pins each one against the module that
#: actually writes it, so a rename cannot silently reintroduce the refusal.
_IGNORED_WHEN_JUDGING_EMPTINESS = frozenset(
    {
        CLAIM_FILE,
        CLAIM_LOCK_FILE,
        # profile_lease._LEASE_FILE, _HANDOFF_FILE
        "profile.lock",
        "profile.handoff",
        # debug_trace._trace_root
        "trace-runs",
        # error_diagnostics.issue_report_dir
        "issue-reports",
        # bootstrap._BROWSER_DIR, _BROWSER_INSTALL_METADATA
        "patchright-browsers",
        "browser-install.json",
    }
)


def default_profile_dir() -> Path:
    """The one profile root that needs no marker.

    Resolved on every call rather than at import: the published image and the
    host expand ``~`` differently, and a test that redirects the account home
    would otherwise get the value from whenever this module happened to load.
    """
    return Path(DEFAULT_USER_DATA_DIR).expanduser().resolve()


def auth_root(source_profile_dir: Path) -> Path:
    """The directory a claim covers: the profile's parent, canonically."""
    from linkedin_mcp_server.session_state import auth_root_dir

    return auth_root_dir(source_profile_dir)


def claim_path(source_profile_dir: Path) -> Path:
    """Where the marker for *source_profile_dir*'s auth root lives."""
    return auth_root(source_profile_dir) / CLAIM_FILE


def is_the_default_root(source_profile_dir: Path) -> bool:
    """Whether *source_profile_dir* is the canonical default."""
    return source_profile_dir.expanduser().resolve() == default_profile_dir()


def require_profile_claim(source_profile_dir: Path) -> Path:
    """Return the resolved profile root, or refuse to let anything touch it.

    Every destructive operation calls this on the *source* root it was given,
    before it looks at what exists. Checking afterwards would be too late for
    the short-circuits: rotation returns early when it finds no artifacts, and a
    foreign directory reaches that return without ever being judged.

    Read without any lock, deliberately. The only writer is
    :func:`ensure_profile_claim`, which runs once at startup and never removes a
    marker, so the two ways this read can be stale are seeing a marker a moment
    before it was written, which refuses, and seeing one a moment after, which
    is the truth. Neither grants access that was not given.
    """
    profile_dir = source_profile_dir.expanduser().resolve()
    if is_the_default_root(profile_dir):
        return profile_dir

    claimed = _claimed_path(claim_path(profile_dir))
    if claimed == profile_dir:
        return profile_dir

    raise ProfileRootRefusedError(_refusal_message(profile_dir, claimed))


def ensure_profile_claim(
    source_profile_dir: Path, *, claim_anyway: bool = False
) -> Path:
    """Record that this auth root belongs to us, or refuse it.

    Called once per process, early, against the *configured* source root and
    never against a derived one. Derived runtime profiles live at
    ``<auth-root>/runtime-profiles/<runtime>/profile``, so their own auth root
    is ``<auth-root>/runtime-profiles/<runtime>`` — a nested root that
    ``clear_runtime_profile`` deletes wholesale. A marker written there would be
    destroyed by ordinary operation and would have claimed the wrong directory
    while looking like protection.

    *claim_anyway* is the operator saying it out loud: it takes a root that is
    neither empty nor carrying a session of ours. It exists for two cases that
    cannot be inferred, a historical Docker mount whose container path matches
    neither the container default nor the recorded host path, and a root that
    already holds the downloaded browser but no session yet.

    Only the write contends, and only against another claimer. A root that is
    already settled answers from the marker alone and takes no lock at all,
    which is what keeps this usable at startup: after the first run there is
    nothing left to decide, and every later run must start while other processes
    are busy.
    """
    profile_dir = source_profile_dir.expanduser().resolve()
    if profile_dir.name == CLAIM_FILE:
        raise ProfileRootRefusedError(
            f"{profile_dir} cannot be used as a browser profile: the name "
            f"{CLAIM_FILE} is reserved for this server's ownership marker."
        )

    if _looks_derived(profile_dir):
        raise ProfileRootRefusedError(
            f"{profile_dir} is a derived runtime profile, not a source profile. "
            "Claiming it would mark a directory that ordinary operation deletes, "
            "and would leave the real auth root unprotected."
        )

    if is_the_default_root(profile_dir):
        # Accepted, and nothing is written. Writing here looks harmless and is
        # not: the container default is /home/pwuser/.linkedin-mcp/profile, and
        # an operator who mounts a *custom* host root there would have the
        # container overwrite the marker with the container's own path, after
        # which the host run reads its own root as somebody else's and is
        # refused. Since `require_profile_claim` recognises the default without
        # consulting a marker, there is nothing the write would buy.
        return profile_dir

    if _settled(profile_dir):
        return profile_dir

    with _the_root_to_ourselves(profile_dir):
        # Re-read inside the lock. Between the check above and here another
        # process may have claimed this very root, and acting on the earlier
        # answer is the race this exists to close.
        if _settled(profile_dir):
            return profile_dir

        # The operator's explicit word comes first, ahead of every reason to
        # refuse. Behind the marker check it was unreachable exactly where it is
        # needed: a root claimed under one path and later reached under another
        # is the historical Docker mount this flag exists for, and the refusal
        # message ends by recommending the flag. Advice that cannot work is
        # worse than none.
        if claim_anyway:
            _write_claim(profile_dir)
            return profile_dir

        marker = claim_path(profile_dir)
        claimed = _claimed_path(marker)
        if claimed is not None:
            raise ProfileRootRefusedError(_refusal_message(profile_dir, claimed))
        if marker.exists():
            raise ProfileRootRefusedError(
                f"{marker} could not be read as an ownership marker. Delete it "
                "if it is debris, point USER_DATA_DIR at the profile it was "
                "written for, or start once with --claim-profile-root."
            )

        if _inherits_a_session(profile_dir) or _is_free(profile_dir):
            _write_claim(profile_dir)
            return profile_dir

    raise ProfileRootRefusedError(_unclaimable_message(profile_dir))


def _settled(profile_dir: Path) -> bool:
    """Whether the marker on disk already says this exact root is ours."""
    return _claimed_path(claim_path(profile_dir)) == profile_dir


@contextlib.contextmanager
def _the_root_to_ourselves(profile_dir: Path) -> Iterator[None]:
    """Serialise the decision and the write against other *claimers*.

    Its own lock file rather than the browser lease, and that distinction is the
    whole of it. The lease is held for as long as a Chromium is open, so a claim
    that reached for it made starting the server depend on nobody else browsing:
    measured, a second process started while another momentarily held the
    profile sat out the whole budget and then refused, which is a startup
    failure invented out of two unrelated resources sharing one lock. What needs
    excluding here is another process deciding the same question about the same
    directory, and that takes microseconds.

    Nothing this holds conflicts with rotation either: the marker is not among
    the artifacts rotation moves, so a rotating peer and a claiming peer do not
    touch the same files.
    """
    from linkedin_mcp_server.profile_lease import acquire_locked_fd, _release_locked_fd

    lock_path = auth_root(profile_dir) / CLAIM_LOCK_FILE
    secure_mkdir(lock_path.parent)
    deadline = time.monotonic() + _CLAIM_WAIT_SECONDS
    while True:
        fd = acquire_locked_fd(lock_path, exclusive=True)
        if fd is not None:
            break
        if time.monotonic() >= deadline:
            raise ProfileRootRefusedError(
                f"Another process is deciding who owns {lock_path.parent} and "
                "this profile has no ownership marker yet. Try again in a "
                "moment."
            )
        time.sleep(_CLAIM_POLL_SECONDS)
    try:
        yield
    finally:
        _release_locked_fd(fd)


def _looks_derived(profile_dir: Path) -> bool:
    """Whether this path has the shape of a runtime profile derived from a root.

    ``runtime_profile_dir`` builds ``<auth-root>/runtime-profiles/<id>/profile``,
    so a claim taken there would write into ``<auth-root>/runtime-profiles/<id>``
    — a nested auth root that ``clear_runtime_profile`` removes wholesale. The
    marker would vanish on the next bridge, and while it existed it would have
    protected a directory the server destroys on purpose rather than the one
    holding the session.

    Checked by shape rather than by comparing against the configured root, which
    is what makes it useful: the mistake this catches is a component passing the
    path it happens to hold instead of the source root, and such a component has
    no idea the two differ.
    """
    from linkedin_mcp_server.session_state import _RUNTIME_PROFILES_DIR

    parents = profile_dir.parents
    return (
        profile_dir.name == "profile"
        and len(parents) >= 2
        and parents[1].name == _RUNTIME_PROFILES_DIR
    )


def _claimed_path(marker: Path) -> Path | None:
    """The profile path a marker names, or None when there is nothing to trust."""
    data = _read_json(marker)
    if data is None:
        return None
    if data.get("version") != CLAIM_VERSION:
        logger.warning(
            "Ignoring %s: unknown claim version %r", marker, data.get("version")
        )
        return None
    recorded = data.get("profile_path")
    if not isinstance(recorded, str) or not recorded:
        logger.warning("Ignoring %s: it names no profile path", marker)
        return None
    try:
        return Path(recorded).expanduser().resolve()
    except (OSError, ValueError):
        # A hand-edited marker can hold something no filesystem will parse, an
        # embedded NUL being the usual one. Unreadable is the same answer as
        # unreadable JSON: refuse, rather than crash.
        logger.warning("Ignoring %s: %r is not a usable path", marker, recorded)
        return None


def _inherits_a_session(profile_dir: Path) -> bool:
    """Whether a session already living here says this root was always ours.

    ``write_source_state`` has stored the resolved profile path since it was
    introduced, so an installation that has been working for months against a
    custom root carries its own proof and migrates without being asked.
    """
    from linkedin_mcp_server.session_state import load_source_state

    state = load_source_state(profile_dir)
    if state is None:
        return False
    try:
        recorded = Path(state.profile_path).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return recorded == profile_dir


def _is_free(profile_dir: Path) -> bool:
    """Whether the whole auth root is missing or holds nothing of anyone's.

    The *root* rather than the profile, which is the entire point: an empty
    ``profile`` directory beside somebody's ``cookies.json`` says only that the
    profile is empty, and rotation and logout would take the sibling anyway.
    """
    from linkedin_mcp_server.session_state import auth_root_dir

    root = auth_root_dir(profile_dir)
    if not root.is_dir():
        return True
    try:
        return not any(
            item.name not in _IGNORED_WHEN_JUDGING_EMPTINESS for item in root.iterdir()
        )
    except OSError as exc:
        # Unreadable is not empty. Refusing costs a message; guessing costs
        # whatever is in there.
        logger.warning("Could not read %s: %s", root, exc)
        return False


def _write_claim(profile_dir: Path) -> None:
    marker = claim_path(profile_dir)
    secure_mkdir(marker.parent)
    secure_write_text(
        marker,
        json.dumps(
            {"version": CLAIM_VERSION, "profile_path": str(profile_dir)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    logger.debug("Claimed the auth root at %s", marker.parent)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable ownership marker: %s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("Ignoring malformed ownership marker: %s", path)
        return None
    return data


def _refusal_message(profile_dir: Path, claimed: Path | None) -> str:
    owner = f"It belongs to {claimed}." if claimed is not None else "It is unclaimed."
    return (
        f"Refusing to move or delete anything under {profile_dir.parent}. "
        f"{owner} Point USER_DATA_DIR back at that profile, or start once with "
        "--claim-profile-root to take this one over."
    )


def _unclaimable_message(profile_dir: Path) -> str:
    return (
        f"Refusing to use {profile_dir} as a browser profile: "
        f"{profile_dir.parent} already holds files that are not ours, and this "
        "server moves and deletes that whole directory when it rotates or "
        "clears a session. Point USER_DATA_DIR at an empty directory, or start "
        "once with --claim-profile-root if this really is the right place."
    )
