"""Name the unavailable Visual C++ runtime instead of letting a DLL error stand.

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

The cause is narrowed, never proven, and the message says only what the two
checks establish. ``DLL load failed`` is what CPython writes in
``dynload_win.c`` for every ``LoadLibraryExW`` that fails, so on its own it also
covers a corrupt ``.pyd``, an architecture mismatch and a different missing
dependency; the operating-system text after the colon does not name which module
was not found. Asking the loader for ``MSVCP140.dll`` directly answers a second
question, and if it loads, the original error is re-raised untouched.

There is deliberately no third check on the installed greenlet version, and the
reason is worth keeping because the check was written and removed. Linking is a
property of the built artifact, not of the number: 3.2.5 publishes no Windows
wheel at all, so a Windows install of it comes from the sdist and is linked
dynamically unless whoever built it set ``GREENLET_STATIC_RUNTIME``, while a
release carrying upstream #526 will be static at a version above every one
measured here. A version predicate is therefore wrong in both directions, and it
fails in the expensive one: it withholds the explanation from someone whose
problem this is. The message names the versions that were measured and leaves
the reader to place their own build against them.

What the DLL probe establishes is that the runtime cannot be *loaded*, which is
weaker than absent. ``ctypes`` reports a wrong-architecture copy, a corrupt
file, a failed initialiser and an unloadable dependency of its own as the same
``OSError``, so none of those can be told apart from nothing being installed.
The message therefore says the loader cannot produce it and quotes what the
loader said, rather than claiming the machine has none.

The search orders also differ slightly. CPython loads an extension with
``LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR``
(``dynload_win.c``), while ``ctypes.CDLL`` given a bare name uses
``LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`` alone and only adds the second flag for a
name containing a path separator (``Lib/ctypes/__init__.py``). The difference is
the directory holding the ``.pyd``, so a ``MSVCP140.dll`` sitting beside
``_greenlet.pyd`` and nowhere else is found by the real import and missed by
this probe.

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

from linkedin_mcp_server.exceptions import VisualCPPRuntimeUnavailableError

#: How CPython opens the message for a failed ``LoadLibraryExW``, in both the
#: ``while importing X:`` and the ``with error code N`` shapes. Matched as a
#: prefix rather than anywhere in the text, so a wrapper that merely mentions
#: those words in a diagnostic of its own is not mistaken for the loader.
_DLL_LOAD_FAILED = "DLL load failed"

#: The C++ standard library from the redistributable. ``VCRUNTIME140*`` are
#: imported too, but those the Python distributions do ship, so this one is
#: what actually decides.
_RUNTIME_DLL = "msvcp140.dll"


def _the_runtime_cannot_be_loaded() -> bool:
    """Whether the loader refuses to produce the C++ runtime.

    Asked of the loader rather than of a path, so it answers with roughly the
    search order that failed for ``_greenlet.pyd``. ``CDLL`` rather than
    ``WinDLL`` because the two differ only in calling convention and nothing is
    ever called through this handle.

    Deliberately not named after absence. Every native loader failure arrives as
    ``OSError``, so a wrong-architecture or corrupt copy on the search path is
    indistinguishable here from no copy at all.
    """
    try:
        ctypes.CDLL(_RUNTIME_DLL)
    except OSError:
        return True
    return False


_UNKNOWN = "unknown"


def _installed_greenlet() -> str:
    """The greenlet version on disk, which is readable even when it will not load.

    Named in the message rather than acted on, so the reader can place their own
    build against the versions that were measured. Every failure here is
    swallowed: this runs inside an import that is already failing, and a broken
    ``METADATA`` masking the real error with a decoding problem would be a worse
    outcome than an unnamed version.
    """
    try:
        return version("greenlet") or _UNKNOWN
    except Exception:
        return _UNKNOWN


def _explain(loader_said: str) -> str:
    return f"""greenlet could not load its C extension, and {_RUNTIME_DLL} cannot
be loaded on this machine either. The loader reported:

  {loader_said}

Installed greenlet: {_installed_greenlet()}

The Windows wheels for greenlet 3.3.1 through 3.5.4 link the Visual C++ runtime
dynamically, so _greenlet.pyd needs MSVCP140.dll. It comes with the
Microsoft Visual C++ Redistributable, and installing that is the fix when the
machine has none.

Two things this cannot tell you. If the redistributable is already installed,
the copy the loader reaches will not load for some other reason, and the line
above is what it said. And if the greenlet above is 3.3.0 or older, its wheel
carries its own C++ runtime, so the failure is probably something else.

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
    nothing when greenlet imports normally. Two things have to hold before the
    error is translated: the failure came from the loader, and the loader cannot
    produce the C++ runtime when asked for it. Anything else is re-raised as it
    came.
    """
    if sys.platform != "win32":
        return

    try:
        import greenlet  # noqa: F401
    except ImportError as exc:
        if not str(exc).startswith(_DLL_LOAD_FAILED):
            raise
        if not _the_runtime_cannot_be_loaded():
            raise
        raise VisualCPPRuntimeUnavailableError(_explain(str(exc))) from exc
