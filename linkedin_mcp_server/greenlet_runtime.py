"""Name the unavailable Visual C++ runtime instead of letting a DLL error stand.

greenlet is not ours and we never call it. patchright imports it unconditionally
from ``_impl/_connection.py``, on the async-only path too, so its C extension is
loaded in every run this server makes and a failure there stops the server
before its entry point runs.

Some of its Windows wheels need a DLL the machine may not have. greenlet built
them on Appveyor with ``GREENLET_STATIC_RUNTIME=1``, which ``setup.py`` turns
into ``/MT``; the move to GitHub Actions dropped the variable and nothing
replaced it. Measured on the published ``cp312-win_amd64`` wheels, reading the
PE import table of ``greenlet/_greenlet.cp312-win_amd64.pyd``:

    3.2.4, 3.3.0       KERNEL32.dll, python312.dll
    3.3.1 .. 3.5.4     + MSVCP140.dll, VCRUNTIME140.dll, VCRUNTIME140_1.dll

``MSVCP140.dll`` comes with the Microsoft Visual C++ Redistributable and with
neither distribution this server is normally started from, the python.org
package and the python-build-standalone builds ``uv`` installs. Reported as
python-greenlet/greenlet#525, fix in #526, which does not retire this module:
the affected wheels stay on PyPI for good.

Three decisions worth stating, because each looks like something else:

Nothing here consults the installed greenlet version, and the reason is worth
keeping because that check was written and removed. Linking is a property of the
built artifact, not of the number: 3.2.5 publishes no Windows wheel at all, so a
Windows install of it comes from the sdist and is linked dynamically unless
whoever built it set ``GREENLET_STATIC_RUNTIME``, while a release carrying #526
will be static above every version measured above. A version predicate is wrong
in both directions and fails in the expensive one, withholding the explanation
from someone whose problem this is.

The two checks narrow the cause and never prove it. ``DLL load failed`` is what
CPython writes in ``dynload_win.c`` for every ``LoadLibraryExW`` that fails, and
the operating-system text after the colon does not name the module it could not
find, so that prefix alone also covers a corrupt ``.pyd`` and an architecture
mismatch. Asking the loader for ``MSVCP140.dll`` answers a second question, and
the search orders differ slightly: CPython loads an extension with
``LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR``
(``dynload_win.c``) while ``ctypes.CDLL`` given a bare name uses the first flag
alone (``Lib/ctypes/__init__.py``), so a ``MSVCP140.dll`` sitting beside
``_greenlet.pyd`` and nowhere else is found by the real import and missed here.

The probe runs at package import, which is charged to every other import of this
package on Windows and is accepted for one reason: it is the only place both
entry paths pass through before ``cli_main`` reaches patchright, so no third
entry point can be added around it.
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
    """The message a Windows user sees, with the thing to do at the top.

    Ordered for a client that truncates or collapses a server's output: what to
    install comes before why, and the evidence follows underneath.
    """
    return f"""greenlet could not load its C extension, and {_RUNTIME_DLL} cannot
be loaded on this machine either.

To fix this:
  Install the Microsoft Visual C++ Redistributable, then start the server again
  https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist

Without administrator rights this usually works instead. The published wheels up
to greenlet 3.3.0 carry the runtime inside the extension, and they are x86-64
only:
  uvx --with "greenlet<=3.3.0" mcp-server-linkedin@latest

The loader reported:
  {loader_said}
Installed greenlet: {_installed_greenlet()}

If the redistributable is already installed, the copy the loader reaches will
not load for some other reason, and the loader line above is what it said.
Background: https://github.com/python-greenlet/greenlet/issues/525"""


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
