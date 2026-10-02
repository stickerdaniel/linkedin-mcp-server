"""Configurations the daemon must refuse (H-R12 remainder), and a rival owner
of the same build with another configuration (H-R14).

**H-R12 remainder.** Each cell runs one configuration the contract keeps on
the Direct server it had before the daemon existed, in every column the same
(``CASES``): the daemon switched off by ``DAEMON_ENABLED=false`` and by
``--no-daemon``; a container (``LINKEDIN_MCP_CONTAINER=true``, the session
staged and preserved as the same runtime); an HTTP server
(``--transport streamable-http``, reached by the harness's HTTP host,
``harness.HttpHost``); and storage the product's own classifier refuses,
from provider metadata the cell plants for its whole run: a Dropbox
``info.json`` naming the row's temporary auth root (synced), one naming the
computed daemon state root (only the state synced), a malformed one
(unknown), and on macOS a profile under ``~/Library/CloudStorage``. K3 runs
the candidate with the daemon enabled; K1 frozen runs the pinned baseline
Direct under the same configuration and reads the same way. Every cell must
show no coordination effect: no owner launched and no release gate, counted
the Windows-safe way (``host_comparison.owner_launches``); no forwarding
line and no election; no daemon state for the row's auth root and no
descriptor; a successful read whose every page went through a browser the
row's own server launched (``harness.browser_lineage``), never credited by
process timing; and R17 retained. K3 also has to say why, once: the
product's own refusal line for a container or for each storage class,
naming the root it refused. The classification of the profile, its auth
root and the state root is read with the product's own classifier during
the row (``storage_reading``), so a configuration that was not in effect is
invalid evidence and never a pass.

What a native cell cannot show stays with its models (``MODEL_COVERAGE``):
that no descriptor was read and no transient lock taken (the coordination
traps of ``tests/test_daemon.py``), and non-local mounts, for which no
rootless fixture exists on a hosted runner.

**H-R14.** Host A reads with ``LOGIN_TIMEOUT=1800``; host B starts while A
is open, with an equal build and ``LOGIN_TIMEOUT=1801``, a fingerprinted
field, and the same small ``BROWSER_MIN_HOLD`` in both columns, which is
not. B reads a person profile of its own and quits; A then reads another.
K3: B probes the owner A elected once and falls back to Direct (its own
fallback line, counted apart from forwarding), never forwards, reads
through the profile lease's handoff with a browser of its own, and quits;
A reads again through the same owner, whose identified lifetime and
instance are unchanged and which started no turnover. K1 frozen: A, B
Direct and A again, each through its own server's browser. Each of the
three reads is credited to the browser that read its own pages; an owner
that idled out before A2 is an idle timeout too small for the runner,
invalid evidence and never a finding.

K2 is recorded not applicable for every cell here: the contract's named K2
set holds only ``CHROME_PATH`` for R12, delivered in ``test_frozen_rows``.
The scripts run on a ``harness.RowContext`` with its ``coordination``
seams; the verdicts read the raw record alone, so each can be replayed from
the published packet. Invalid evidence starts with ``INVALID``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from differential import model_coverage
from differential.call_loss import (
    CALIBRATION_IDLE_TIMEOUT_SECONDS,
    INVALID,
    PERSON_TOOL,
    _phase,
    _read_of,
    _settled,
)
from differential.host_comparison import (
    host_problems,
    owner_launches,
    same_invocation,
    same_lifetime,
)
from differential.owner_loss import (
    _called,
    _identified,
    _identity,
    _mapping,
    _ns,
    _number,
    _sequence,
    _settle_tasks,
)
from differential.retirement_race import (
    START_TOLERANCE_SECONDS,
    WARM_TOOL,
    _browsers_of,
    _calls,
    _members,
    _other_launches,
    _read_ok,
)
from differential.synthetic_origin import ALLOWED_HOSTS, person_path
from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.daemon import DirectFallback

if TYPE_CHECKING:
    from differential.harness import CoordinationSeams, RowContext

#: How a row's host reaches its server (``harness.TRANSPORTS``).
STDIO = "stdio"
STREAMABLE_HTTP = "streamable-http"

ROW_DISABLED_ENV = "H-R12-disabled-env"
ROW_NO_DAEMON = "H-R12-no-daemon"
ROW_CONTAINER = "H-R12-container"
ROW_HTTP = "H-R12-http"
ROW_SYNCED = "H-R12-synced"
ROW_STATE_SYNCED = "H-R12-state-synced"
ROW_UNKNOWN = "H-R12-unknown"
ROW_CLOUD = "H-R12-cloud-storage"
ROW_RIVAL = "H-R14"

#: The calibration's idle timeout, the same in K1, K3 and K0: nothing here
#: idles out inside the row, and it is the configuration the other call rows
#: measured.
INELIGIBLE_IDLE_TIMEOUT_SECONDS = CALIBRATION_IDLE_TIMEOUT_SECONDS
#: H-R14's: the owner must outlive B's whole session, which it sits out idle,
#: with room for the slowest runner; an owner that idled out before A2 is
#: invalid evidence, never a finding.
RIVAL_IDLE_TIMEOUT_SECONDS = 120.0
#: How far before the owner's earliest possible idle deadline A2 must be sent.
#: The owner's idle clock cannot start before A1 was sent, so it cannot run
#: out before ``A1 sent + RIVAL_IDLE_TIMEOUT_SECONDS``; an A2 sent later than
#: this margin before that moment may meet an owner that retired on its own,
#: which is invalid evidence, never a finding. The margin covers A2's way to
#: the owner.
RIVAL_IDLE_MARGIN_SECONDS = 15.0

K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "the contract's named K2 set holds only CHROME_PATH for R12, delivered "
        "in test_frozen_rows; it names no historical-daemon witness for these "
        "configurations or for a rival owner, and forbids inventing one"
    ),
}

# --- What the product says --------------------------------------------------------

FORWARDING_LINE = "Forwarding to the shared browser owner"
NO_OWNER_LINE = "No shared browser owner could be started"
FALLBACK_LINE = "Leaving the shared browser owner alone: "
LIVE_RIVAL_LINE = FALLBACK_LINE + DirectFallback.LIVE_RIVAL.value
SILENT_RIVAL_LINE = FALLBACK_LINE + DirectFallback.SILENT_RIVAL.value
ASK_AGAIN_LINE = "The published daemon has not answered yet; will ask again"
ELECTING_LINE = "The published daemon is not answering; electing a new one"
UNAVAILABLE_LINE = "The shared browser owner is unavailable"
UNDECIDED_LINE = "Could not decide whether to share a browser"
CONTAINER_LINE = "DAEMON_ENABLED is ignored in a container"
_STORAGE_LINE = re.compile(
    r"The (?P<root>profile directory|directory holding the profile|daemon state "
    r"directory) is on (?P<kind>[a-z-]+) storage \((?P<reason>[^)]*)\); this "
    r"server drives its own browser"
)
#: Every line a frontend that went on to an election says; none of them may
#: appear where the configuration is refused before one.
ELECTION_LINES = {
    "no_owner": NO_OWNER_LINE,
    "fallback": FALLBACK_LINE,
    "ask_again": ASK_AGAIN_LINE,
    "electing": ELECTING_LINE,
    "unavailable": UNAVAILABLE_LINE,
}

#: The owner's own lines H-R14 reads between B and A2.
OWNER_IDLE_LINE = "Nothing has needed the browser in"
_STANDING_DOWN = re.compile(r"standing down", re.IGNORECASE)

LOCAL = "local"
SYNCED = "synced"
UNKNOWN = "unknown"

#: The provider metadata a cell plants for its whole run.
DROPBOX_AUTH_ROOT = "dropbox-auth-root"
DROPBOX_STATE_ROOT = "dropbox-state-root"
DROPBOX_MALFORMED = "dropbox-malformed"
CLOUD_STORAGE = "cloud-storage"

HTTP_NOT_ON_WINDOWS = (
    "a streamable-HTTP server reads no EOF, so its quit is the operator's "
    "interrupt, which Windows delivers to a child only through a console "
    "process group of its own, a topology no other row runs; the cell is a "
    "counted skip on Windows and the HTTP branch stays with its models there"
)
CLOUD_ONLY_ON_MACOS = (
    "~/Library/CloudStorage is the macOS file-provider folder; Linux and "
    "Windows have none, and their synced roots are the Dropbox cells and the "
    "models"
)


@dataclass(frozen=True)
class IneligibleCase:
    """One configuration the daemon must refuse, as every column runs it."""

    #: Settings over the actors' environment (``RowLifecycle.environment``).
    environment: Mapping[str, str] | None = None
    #: Settings that decide the runtime, staging and preservation included
    #: (``RowLifecycle.runtime_environment``).
    runtime_environment: Mapping[str, str] | None = None
    #: The row's own arguments (``RowLifecycle.arguments``).
    arguments: tuple[str, ...] = ()
    transport: str = STDIO
    #: The provider metadata the cell plants for its whole run, or None.
    plant: str | None = None
    #: The product's own classification of the profile, its auth root and the
    #: daemon state root while the cell runs.
    storage: tuple[str, str, str] = (LOCAL, LOCAL, LOCAL)
    #: The refusal K3 says once: ``("container",)``, ``("storage", root,
    #: class)``, or None where the configuration is refused in silence.
    refusal: tuple[str, ...] | None = None
    #: The one platform the cell runs on and why, or None for every one.
    only_on: tuple[str, str] | None = None
    #: The platform the cell cannot run on, and why.
    not_on: tuple[str, str] | None = None

    def skip_reason(self, platform: str) -> str | None:
        """Why the cell is a counted skip on *platform* (``sys.platform``),
        or None where it runs."""
        if self.not_on is not None and platform == self.not_on[0]:
            return self.not_on[1]
        if self.only_on is not None and platform != self.only_on[0]:
            return self.only_on[1]
        return None

    def as_record(self) -> dict[str, Any]:
        return {
            "plant": self.plant,
            "storage": list(self.storage),
            "refusal": list(self.refusal) if self.refusal is not None else None,
        }


CASES: dict[str, IneligibleCase] = {
    ROW_DISABLED_ENV: IneligibleCase(
        environment={EnvironmentKeys.DAEMON_ENABLED: "false"}
    ),
    ROW_NO_DAEMON: IneligibleCase(arguments=("--no-daemon",)),
    ROW_CONTAINER: IneligibleCase(
        runtime_environment={"LINKEDIN_MCP_CONTAINER": "true"},
        refusal=("container",),
    ),
    ROW_HTTP: IneligibleCase(
        transport=STREAMABLE_HTTP, not_on=("win32", HTTP_NOT_ON_WINDOWS)
    ),
    ROW_SYNCED: IneligibleCase(
        plant=DROPBOX_AUTH_ROOT,
        storage=(SYNCED, SYNCED, LOCAL),
        refusal=("storage", "profile directory", SYNCED),
    ),
    ROW_STATE_SYNCED: IneligibleCase(
        plant=DROPBOX_STATE_ROOT,
        storage=(LOCAL, LOCAL, SYNCED),
        refusal=("storage", "daemon state directory", SYNCED),
    ),
    ROW_UNKNOWN: IneligibleCase(
        plant=DROPBOX_MALFORMED,
        storage=(UNKNOWN, UNKNOWN, UNKNOWN),
        refusal=("storage", "profile directory", UNKNOWN),
    ),
    ROW_CLOUD: IneligibleCase(
        plant=CLOUD_STORAGE,
        storage=(SYNCED, SYNCED, LOCAL),
        refusal=("storage", "profile directory", SYNCED),
        only_on=("darwin", CLOUD_ONLY_ON_MACOS),
    ),
}
INELIGIBLE_ROWS = tuple(CASES)

# --- H-R14's configuration ---------------------------------------------------------

#: Host A's settings and the owner's, the same in every column. The minimum
#: hold is small and equal in both hosts: it decides how soon the profile is
#: handed over and is not in the fingerprint.
OWNER_LOGIN_TIMEOUT = "1800"
RIVAL_LOGIN_TIMEOUT = "1801"
MIN_HOLD_SECONDS = 2.0
RIVAL_ENVIRONMENT = {
    EnvironmentKeys.LOGIN_TIMEOUT: OWNER_LOGIN_TIMEOUT,
    EnvironmentKeys.BROWSER_MIN_HOLD: f"{MIN_HOLD_SECONDS:g}",
}
#: Host B's, over the row's: one fingerprinted field, one second apart.
RIVAL_OVERRIDES = {EnvironmentKeys.LOGIN_TIMEOUT: RIVAL_LOGIN_TIMEOUT}
#: Each read's own pages, so no request of one stands for another's.
B_USERNAME = "synthetic-rival"
A2_USERNAME = "synthetic-rival-again"
#: B's whole session: its start, one read through the handoff, and its quit.
RIVAL_SECONDS = 240.0
#: How long the product's classifier is given for the three roots.
STORAGE_SECONDS = 30.0

# --- Reading what the frontend decided --------------------------------------------


def decision_lines(lines: Sequence[str]) -> dict[str, Any]:
    """How often a frontend or Direct server said each of its coordination
    decisions, and every storage refusal by the root and class it names."""
    found: dict[str, Any] = {
        "forwarding": sum(1 for line in lines if FORWARDING_LINE in line),
        "live_rival": sum(1 for line in lines if LIVE_RIVAL_LINE in line),
        "silent_rival": sum(1 for line in lines if SILENT_RIVAL_LINE in line),
        "container": sum(1 for line in lines if CONTAINER_LINE in line),
        "undecided": sum(1 for line in lines if UNDECIDED_LINE in line),
        "election": {
            name: sum(1 for line in lines if text in line)
            for name, text in ELECTION_LINES.items()
        },
        "storage": [],
    }
    for line in lines:
        match = _STORAGE_LINE.search(line)
        if match is not None:
            found["storage"].append([match.group("root"), match.group("kind")])
    return found


def owner_lines(lines: Sequence[str]) -> dict[str, int]:
    """How often the owner's log says it idled out, or stood down for anyone."""
    return {
        "idle_exit": sum(1 for line in lines if OWNER_IDLE_LINE in line),
        "stood_down": sum(1 for line in lines if _STANDING_DOWN.search(line)),
    }


