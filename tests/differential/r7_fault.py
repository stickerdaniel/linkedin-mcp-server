"""R7's declared fault: one selected POSIX drain's True, handed back as False.

This file is copied byte for byte into each actor overlay (``fault_overlay``),
whose ``.pth`` imports it and calls :func:`arm`. It uses the standard library
only and imports nothing from this repository, so the same text runs under the
frozen baseline and the candidate.

**What it changes.** Nothing, until ``linkedin_mcp_server.process_tree`` has
been imported by the product itself: then the private global
``_drain_marked_posix_groups`` is replaced, once, by :meth:`Fault.drain`. The
module is never reloaded, and ``core.browser`` keeps its by-value alias of the
public ``drain_browser_process_marker``, whose globals are that same module's,
so the alias reaches the replacement. The real function is called exactly once
per entry with the arguments and deadline it was given.

**Which call.** Only the original actor's, bound by the activation file the
harness publishes in the row's own directory (``R7_FAULT_DIR``): that
lifetime's pid and kernel start ticks, its role at the time of the call, the
digest of its browser marker, and the immediate caller being the public drain
of that module. Anything else is inert: no activation, another lifetime (a
successor, the preservation session). A mismatch in the original lifetime is
recorded as invalid and changes nothing. The first eligible entry takes an
exclusive claim; a later one is a duplicate, recorded, and changes nothing.

**What is changed.** Only a real True, and only after the claim was written in
full and the outcome published by replacement: then False is returned. A real
False or an exception passes unchanged and invalidates the calibration. A
failure to read, claim or write never alters the product's result; it only
loses evidence, and missing evidence invalidates the row.

**Two logger events** are observed as well, and changed in nothing: the core
close consuming the drain's False and the driver keeping the lease. Each record
names the lifetime that reached the logging call. None of these records is a
complete stream of anything; each certifies one invocation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from typing import Any

FAULT_DIR_ENV = "R7_FAULT_DIR"
MODULE = "linkedin_mcp_server.process_tree"
PRIVATE = "_drain_marked_posix_groups"
PUBLIC = "drain_browser_process_marker"
ROLE_MODULE = "linkedin_mcp_server.server_role"

ACTIVATION = "activation.json"
CLAIM = "claim.json"
OUTCOME = "outcome.json"
EVENTS = "events.jsonl"
INVALID_PREFIX = "invalid-"

#: The two logger events observed, by logger name and exact message template.
CONSUMED = "consumed-false"
HELD = "lease-held"
_EVENTS = {
    (
        "linkedin_mcp_server.core.browser",
        "Browser processes from this launch are still running after close, so "
        "the shutdown stays unconfirmed.",
    ): CONSUMED,
    (
        "linkedin_mcp_server.drivers.browser",
        "Browser shutdown could not be confirmed; keeping the profile lease "
        "until this process exits.",
    ): HELD,
}


def marker_digest(marker: str) -> str:
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()


def self_identity() -> tuple[int, int | None]:
    """This process's pid and its start time in kernel ticks; None off Linux."""
    try:
        with open("/proc/self/stat", encoding="ascii") as stream:
            text = stream.read()
        # Fields after the command name's closing parenthesis start at field 3;
        # the start time is field 22.
        return os.getpid(), int(text[text.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return os.getpid(), None


def _role() -> str | None:
    """The product's role for this process, asked at the time of the call."""
    roles = sys.modules.get(ROLE_MODULE)
    if roles is None:
        return None
    return roles.process_role().value


def _write_new(path: str, payload: dict) -> bool:
    """Create *path* exclusively and write *payload* in full; False if not."""
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0), 0o600
    )
    try:
        data = json.dumps(payload).encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
    finally:
        os.close(descriptor)
    return True


