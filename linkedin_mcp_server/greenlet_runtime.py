"""Name the missing Visual C++ runtime instead of letting a DLL error stand.

greenlet is not ours and we never call it. patchright imports it unconditionally
from ``_impl/_connection.py``, on the async-only path too, so its C extension is
loaded in every run this server makes and a failure there stops the server
before any of our code executes.

Since 3.3.1 that extension needs a DLL the machine may not have. greenlet built
its Windows wheels on Appveyor with ``GREENLET_STATIC_RUNTIME=1``, which
``setup.py`` turns into ``/MT``; the move to GitHub Actions dropped the variable
and nothing replaced it, so the C++ runtime is linked dynamically now. Measured
on the published ``cp312-win_amd64`` wheels, reading the PE import table of
``greenlet/_greenlet.cp312-win_amd64.pyd``:

    3.2.4              KERNEL32.dll, python312.dll
    3.3.0              KERNEL32.dll, python312.dll
    3.3.1 .. 3.5.4     + MSVCP140.dll, VCRUNTIME140.dll, VCRUNTIME140_1.dll

``MSVCP140.dll`` ships with the Microsoft Visual C++ Redistributable. It does
not ship with either distribution this server is normally started from: the
python.org embeddable package and the python-build-standalone builds ``uv``
installs both carry ``vcruntime140.dll`` and ``vcruntime140_1.dll`` and stop
there. Not a universal claim about Python on Windows -- a conda environment
pulls ``vc14_runtime``, which does carry it -- and not a claim that the
interpreter is sealed off either, since the loader still searches ``System32``
and finds a system-wide redistributable there. It is a statement about what the
tree brings with it when nothing else on the machine has ever installed one,
which is the situation a ``uvx`` user is most likely to be in.

Reported upstream as python-greenlet/greenlet#525 with the fix in #526. That
does not retire this module. The affected wheels stay on PyPI for good, and any
resolver that lands on one of them reproduces the failure years from now.

Three decisions worth stating, because each looks like something else:

The cause is measured, not inferred from the message. ``DLL load failed`` is
what CPython writes in ``dynload_win.c`` for every ``LoadLibraryExW`` that
fails, so on its own it also covers a corrupt ``.pyd``, an architecture
mismatch, and a different missing dependency; the operating-system text after
the colon does not name which module was not found. Reading that prefix
therefore only says a load failed, and the question of *why* is answered by
asking the loader for ``MSVCP140.dll`` directly. If it loads, this is some other
failure and the original error is re-raised untouched, which matters most for
the 3.3.0 stopgap suggested below: that build needs no C++ runtime at all, so
translating its DLL errors would be certainly wrong. The one imprecision left is
that a ``.pyd`` is loaded with ``LOAD_WITH_ALTERED_SEARCH_PATH`` and so also
searches its own directory, while this probe does not; a ``MSVCP140.dll`` placed
next to ``_greenlet.pyd`` and nowhere else would be missed.

The probe imports greenlet eagerly rather than waiting for patchright to do it.
It has to, because the failure happens while importing ``cli_main`` -- through
``drivers.browser`` to ``core.browser`` to ``patchright.async_api`` -- which is
before ``main()`` exists to wrap anything in. The package ``__init__`` is the one
place both entry paths pass through first, the console script and ``python -m``
alike. On the path that matters the probe is free: patchright imports the same
module seconds later and finds it in ``sys.modules``.

That placement is charged to every other import of this package too, and the
cost is accepted rather than overlooked. On an affected machine, importing a
browser-free module such as ``session_state`` now raises where it used to work,
and a Windows contributor without the redistributable sees pytest fail during
collection rather than at the first test that needs a browser. Both are true,
and both describe a machine on which the server itself cannot start. Guarding
the two entry points instead would spare them, at the price of a guard that a
third entry point can be added around without anyone noticing, which is the
failure this exists to prevent. One unconditional check is worth an error
message on a machine that was already broken.
"""

import ctypes
import sys
from importlib.metadata import version

from linkedin_mcp_server.exceptions import VisualCPPRuntimeMissingError

_DLL_LOAD_FAILED = "DLL load failed"

#: The C++ standard library from the redistributable. ``VCRUNTIME140*`` are
#: imported too, but those the Python distributions do ship, so this one is
#: what actually decides.
_RUNTIME_DLL = "msvcp140.dll"


def _the_runtime_is_absent() -> bool:
    """Whether the loader can find the C++ runtime at all.

    Asked of the loader rather than of a path, so it answers with the same
    search order that failed for ``_greenlet.pyd``. ``CDLL`` rather than
    ``WinDLL`` because the two differ only in calling convention and nothing is
    ever called through this handle.
    """
    try:
        ctypes.CDLL(_RUNTIME_DLL)
    except OSError:
        return True
    return False


def _installed_greenlet() -> str:
    """The greenlet version on disk, which is readable even when it will not load.

    Every failure here is swallowed. This runs inside an import that is already
    failing, and a broken ``METADATA`` masking the real error with a decoding
    problem would be a worse outcome than an unnamed version.
    """
    try:
        return version("greenlet") or "unknown"
    except Exception:
        return "unknown"


def _explain(loader_said: str) -> str:
    return f"""greenlet could not load its C extension, and {_RUNTIME_DLL} cannot
be loaded on this machine either. The loader reported:

  {loader_said}

Installed greenlet: {_installed_greenlet()}

greenlet 3.3.1 through 3.5.4 link the Visual C++ runtime dynamically, so
_greenlet.pyd needs MSVCP140.dll. It comes with the
Microsoft Visual C++ Redistributable, which nothing on this machine has
installed.

To fix this:
  Install the redistributable, then start the server again
  https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist

Without administrator rights, an older greenlet works as a stopgap. It links
that runtime statically and needs none of this
(x86-64 only, the older wheels have no ARM64 build):
  uvx --with "greenlet<=3.3.0" mcp-server-linkedin

Tracked upstream at https://github.com/python-greenlet/greenlet/issues/525"""


def explain_a_missing_runtime() -> None:
    """Raise a message naming the redistributable when greenlet cannot load.

    Does nothing off Windows, where the dynamic link is not a problem, and
    nothing when greenlet imports normally. An import failure that is not the
    loader's is re-raised as it came, and so is a load that failed while the
    runtime is present, because then it is not this problem.
    """
    if sys.platform != "win32":
        return

    try:
        import greenlet  # noqa: F401
    except ImportError as exc:
        if _DLL_LOAD_FAILED not in str(exc):
            raise
        if not _the_runtime_is_absent():
            raise
        raise VisualCPPRuntimeMissingError(_explain(str(exc))) from exc
