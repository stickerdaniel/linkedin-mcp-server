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

``MSVCP140.dll`` ships with the Microsoft Visual C++ Redistributable and with no
Python distribution. Both distributions people actually run here carry
``vcruntime140.dll`` and ``vcruntime140_1.dll`` and stop there: the python.org
embeddable package, and the python-build-standalone builds ``uv`` installs. A
``uvx`` user is therefore more exposed than most, because a uv-managed Python is
a self-contained tree that never inherits anything from a system install.

Reported upstream as python-greenlet/greenlet#525 with the fix in #526. That
does not retire this module. The affected wheels stay on PyPI for good, and any
resolver that lands on one of them reproduces the failure years from now.

Two decisions worth stating, because both look like something else:

The probe imports greenlet eagerly rather than waiting for patchright to do it.
It has to, because the failure happens while importing ``cli_main`` -- through
``drivers.browser`` to ``core.browser`` to ``patchright.async_api`` -- which is
before ``main()`` exists to wrap anything in. The package ``__init__`` is the one
place both entry paths pass through first, the console script and ``python -m``
alike. On the path that matters the probe is free: patchright imports the same
module seconds later and finds it in ``sys.modules``.

Matching on ``DLL load failed`` is locale-independent despite being a text match.
CPython formats that prefix itself in ``dynload_win.c`` and it is English on
every install; only the operating-system text after the colon is translated, and
this never reads it. Anything else raised by the import is re-raised untouched,
so a greenlet that is merely absent still says so instead of being handed advice
about a redistributable.
"""

import sys

from linkedin_mcp_server.exceptions import VisualCPPRuntimeMissingError

_DLL_LOAD_FAILED = "DLL load failed"

_EXPLANATION = """greenlet could not load its C extension.

greenlet 3.3.1 and later link the Visual C++ runtime dynamically, so
_greenlet.pyd needs MSVCP140.dll. No Python distribution ships that DLL. It
comes with the Microsoft Visual C++ Redistributable.

To fix this:
  Install the redistributable, then start the server again
  https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist

Without administrator rights, an older greenlet works as a stopgap
(x86-64 only, the older wheels have no ARM64 build):
  uvx --with "greenlet<=3.3.0" mcp-server-linkedin

Tracked upstream at https://github.com/python-greenlet/greenlet/issues/525"""


def explain_a_missing_runtime() -> None:
    """Raise a message naming the redistributable when greenlet cannot load.

    Does nothing off Windows, where the dynamic link is not a problem, and
    nothing when greenlet imports normally. Any import failure that is not the
    DLL one is re-raised as it came.
    """
    if sys.platform != "win32":
        return

    try:
        import greenlet  # noqa: F401
    except ImportError as exc:
        if _DLL_LOAD_FAILED not in str(exc):
            raise
        raise VisualCPPRuntimeMissingError(_EXPLANATION) from exc