def storage_reading(profile: Path) -> dict[str, Any]:
    """The product's own classification of the three roots it asks about
    before it shares a browser (``daemon._storage_refusal``): the profile,
    the directory holding it, and the daemon state root. Reads, creates
    nothing; each as its class and reason, which name no path."""
    from linkedin_mcp_server import daemon_descriptor, storage_class
    from linkedin_mcp_server.session_state import auth_root_dir, canonical

    resolved = canonical(profile)
    found: dict[str, Any] = {}
    for name, root in (
        ("profile", resolved),
        ("auth_root", auth_root_dir(resolved)),
        ("state_root", daemon_descriptor.daemon_state_root()),
    ):
        verdict = storage_class.classify(root)
        found[name] = {"class": verdict.storage_class.value, "reason": verdict.reason}
    return found


# --- The scripts --------------------------------------------------------------------


def _seams(ctx: RowContext) -> CoordinationSeams | None:
    if ctx.coordination is None:
        ctx.record["observation_problems"].append(
            f"{INVALID}the row was given no way to read the frontend's decision"
        )
    return ctx.coordination


async def _storage(ctx: RowContext) -> dict[str, Any]:
    try:
        return await ctx.run_owned(
            "the storage classification",
            storage_reading,
            ctx.account.profile,
            seconds=STORAGE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - the reading's own evidence
        return {"error": f"{type(exc).__name__}: {exc}"}


async def ineligible_script(ctx: RowContext) -> None:
    """An H-R12 cell's scripted phase, after its one read: which server ran,
    what the product's classifier reads for the cell, and what the frontend
    said. It calls nothing more."""
    record = ctx.record
    record["case"] = CASES[ctx.row].as_record()
    record["host_a"] = {"pid": getattr(ctx.transport, "pid", None)}
    seams = _seams(ctx)
    record["storage"] = await _storage(ctx)
    if seams is not None:
        record["lines"] = decision_lines(seams.host_output())


async def rival_script(ctx: RowContext) -> None:
    """H-R14 after A1: host B with its own configuration reads and quits while
    A stays open, B's browser is shown gone, and A reads again."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    record["host_a"] = {"pid": getattr(ctx.transport, "pid", None)}
    record["usernames"] = {"B": B_USERNAME, "A2": A2_USERNAME}
    record["storage"] = await _storage(ctx)
    seams = _seams(ctx)
    if seams is None:
        return
    if seams.rival is None:
        problems.append(f"{INVALID}the row was given no way to start a rival host")
        return
    if ctx.daemon:
        record["owner_identified"] = _identity(ctx)
        if record["owner_identified"] is None:
            problems.append(f"{INVALID}the owner was never identified")
            return
        record["owner_before_b"] = await seams.owner_reading("before B")
    record["lines"] = decision_lines(seams.host_output())
    _phase(ctx, "rival")
    rival = asyncio.ensure_future(
        seams.rival(RIVAL_OVERRIDES, tool=PERSON_TOOL, arguments=_read_of(B_USERNAME))
    )
    try:
        await asyncio.wait({rival}, timeout=RIVAL_SECONDS)
        record["rival"] = (
            rival.result()
            if rival.done() and not rival.cancelled() and rival.exception() is None
            else None
        )
    finally:
        await _settle_tasks([rival])
    if record["rival"] is None:
        problems.append(
            f"{INVALID}the rival host did not end within {RIVAL_SECONDS}s, or its "
            f"start failed"
        )
        return
    if ctx.daemon:
        record["owner_after_b"] = await seams.owner_reading("after B")
        record["owner_lines_after_b"] = owner_lines(seams.owner_log())
    # In either mode nothing holds the profile once B quit: the owner gave its
    # browser up to B, and B closed its own. Shown before A2 asks for it.
    record["before_a2"] = await seams.settlement()
    if not _settled(record["before_a2"]):
        problems.append(
            f"{INVALID}B's browser was not shown gone before A2, so A2 was not sent"
        )
        return
    if ctx.daemon and _owner_near_idle(record["owner_identified"], time.time()):
        problems.append(f"{INVALID}{NEAR_IDLE}, so A2 was not sent")
        return
    _phase(ctx, "A2")
    await _called(ctx, PERSON_TOOL, _read_of(A2_USERNAME))
    if ctx.daemon:
        record["owner_after_a2"] = await seams.owner_reading("after A2")


# --- Reading a record ---------------------------------------------------------------


def _requests(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [_mapping(request) for request in _sequence(record.get("requests"))]


def _feed_pages(record: Mapping[str, Any], call: Mapping[str, Any]) -> list[Any]:
    """The ``/feed/`` requests inside *call*'s wall interval, by arrival."""
    began, ended = _number(call.get("began")), _number(call.get("ended"))
    if began is None or ended is None:
        return []
    return [
        _number(request.get("t"))
        for request in _requests(record)
        if str(request.get("path", "")).split("?", 1)[0] == "/feed/"
        and began <= (_number(request.get("t")) or 0.0) <= ended
    ]


def _person_pages(record: Mapping[str, Any], username: str) -> list[Any]:
    """Every request for *username*'s pages, by arrival: no other read asks
    for them."""
    prefix = person_path(username, "main_profile")
    return [
        _number(request.get("t"))
        for request in _requests(record)
        if str(request.get("path", "")).startswith(prefix)
    ]


def server_members(
    record: Mapping[str, Any], pid: Any, *, launched: float | None = None
) -> list[Sequence[Any]]:
    """The lifetimes of the server a host started as *pid*: that process and,
    on Windows, the interpreter its venv launcher started for it
    (``host_comparison.same_invocation``), from the row's server lifetimes
    (``harness.frontend_lifetimes``). With *launched* (wall clock), a reused
    pid's lifetime begun before the launch is not it."""
    lifetimes = [
        _sequence(p)
        for p in _sequence(record.get("frontend_processes"))
        if len(_sequence(p)) >= 7 and _number(_sequence(p)[1]) is not None
    ]
    own = [
        p
        for p in lifetimes
        if type(pid) is int
        and p[0] == pid
        and (launched is None or float(p[1]) >= launched - START_TOLERANCE_SECONDS)
    ]
    if not own:
        return []
    first = min(own, key=lambda p: float(p[1]))
    return [first, *(child for child in lifetimes if same_invocation(first, child))]


def _unread(pages: Sequence[Any], browsers: Sequence[Sequence[Any]]) -> int:
    """How many of *pages* (arrivals, wall clock) no browser of *browsers*
    was alive for: the browser that read a page was running when it came."""
    return sum(
        1
        for page in pages
        if page is None
        or not any(
            (_number(root[1]) or 0.0) - START_TOLERANCE_SECONDS <= page
            and (root[2] is None or page <= (_number(root[2]) or 0.0))
            for root in browsers
        )
    )


def read_through(
    record: Mapping[str, Any],
    pages: Sequence[Any],
    members: Sequence[Sequence[Any]],
    *,
    read: str,
    whose: str,
) -> list[str]:
    """*read*'s *pages* each went through a browser one of *members*
    launched. A launch that read a page had a browser of its own, so this
    names who read it whatever the processes' timing."""
    if not members:
        return [f"{INVALID}{whose} was never seen among the row's processes"]
    browsers = _browsers_of(members, record)
    if browsers is None:
        return [
            f"{INVALID}the row's browsers and who launched them were not recorded, "
            f"so {read} cannot be credited"
        ]
    if not pages:
        return [f"{INVALID}{read} asked the origin for no page of its own"]
    unread = _unread(pages, browsers)
    if unread:
        return [
            f"{unread} of {read}'s {len(pages)} page(s) went through no browser "
            f"{whose} launched"
        ]
    return []


def _launch_problems(record: Mapping[str, Any]) -> list[str]:
    """No owner and no release gate, each launch counted once on Windows."""
    owners, gates = record.get("owner_processes"), record.get("gate_processes")
    if not isinstance(owners, list) or not isinstance(gates, list):
        return [
            f"{INVALID}the row's owner and release-gate lifetimes were not recorded"
        ]
    windows = str(record.get("platform", "")).startswith("win")
    found = []
    launched = owner_launches(owners, windows=windows)
    if launched:
        found.append(f"the row started {len(launched)} shared owner process(es)")
    gated = owner_launches(gates, windows=windows)
    if gated:
        found.append(
            f"the row started {len(gated)} owner release gate(s), an owner start "
            f"attempted"
        )
    return found


def _quiet(lines: Mapping[str, Any], *, who: str) -> list[str]:
    """No forwarding and no election at all."""
    found = []
    if lines.get("forwarding"):
        found.append(f"{who} forwarded to a shared owner")
    election = {
        name: count for name, count in _mapping(lines.get("election")).items() if count
    }
    if election:
        found.append(f"{who} went on to an election: {sorted(election)}")
    if lines.get("undecided"):
        found.append(f"{who} could not decide whether to share a browser")
    return found


def _egress(record: Mapping[str, Any]) -> list[str]:
    forwarded = _mapping(record.get("egress")).get("forwarded")
    if not isinstance(forwarded, list):
        return ["the row's egress through its proxy was not recorded"]
    outside = sorted(set(forwarded) - set(ALLOWED_HOSTS))
    if outside:
        return [
            f"the proxy forwarded the row to hosts outside the synthetic origin: "
            f"{outside}"
        ]
    return []


def _declared(
    record: Mapping[str, Any],
    *,
    idle: float,
    environment: Mapping[str, str] | None,
    runtime_environment: Mapping[str, str] | None,
    arguments: Sequence[str],
    transport: str,
) -> list[str]:
    """The cell ran with exactly what it declared, read back from the actors'
    own environment and command; anything else measured another cell."""
    found = []
    if record.get("idle_timeout_seconds") != idle:
        found.append(
            f"the row ran with an idle timeout of "
            f"{record.get('idle_timeout_seconds')!r}, not the declared {idle}"
        )
    if (
        record.get("environment") != (dict(environment) if environment else None)
        or record.get("runtime_environment")
        != (dict(runtime_environment) if runtime_environment else None)
        or list(_sequence(record.get("arguments"))) != list(arguments)
        or record.get("transport") != transport
    ):
        found.append(
            f"{INVALID}the cell did not run its declared configuration: "
            f"{record.get('environment')!r}, {record.get('runtime_environment')!r}, "
            f"{record.get('arguments')!r}, {record.get('transport')!r}"
        )
    return found


def _common(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    problems: list[str] = []
    mode = "daemon" if daemon else "direct"
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    if record.get("script_error"):
        problems.append(f"the row's script failed: {record['script_error']}")
    problems += [str(p) for p in _sequence(record.get("observation_problems"))]
    if record.get("k2") != K2_NOT_APPLICABLE:
        problems.append("the record does not say why K2 is not applicable")
    left = _sequence(record.get("left_running"))
    if left:
        problems.append(
            f"{INVALID}a harness failure: the row left {list(left)} running, and "
            f"the harness ended it"
        )
    return problems + _egress(record)


def http_host_problems(host: Any) -> list[str]:
    """Why an HTTP host's end was not one normal quit: alive when it was
    interrupted, the interrupt delivered, then its own exit with status 0,
    both times recorded in order (``host_comparison.host_problems`` for an
    interrupt in place of EOF)."""
    host = _mapping(host)
    problems = []
    if host.get("error"):
        problems.append(f"the host session failed: {host['error']}")
    if host.get("alive_before_quit") is not True:
        problems.append("the HTTP server was not shown alive when it was interrupted")
    if host.get("interrupted") is not True:
        problems.append(
            f"the HTTP server's interrupt was not shown delivered: "
            f"{host.get('interrupt_error')}"
        )
    if (
        host.get("exited_on_quit") is not True
        or host.get("exit_code") != 0
        or host.get("killed_by_harness") is not False
    ):
        problems.append(
            f"the HTTP server's quit was not a normal exit on its interrupt: exited "
            f"{host.get('exited_on_quit')!r}, status {host.get('exit_code')!r}, "
            f"killed {host.get('killed_by_harness')!r}"
        )
    sent, seen = _ns(host.get("interrupt_ns")), _ns(host.get("exit_seen_ns"))
    if sent is None or seen is None or seen < sent:
        problems.append("the interrupt and the exit after it are not recorded in order")
    return problems


def _storage_problems(record: Mapping[str, Any], expected: Sequence[str]) -> list[str]:
    """The product's classifier read the cell's declared storage for each of
    the three roots, or the configuration was not in effect."""
    storage = _mapping(record.get("storage"))
    if storage.get("error") or not storage:
        return [
            f"{INVALID}the storage classification was not read: "
            f"{storage.get('error')!r}"
        ]
    seen = [
        _mapping(storage.get(name)).get("class")
        for name in ("profile", "auth_root", "state_root")
    ]
    if seen != list(expected):
        return [
            f"{INVALID}the product's classifier read {seen} for the profile, its "
            f"auth root and the state root, not the cell's {list(expected)}"
        ]
    return []


def _state_problems(record: Mapping[str, Any]) -> list[str]:
    found = []
    existed, published = (
        record.get("daemon_state_existed"),
        record.get("descriptor_present"),
    )
    if existed is None or published is None:
        return [f"{INVALID}the row's daemon state and descriptor were not recorded"]
    if existed:
        found.append("daemon state was created for the row's auth root")
    if published:
        found.append("a descriptor was published for the row's auth root")
    return found


def _refusal_problems(
    lines: Mapping[str, Any], refusal: Sequence[str] | None, *, daemon: bool
) -> list[str]:
    """K3 says why it keeps its own browser, once and for the declared
    reason; K1 frozen, Direct by its configuration, asks nothing."""
    storage = [list(_sequence(item)) for item in _sequence(lines.get("storage"))]
    said = {"container": int(lines.get("container") or 0), "storage": storage}
    if not daemon:
        if said["container"] or storage:
            return [
                f"{INVALID}the frozen Direct column asked whether to share a "
                f"browser: {said}"
            ]
        return []
    if refusal is None:
        if said["container"] or storage:
            return [
                f"the frontend refused for a reason the cell does not declare: {said}"
            ]
        return []
    if refusal[0] == "container":
        found = []
        if said["container"] != 1:
            found.append(
                f"the frontend said {said['container']} time(s), not once, that a "
                f"container ignores DAEMON_ENABLED"
            )
        if storage:
            found.append(f"the frontend also refused for its storage: {storage}")
        return found
    wanted = list(refusal[1:])
    if storage != [wanted]:
        return [
            f"the frontend did not refuse once for the {refusal[1]} on "
            f"{refusal[2]} storage: {storage}"
        ]
    if said["container"]:
        return ["the frontend also said a container ignores DAEMON_ENABLED"]
    return []


def _direct_read(record: Mapping[str, Any]) -> list[str]:
    """The one read returned the post, and every page of it went through a
    browser the row's own server launched."""
    calls = [_mapping(call) for call in _sequence(record.get("calls"))]
    first = calls[0] if calls else {}
    if not (
        first.get("tool") == WARM_TOOL
        and first.get("outcome") == "returned"
        and first.get("is_error") is False
        and first.get("read_the_post") is True
    ):
        return ["the row's read did not return the post"]
    members = server_members(record, _mapping(record.get("host_a")).get("pid"))
    return read_through(
        record,
        _feed_pages(record, first),
        members,
        read="the row's read",
        whose="the row's own server",
    )


# --- H-R12 remainder: the verdict ---------------------------------------------------


def ineligible_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """An H-R12 remainder cell's verdict, any configuration: every problem, or
    nothing."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    row = record.get("row")
    case = CASES.get(str(row))
    if case is None:
        return [
            f"the record is for row {row!r}, which declares no refused configuration"
        ]
    problems = _common(record, daemon=daemon)
    problems += _declared(
        record,
        idle=INELIGIBLE_IDLE_TIMEOUT_SECONDS,
        environment=case.environment,
        runtime_environment=case.runtime_environment,
        arguments=case.arguments,
        transport=case.transport,
    )
    if _mapping(record.get("case")) != case.as_record():
        problems.append(f"{INVALID}the record does not name the cell's own case")
    host = record.get("host")
    problems += (
        http_host_problems(host)
        if case.transport == STREAMABLE_HTTP
        else host_problems(host)
    )
    problems += _storage_problems(record, case.storage)
    lines = record.get("lines")
    if not isinstance(lines, Mapping):
        problems.append(f"{INVALID}what the server said was not recorded")
    else:
        problems += _quiet(lines, who="the frontend" if daemon else "Direct")
        problems += _refusal_problems(lines, case.refusal, daemon=daemon)
    problems += _launch_problems(record)
    problems += _state_problems(record)
    problems += _direct_read(record)
    return problems


# --- H-R14: the verdict -------------------------------------------------------------


def _rival_host(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """B ran with its one difference, read its own profile, never forwarded,
    and quit normally; in K3 it fell back after one probe."""
    rival = _mapping(record.get("rival"))
    if not rival.get("made"):
        return [f"{INVALID}the rival host never ran: {rival.get('why')!r}"]
    found = []
    if rival.get("environment") != RIVAL_OVERRIDES:
        found.append(
            f"{INVALID}B did not run with its declared difference: "
            f"{rival.get('environment')!r}"
        )
    if rival.get("quit_problems"):
        found.append(f"B's quit: {rival['quit_problems']}")
    if rival.get("retained") is not False:
        found.append(f"{INVALID}B's server is not shown gone after its quit")
    if not _read_ok(_mapping(rival.get("call"))):
        found.append("B's read did not return its own profile")
    if rival.get("forwarded") is not False:
        found.append("B forwarded to the owner of another configuration")
    lines = _mapping(rival.get("lines"))
    fallbacks = int(lines.get("live_rival") or 0) + int(lines.get("silent_rival") or 0)
    election = _mapping(lines.get("election"))
    if not daemon:
        if _quiet(lines, who="B"):
            found.append(f"{INVALID}the frozen Direct B asked about an owner: {lines}")
        return found
    if fallbacks != 1:
        found.append(f"B fell back {fallbacks} time(s), not once after one probe")
    if election.get("ask_again") or election.get("electing"):
        found.append(
            "B probed the rival again or wrote it off as leftovers instead of "
            "leaving it alone"
        )
    if election.get("no_owner") != 1:
        found.append(
            f"B did not warn once that it drives its own browser: "
            f"{election.get('no_owner')!r}"
        )
    return found


def _order(
    record: Mapping[str, Any], a1: Mapping[str, Any], a2: Mapping[str, Any] | None
) -> list[str]:
    """B after A1, and A2 after B's browser was shown gone."""
    found = []
    rival = _mapping(record.get("rival"))
    launched, ended = _ns(rival.get("launched_ns")), _ns(a1.get("ended_monotonic_ns"))
    if launched is None or ended is None or launched < ended:
        found.append(f"{INVALID}B was not started after A1 ended")
    before = _mapping(record.get("before_a2"))
    if not _settled(before):
        found.append(f"{INVALID}B's browser was not shown gone before A2")
    elif a2 is not None and (_ns(a2.get("began_monotonic_ns")) or 0) < (
        _ns(before.get("seen_ns")) or 0
    ):
        found.append(f"{INVALID}A2 was sent before B's browser was shown gone")
    return found


NEAR_IDLE = (
    f"A2 was due within {RIVAL_IDLE_MARGIN_SECONDS:g}s of the earliest moment "
    f"the owner could idle out"
)


def _near_idle(a1: Mapping[str, Any], at_ns: int | None) -> bool:
    """Whether A2 at *at_ns* is too close to the owner's earliest possible
    idle deadline for its answer to say anything about B (unknown counts as
    too close)."""
    began = _ns(a1.get("began_monotonic_ns"))
    if began is None or at_ns is None:
        return True
    deadline = began + int(
        (RIVAL_IDLE_TIMEOUT_SECONDS - RIVAL_IDLE_MARGIN_SECONDS) * 1e9
    )
    return at_ns >= deadline


def _owner_near_idle(identified: Sequence[Any], now: float) -> bool:
    """The script's check before sending A2, on the wall clock: the owner's
    idle clock cannot start before the owner did, so its start time bounds
    the deadline from below as A1's send does (``_near_idle``), only earlier."""
    started = identified[1] if len(identified) > 1 else None
    if not isinstance(started, (int, float)):
        return True
    return now >= started + RIVAL_IDLE_TIMEOUT_SECONDS - RIVAL_IDLE_MARGIN_SECONDS


def _owner_kept(record: Mapping[str, Any], identified: Sequence[Any]) -> list[str]:
    """The owner A elected, alive and the same lifetime and instance before
    B, after B and after A2, and no other owner launched: B neither turned
    it over nor replaced it."""
    found = []
    for label in ("owner_before_b", "owner_after_b", "owner_after_a2"):
        seen = _mapping(record.get(label))
        if not (
            seen.get("alive") is True
            and same_lifetime(seen.get("lifetime"), identified[:2])
            and seen.get("instance_id") == identified[2]
        ):
            found.append(
                f"the owner A reads through is not the one A1 reached at "
                f"{label.removeprefix('owner_')}: {seen.get('lifetime')!r}, "
                f"{seen.get('problem')!r}"
            )
    others = _other_launches(record, identified[:2])
    if others is None:
        found.append(
            f"{INVALID}the row's owner and release-gate lifetimes were not recorded"
        )
    elif others:
        found.append(f"another owner was launched beside the one A elected: {others}")
    lines = _mapping(record.get("owner_lines_after_b"))
    if not lines:
        found.append(f"{INVALID}the owner's log was not read after B")
    elif lines.get("idle_exit"):
        found.append(
            f"{INVALID}the owner idled out before A2: the idle timeout is too "
            f"small for this runner"
        )
    elif lines.get("stood_down"):
        found.append("the owner stood down while B ran")
    return found


def rival_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """H-R14's verdict over its raw record: every problem, or nothing."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    if record.get("row") != ROW_RIVAL:
        return [f"the record is for row {record.get('row')!r}, which runs no rival"]
    problems = _common(record, daemon=daemon)
    problems += _declared(
        record,
        idle=RIVAL_IDLE_TIMEOUT_SECONDS,
        environment=RIVAL_ENVIRONMENT,
        runtime_environment=None,
        arguments=(),
        transport=STDIO,
    )
    problems += host_problems(record.get("host"))
    problems += _storage_problems(record, (LOCAL, LOCAL, LOCAL))
    calls = [_mapping(call) for call in _sequence(record.get("calls"))]
    a1 = calls[0] if calls else {}
    if not (
        a1.get("tool") == WARM_TOOL
        and a1.get("outcome") == "returned"
        and a1.get("read_the_post") is True
    ):
        return [*problems, f"{INVALID}A1 did not return the post"]
    if daemon and _identified(record) is None:
        return [*problems, f"{INVALID}the owner was never identified"]
    problems += _rival_host(record, daemon=daemon)
    reads = _calls(record, PERSON_TOOL)
    if len(reads) > 1:
        problems.append(f"{INVALID}A read {len(reads)} profiles, not the one A2")
    a2 = reads[0] if len(reads) == 1 else None
    problems += _order(record, a1, a2)
    if a2 is None:
        return [*problems, f"{INVALID}A2 was never sent"]
    rival = _mapping(record.get("rival"))
    b_members = server_members(
        record, rival.get("pid"), launched=_number(rival.get("launched"))
    )
    problems += read_through(
        record,
        _person_pages(record, B_USERNAME),
        b_members,
        read="B's read",
        whose="B's own server",
    )
    if daemon and _near_idle(a1, _ns(a2.get("began_monotonic_ns"))):
        # Whatever A2 met, the owner may have retired on its own first.
        return [*problems, f"{INVALID}{NEAR_IDLE}"]
    if not _read_ok(a2):
        problems.append("A2 did not return its profile")
    if daemon:
        identified = _identified(record)
        assert identified is not None
        reader = _members(list(identified[:2]), record)
        whose = "the owner A1 reached"
        problems += _owner_kept(record, identified)
    else:
        reader = server_members(record, _mapping(record.get("host_a")).get("pid"))
        whose = "A's own server"
        problems += _launch_problems(record)
    problems += read_through(
        record, _feed_pages(record, a1), reader, read="A1", whose=whose
    )
    problems += read_through(
        record, _person_pages(record, A2_USERNAME), reader, read="A2", whose=whose
    )
    lines = record.get("lines")
    if not isinstance(lines, Mapping):
        problems.append(f"{INVALID}what A's server said was not recorded")
    elif daemon and lines.get("forwarding") != 1:
        problems.append("A's frontend did not forward to the owner it elected")
    elif not daemon and _quiet(lines, who="A"):
        problems.append(f"{INVALID}the frozen Direct A asked about an owner")
    return problems


# --- The verdicts, by row -----------------------------------------------------------


def problems_for(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """The verdict of whichever row of this module *record* is for."""
    if _mapping(record).get("row") == ROW_RIVAL:
        return rival_problems(record, daemon=daemon)
    return ineligible_problems(record, daemon=daemon)


def invalid_evidence(problems: Sequence[str]) -> list[str]:
    """The problems that leave a row unmeasured rather than failed."""
    return [problem for problem in problems if problem.startswith(INVALID)]


def semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """What K0 compares: classifications only, no pid, time or path."""
    lines = _mapping(record.get("lines"))
    storage = _mapping(record.get("storage"))
    found: dict[str, Any] = {
        "row": record.get("row"),
        "mode": record.get("mode"),
        "storage": [
            _mapping(storage.get(name)).get("class")
            for name in ("profile", "auth_root", "state_root")
        ],
        "forwarding": lines.get("forwarding"),
        "container": lines.get("container"),
        "refused": [list(_sequence(item)) for item in _sequence(lines.get("storage"))],
        "owners": len(_sequence(record.get("owner_processes"))),
        "state": record.get("daemon_state_existed"),
        "reads": [
            call.get("outcome") == "returned" and call.get("is_error") is False
            for call in map(_mapping, _sequence(record.get("calls")))
        ],
    }
    if record.get("row") == ROW_RIVAL:
        rival = _mapping(record.get("rival"))
        rival_lines = _mapping(rival.get("lines"))
        found["rival"] = {
            "read": _read_ok(_mapping(rival.get("call"))),
            "forwarded": rival.get("forwarded"),
            # Fell back or not; live against silent is the probe's timing.
            "fell_back": int(rival_lines.get("live_rival") or 0)
            + int(rival_lines.get("silent_rival") or 0),
        }
    return found


def _refusals(named: Sequence[tuple[str, Mapping[str, Any] | None, bool]]) -> list[str]:
    refusals = []
    for name, record, daemon in named:
        problems = problems_for(record, daemon=daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    return refusals


def semantic_differences(
    reference: Mapping[str, Any] | None, repeat: Mapping[str, Any] | None
) -> list[str]:
    """K0 against K3: both valid by their own verdict, and alike in every
    classification. A missing or invalid record is a refusal."""
    refusals = _refusals([("reference", reference, True), ("repeat", repeat, True)])
    if refusals:
        return refusals
    assert reference is not None and repeat is not None
    one, two = semantics(reference), semantics(repeat)
    return [
        f"{name}: {one[name]!r} then {two.get(name)!r}"
        for name in one
        if one[name] != two.get(name)
    ]


def comparison_refusals(
    direct: Mapping[str, Any] | None, daemon: Mapping[str, Any] | None
) -> list[str]:
    """Why K3 cannot be held to K1 on a row here: a record missing or invalid.
    O1 to O4 are the vectors' (``compare_to_direct``)."""
    return _refusals([("Direct", direct, False), ("daemon", daemon, True)])


# --- Provider metadata, planted on a disposable runner -----------------------------


class ProviderConfigExists(RuntimeError):
    """A provider configuration is already where the cell would plant one."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _missing_ancestors(path: Path) -> list[Path]:
    """The directories above *path* that do not exist, outermost first."""
    missing = []
    for parent in path.parents:
        if os.path.lexists(parent):
            break
        missing.append(parent)
    return list(reversed(missing))


@dataclass
class PlantedFile:
    """One provider file the cell wrote, and the directories it created for
    it. Removed exactly: the file only while it is still the one written
    (the same device and inode, not a link) and holds what was written, then
    each directory only while it is empty."""

    path: Path
    sha256: str
    #: ``(st_dev, st_ino)`` of the file as created.
    identity: tuple[int, int]
    created: tuple[Path, ...] = ()
    removed: bool = False

    def remove(self) -> list[str]:
        if self.removed:
            return []
        problems: list[str] = []
        try:
            found = os.lstat(self.path)
            content = self.path.read_bytes()
        except FileNotFoundError:
            problems.append(f"the planted {self.path.name} was gone before its removal")
        else:
            if (found.st_dev, found.st_ino) != self.identity or os.path.islink(
                self.path
            ):
                return [
                    f"the planted {self.path} was replaced by another file; it and "
                    f"its directories were left as they are"
                ]
            if _sha256(content) != self.sha256:
                return [
                    f"the planted {self.path} no longer holds what was written; "
                    f"it and its directories were left as they are"
                ]
            self.path.unlink()
        for directory in reversed(self.created):
            try:
                directory.rmdir()
            except OSError as exc:
                problems.append(f"{directory} was kept: {type(exc).__name__}")
        self.removed = True
        return problems


@dataclass
class PlantedTree:
    """A directory the cell created, its whole content its own, and the
    ancestors it created for it. Removed exactly: the tree, then each
    ancestor only while it is empty."""

    root: Path
    created: tuple[Path, ...] = ()
    removed: bool = False

    def remove(self) -> list[str]:
        if self.removed:
            return []
        problems: list[str] = []
        try:
            shutil.rmtree(self.root)
        except OSError as exc:
            problems.append(f"{self.root} could not be removed: {type(exc).__name__}")
        for directory in reversed(self.created):
            try:
                directory.rmdir()
            except OSError as exc:
                problems.append(f"{directory} was kept: {type(exc).__name__}")
        self.removed = True
        return problems


def dropbox_locations() -> list[Path]:
    """Every place the product reads Dropbox's account file from, on this
    platform and for this account, as the product itself names them
    (``storage_class._dropbox_info_files``)."""
    from linkedin_mcp_server import storage_class

    found = storage_class._dropbox_info_files(sys.platform, storage_class._homes())
    if found is None:
        raise ProviderConfigExists(
            "the product cannot name where Dropbox's file would be, so one being "
            "there already cannot be ruled out"
        )
    return list(found)


def dropbox_document(root: Path) -> bytes:
    """An ``info.json`` with one account whose folder is *root*."""
    return json.dumps({"personal": {"path": str(root), "host": 1}}).encode()


#: What no Dropbox client writes: present, and not JSON.
MALFORMED_DOCUMENT = b"{ this is not an info.json"


def plant_file(path: Path, content: bytes, *, locations: Sequence[Path]) -> PlantedFile:
    """Write *content* to *path*, one of *locations*, where nothing of any
    provider is; refused when any of *locations* already exists, and never
    written over (the file is created exclusively)."""
    if path not in locations:
        raise ValueError(f"{path} is not where the product reads a provider file")
    present = [str(p) for p in locations if os.path.lexists(p)]
    if present:
        raise ProviderConfigExists(
            f"refusing to plant: a provider configuration already exists at {present}"
        )
    created = _missing_ancestors(path)
    for directory in created:
        directory.mkdir()
    try:
        with open(path, "xb") as handle:
            handle.write(content)
            stat = os.fstat(handle.fileno())
    except BaseException:
        for directory in reversed(created):
            with contextlib.suppress(OSError):
                directory.rmdir()
        raise
    return PlantedFile(
        path, _sha256(content), (stat.st_dev, stat.st_ino), tuple(created)
    )


def plant_tree(root: Path) -> PlantedTree:
    """Create *root*, which must not exist, and its missing ancestors."""
    if os.path.lexists(root):
        raise ProviderConfigExists(f"refusing to plant: {root} already exists")
    created = _missing_ancestors(root)
    for directory in created:
        directory.mkdir()
    root.mkdir()
    return PlantedTree(root, tuple(created))


def cloud_storage_root(home: Path, name: str) -> Path:
    """A folder of the cell's own inside the macOS file-provider folder."""
    return home / "Library" / "CloudStorage" / name


@dataclass(frozen=True)
class Planted:
    """What a cell planted for its whole run, removed by ``remove``."""

    kind: str
    items: tuple[PlantedFile | PlantedTree, ...] = field(default_factory=tuple)

    def remove(self) -> list[str]:
        problems: list[str] = []
        for item in reversed(self.items):
            problems += item.remove()
        return problems


def plant_for(kind: str, *, auth_root: Path, state_root: Path) -> Planted:
    """The Dropbox file *kind* names, at the first place the product reads
    it, naming *auth_root* or *state_root* or nothing parseable."""
    locations = dropbox_locations()
    if kind == DROPBOX_AUTH_ROOT:
        content = dropbox_document(auth_root)
    elif kind == DROPBOX_STATE_ROOT:
        content = dropbox_document(state_root)
    elif kind == DROPBOX_MALFORMED:
        content = MALFORMED_DOCUMENT
    else:
        raise ValueError(f"no Dropbox file for {kind!r}")
    if not locations:
        raise ProviderConfigExists("the product reads Dropbox's file from nowhere")
    return Planted(kind, (plant_file(locations[0], content, locations=locations),))


# --- What stays with the models ------------------------------------------------------

#: Every R12 branch and R14 path left to its models; the accounting counts
#: them from ``model_coverage``.
MODEL_COVERAGE = model_coverage.ELIGIBILITY_MODEL_COVERAGE

#: The H-R14 lane no cell and no test reaches, kept open.
RIVAL_RETRY_OPEN = (
    "a configuration mismatch met only on a retry or settlement lookup is not "
    "observed: the same _live_lookup decides every lookup, but no test "
    "publishes the rival only after the first, so the first-look models "
    "beside it (RIVAL_RETRY_REFERENCES) are references and the lane stays open"
)
#: The first-look models beside the open lane: references, never counted as
#: its coverage (``RIVAL_RETRY_OPEN``).
RIVAL_RETRY_REFERENCES: tuple[str, ...] = (
    "tests/test_daemon_election.py::TestAnOwnerThisBuildMayOnlyControl"
    "::test_a_live_owner_of_this_build_with_another_configuration_is_left_alone",
    "tests/test_daemon_election.py::TestAnOwnerThisBuildMayOnlyControl"
    "::test_a_silent_owner_of_this_build_with_another_configuration_is_left_alone",
    "tests/test_daemon_election.py::TestAnOwnerThisBuildMayOnlyControl"
    "::test_a_dead_owner_of_another_configuration_is_leftovers",
)
