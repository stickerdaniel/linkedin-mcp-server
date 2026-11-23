"""The synthetic origin for a run by hand.

A loopback www.linkedin.com, one profile signed in to it, and holds and
sign-ins on command, for a protocol run on a disposable machine such as
``docs/daemon-gate-2-windows.md``. The session is deterministic and needs no
live account, and no request reaches LinkedIn.

Run it with the candidate's interpreter, once ``synthetic_origin.py CERTS``
has issued the certificates and the machine trusts ``CERTS/ca.pem`` and maps
the names to loopback::

    python manual_origin.py --certs CERTS [--port 8899]

It refuses while a name resolves anywhere but loopback, and unless the
configured profile is new: no source state, no cookie file, and an empty
profile directory. Then it starts the origin and a proxy on
``127.0.0.1:PORT``, installs the browser, stages a synthetic session through
the product's own import validation, and reads commands until ``quit``. Every
host and command of the run sets ``PROXY_SERVER=http://127.0.0.1:PORT``.

Every page needs a session the origin accepts (``require_sessions``), so a
browser that lost its session meets the login wall instead of a feed. The
origin keeps those sessions in memory only, so the run lasts as long as this
process.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    # In place of this script's own directory, whose ``session`` would shadow
    # any top-level module of that name. ``linkedin_mcp_server`` then
    # resolves to the candidate installed in this interpreter.
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from differential.session import snapshot, stage_signed_in_session  # noqa: E402
from differential.synthetic_origin import (  # noqa: E402
    ALLOWED_HOSTS,
    GATE_DEADLINE_SECONDS,
    ISSUED,
    PERSON_PAGES,
    EgressProxy,
    ProxyDecision,
    SyntheticOrigin,
    fence_breaches,
    person_path,
)

DEFAULT_PORT = 8899

COMMANDS = f"""\
hold USERNAME [SECONDS]  hold each page of USERNAME's profile, up to SECONDS each (default and most {GATE_DEADLINE_SECONDS:g})
signin                   complete every login page from now on with a fresh session
check                    which session the profile holds, and whether the origin accepts it
quit                     release every hold and stop"""


def say(line: str) -> None:
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"{stamp} {line}", flush=True)


class _Origin(SyntheticOrigin):
    def complete_login(self, header: str | None) -> tuple[str, str | None]:
        answer, cookie = super().complete_login(header)
        if answer == ISSUED:
            say("signin: issued a fresh session to a login page")
        return answer, cookie


class _Proxy(EgressProxy):
    def record(self, decision: ProxyDecision) -> None:
        super().record(decision)
        if not decision.forwarded:
            say(f"proxy: refused {decision.method} {decision.target}")


def report_gate(kind: str, **fields: Any) -> None:
    """Print a hold's start and how it ended: ``peer-gone`` is the browser
    dropping the held request, ``deadline`` the page answered once the hold
    ran out, ``served`` the page answered at ``quit``."""
    if kind == "gate.entered":
        say(f"hold: {fields['path']} is held")
        return
    entered, released = fields["entered_monotonic_ns"], fields["released_monotonic_ns"]
    held = (
        ""
        if None in (entered, released)
        else f" after {(released - entered) / 1e9:.1f}s"
    )
    say(f"hold: {fields['path']} ended {fields['terminal']}{held}")


def describe_session(
    profile: Path, staged_digest: str, accepts: Callable[[str], bool]
) -> str:
    """One line on the session *profile* holds: whether its ``li_at`` is the
    staged one, whether the origin accepts it, its generation and any
    quarantine. Never a cookie value."""
    found = snapshot(profile, expected_digest=staged_digest)
    if found.unreadable:
        return "unreadable: " + "; ".join(found.unreadable)
    if found.li_at_digests:
        which = " and ".join(
            "staged" if digest == staged_digest else "other"
            for digest in found.li_at_digests
        )
        verdict = (
            "accepted"
            if any(accepts(digest) for digest in found.li_at_digests)
            else "refused"
        )
        session = f"{which} li_at, {verdict} by the origin"
    else:
        session = "no li_at"
    quarantine = ", ".join(found.quarantine) or "no quarantine"
    return f"{session}; generation {found.generation or 'none'}; {quarantine}"


def stage(origin: SyntheticOrigin) -> tuple[Path, str]:
    """Sign the configured profile in to *origin*; its path and the staged
    session's digest."""
    from linkedin_mcp_server.bootstrap import ensure_browser_installed
    from linkedin_mcp_server.drivers.browser import get_profile_dir
    from linkedin_mcp_server.session_state import (
        portable_cookie_path,
        source_state_path,
    )

    profile = get_profile_dir()
    # Staging overwrites the cookie file and, after a stalled browser, resets
    # the profile directory, so it starts only where neither holds anything.
    found = [
        str(path)
        for path in (source_state_path(profile), portable_cookie_path(profile))
        if path.exists()
    ]
    if profile.is_dir() and any(profile.iterdir()):
        found.append(str(profile))
    if found:
        raise SystemExit(
            f"Refused: {', '.join(found)} already exist. Staging needs a new "
            f"profile; run --logout, then start again."
        )
    ensure_browser_installed()
    staged = asyncio.run(
        stage_signed_in_session(
            profile, accept=lambda session: origin.accept_session(session.li_at)
        )
    )
    return profile, staged.li_at_digest