class Fault:
    """The installed fault for one process, over one row directory."""

    def __init__(
        self,
        directory: str,
        *,
        identity=self_identity,
        role=_role,
        monotonic_ns=time.monotonic_ns,
    ) -> None:
        self.directory = directory
        self.identity = identity
        self.role = role
        self.monotonic_ns = monotonic_ns
        #: The real private drain, the public drain's code and its globals,
        #: set by :meth:`install`.
        self.original: Any = None
        self.public_code: Any = None
        self.module_globals: Any = None

    # --- installation -----------------------------------------------------------

    def install(self, module) -> None:
        """Replace the private drain global of *module*, once."""
        namespace = module.__dict__
        current = namespace[PRIVATE]
        if getattr(current, "__r7_fault__", False):
            return
        self.original = current
        self.public_code = namespace[PUBLIC].__code__
        self.module_globals = namespace

        def _drain_marked_posix_groups(marker, deadline):
            return self.drain(marker, deadline)

        setattr(_drain_marked_posix_groups, "__r7_fault__", True)
        namespace[PRIVATE] = _drain_marked_posix_groups

    # --- the selected call ----------------------------------------------------------

    def drain(self, marker, deadline):
        # Before any file is touched: an entry cannot be dated later than it was.
        entered_ns = self.monotonic_ns()
        caller = sys._getframe(2)
        selected = None
        try:
            selected = self._select(marker, entered_ns, caller)
        except Exception as exc:  # noqa: BLE001 - evidence only, never the result
            self._invalid(entered_ns, f"selection failed: {exc!r}")
            selected = None
        try:
            result = self.original(marker, deadline)
        except BaseException as exc:
            if selected is not None:
                self._outcome(selected, real=f"raised {exc!r}")
            raise
        if selected is None:
            return result
        if result is True and self._outcome(selected, real=True):
            return False
        if result is not True:
            self._outcome(selected, real=result)
        return result

    def _select(self, marker, entered_ns, caller):
        path = os.path.join(self.directory, ACTIVATION)
        try:
            with open(path, encoding="utf-8") as stream:
                activation = json.load(stream)
        except FileNotFoundError:
            return None
        pid, start = self.identity()
        if start is None or [pid, start] != [
            activation.get("pid"),
            activation.get("start_ticks"),
        ]:
            return None  # another lifetime: a successor, a later session
        problems = []
        if caller.f_code is not self.public_code or caller.f_globals is not (
            self.module_globals
        ):
            problems.append(
                f"called from {caller.f_code.co_name}, not the public drain"
            )
        if marker_digest(marker) != activation.get("marker_digest"):
            problems.append("the marker is not the activated launch's")
        role = self.role()
        if role != activation.get("role"):
            problems.append(f"the role is {role!r}, not {activation.get('role')!r}")
        if problems:
            self._invalid(entered_ns, "; ".join(problems))
            return None
        claim = {
            "nonce": activation.get("nonce"),
            "pid": pid,
            "start_ticks": start,
            "marker_digest": activation.get("marker_digest"),
            "role": role,
            "entered_ns": entered_ns,
            "thread": threading.get_ident(),
        }
        try:
            _write_new(os.path.join(self.directory, CLAIM), claim)
        except FileExistsError:
            self._invalid(entered_ns, "a second eligible entry: the claim was taken")
            return None
        except OSError as exc:
            self._invalid(entered_ns, f"the claim could not be written: {exc!r}")
            return None
        return claim

    def _outcome(self, claim, *, real) -> bool:
        """Publish the real outcome by replacement; whether that succeeded."""
        final = os.path.join(self.directory, OUTCOME)
        temporary = f"{final}.{claim['entered_ns']}.tmp"
        try:
            _write_new(
                temporary,
                {
                    "nonce": claim["nonce"],
                    "entered_ns": claim["entered_ns"],
                    "real": real,
                    "returned_ns": self.monotonic_ns(),
                },
            )
            os.replace(temporary, final)
        except Exception:  # noqa: BLE001 - an unpublished outcome changes nothing
            return False
        return True

    def _invalid(self, entered_ns, reason) -> None:
        try:
            pid, start = self.identity()
            _write_new(
                os.path.join(
                    self.directory, f"{INVALID_PREFIX}{os.getpid()}-{entered_ns}.json"
                ),
                {
                    "pid": pid,
                    "start_ticks": start,
                    "entered_ns": entered_ns,
                    "reason": reason,
                },
            )
        except Exception:  # noqa: BLE001 - evidence only
            pass

    # --- the two logger events ----------------------------------------------------

    def observe(self, logging):
        """Record the two events; the filter always returns True, and is returned."""

        def observed(entry):
            try:
                event = _EVENTS.get((entry.name, entry.msg))
                if event is not None:
                    pid, start = self.identity()
                    line = json.dumps(
                        {
                            "event": event,
                            "pid": pid,
                            "start_ticks": start,
                            "monotonic_ns": self.monotonic_ns(),
                        }
                    )
                    with open(
                        os.path.join(self.directory, EVENTS), "a", encoding="utf-8"
                    ) as stream:
                        stream.write(line + "\n")
            except Exception:  # noqa: BLE001 - evidence only
                pass
            return True

        for name in sorted({logger for logger, _ in _EVENTS}):
            logging.getLogger(name).addFilter(observed)
        return observed


class _AfterImport:
    """Install the fault once the product itself has imported its module."""

    def __init__(self, fault: Fault) -> None:
        self.fault = fault

    def find_spec(self, name, path=None, target=None):
        if name != MODULE:
            return None
        spec = None
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None)
            if isinstance(finder, _AfterImport) or find is None:
                continue
            spec = find(name, path, target)
            if spec is not None:
                break
        if spec is None or not hasattr(spec.loader, "exec_module"):
            return spec
        spec.loader = _Loader(spec.loader, self.fault)
        return spec


class _Loader:
    def __init__(self, loader, fault: Fault) -> None:
        self.loader = loader
        self.fault = fault

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        # Back to the real loader before the module runs, so nothing the
        # module or anyone else later asks of its loader meets this one.
        module.__loader__ = self.loader
        module.__spec__.loader = self.loader
        self.loader.exec_module(module)
        try:
            self.fault.install(module)
        except Exception as exc:  # noqa: BLE001 - evidence only
            self.fault._invalid(self.fault.monotonic_ns(), f"install failed: {exc!r}")

    def __getattr__(self, name):
        return getattr(self.loader, name)


#: This process's fault, once armed.
_ARMED: list[Fault] = []


def arm(environ=os.environ) -> Fault | None:
    """Arm this process when the row named its directory; from the ``.pth``.

    Once per process: ``site`` can process a venv's site directory twice (the
    venv step and the main step both add it), and so run the ``.pth`` twice.
    """
    if _ARMED:
        return _ARMED[0]
    directory = environ.get(FAULT_DIR_ENV)
    if not directory:
        return None
    fault = Fault(directory)
    _ARMED.append(fault)
    module = sys.modules.get(MODULE)
    if module is not None:
        fault.install(module)
    else:
        sys.meta_path.insert(0, _AfterImport(fault))
    try:
        import logging

        fault.observe(logging)
    except Exception:  # noqa: BLE001 - evidence only
        pass
    return fault