def command(
    words: list[str], origin: SyntheticOrigin, profile: Path, staged_digest: str
) -> str:
    """Carry out one command line and say what it did."""
    match words:
        case ["hold", username] | ["hold", username, _]:
            seconds = float(words[2]) if len(words) == 3 else GATE_DEADLINE_SECONDS
            for section in PERSON_PAGES:
                origin.hold(
                    person_path(username, section),
                    seconds=seconds,
                    on_event=report_gate,
                )
            return f"hold: armed {len(PERSON_PAGES)} pages of {username}"
        case ["signin"]:
            origin.release_login()
            return "signin: every login page completes from now on"
        case ["check"]:
            return "check: " + describe_session(
                profile, staged_digest, origin.accepts_digest
            )
        case _:
            return f"unknown command: {' '.join(words)}\n{COMMANDS}"


def run(origin: SyntheticOrigin, profile: Path, staged_digest: str) -> None:
    say(COMMANDS)
    for line in sys.stdin:
        words = line.split()
        if words == ["quit"]:
            return
        if not words:
            continue
        # A mistyped command must not end the run: the session lives here.
        try:
            say(command(words, origin, profile, staged_digest))
        except ValueError as exc:
            say(f"{words[0]}: {exc}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve a synthetic LinkedIn and a signed-in profile for a run by hand."
    )
    parser.add_argument(
        "--certs",
        type=Path,
        required=True,
        help="the directory synthetic_origin.py issued the certificates into",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"the proxy's loopback port (default {DEFAULT_PORT})",
    )
    args = parser.parse_args(argv)

    breaches = fence_breaches()
    if breaches:
        print(
            f"Refused: these names resolve outside loopback: {breaches}. "
            f"Map them to 127.0.0.1 and ::1 in the hosts file first.",
            file=sys.stderr,
        )
        return 1

    origin = _Origin(args.certs)
    proxy = _Proxy({host: origin.port for host in ALLOWED_HOSTS}, port=args.port)
    origin.start()
    proxy.start()
    try:
        # Read by the configuration on its first load, which staging triggers.
        os.environ["PROXY_SERVER"] = proxy.url
        origin.require_sessions()
        origin.arm_login()
        profile, staged_digest = stage(origin)
        say(f"ready: proxy {proxy.url}, profile {profile}")
        say("check: " + describe_session(profile, staged_digest, origin.accepts_digest))
        run(origin, profile, staged_digest)
    except KeyboardInterrupt:
        pass
    finally:
        proxy.stop()
        origin.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
