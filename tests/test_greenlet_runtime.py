"""What a Windows machine without the Visual C++ runtime is told.

greenlet's C extension links that runtime dynamically since 3.3.1, so on a
machine without the redistributable it fails to load and the server stops with
``DLL load failed while importing _greenlet`` and nothing naming the cause.
patchright imports greenlet even on the async-only path this server uses, so the
failure arrives before any of our code runs and there is nothing to catch it
later.

These tests run everywhere, including on the platform that cannot reproduce the
condition, which is the point: the failure belongs to Windows and the reasoning
about it should not need one.
"""

import importlib
import sys
from types import ModuleType

import pytest

from linkedin_mcp_server.exceptions import VisualCPPRuntimeMissingError
from linkedin_mcp_server.greenlet_runtime import explain_a_missing_runtime

#: What CPython raises when a ``.pyd`` cannot find a DLL it imports. The prefix
#: is formatted by ``dynload_win.c`` and is English on every install; only the
#: operating-system text after the colon is localised.
_REAL_MESSAGE = (
    "DLL load failed while importing _greenlet: "
    "The specified module could not be found."
)


class _RefusingFinder:
    """A meta-path finder that fails one module the way the loader would."""

    def __init__(self, name: str, error: ImportError) -> None:
        self.name = name
        self.error = error

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name:
            raise self.error
        return None


@pytest.fixture
def greenlet_fails(monkeypatch):
    """Make ``import greenlet`` raise, whatever is installed in this venv."""

    def arrange(error: ImportError) -> None:
        monkeypatch.delitem(sys.modules, "greenlet", raising=False)
        monkeypatch.setattr(
            sys, "meta_path", [_RefusingFinder("greenlet", error), *sys.meta_path]
        )

    return arrange


class TestOnWindows:
    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")

    def test_a_dll_failure_names_the_runtime(self, greenlet_fails):
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(VisualCPPRuntimeMissingError) as caught:
            explain_a_missing_runtime()

        message = str(caught.value)
        assert "MSVCP140.dll" in message
        assert "Microsoft Visual C++ Redistributable" in message
        assert "latest-supported-vc-redist" in message

    def test_the_original_error_is_kept(self, greenlet_fails):
        original = ImportError(_REAL_MESSAGE)
        greenlet_fails(original)

        with pytest.raises(VisualCPPRuntimeMissingError) as caught:
            explain_a_missing_runtime()

        # Chained, not swallowed: whoever reads the traceback still sees which
        # DLL the loader named, which is the only machine-specific detail here.
        assert caught.value.__cause__ is original

    def test_a_stopgap_is_offered_for_a_machine_nobody_administers(
        self, greenlet_fails
    ):
        # Installing the redistributable needs administrator rights and a uvx
        # user may have neither them nor anyone to ask.
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(VisualCPPRuntimeMissingError) as caught:
            explain_a_missing_runtime()

        assert "greenlet<=3.3.0" in str(caught.value)

    def test_a_missing_greenlet_is_not_given_the_wrong_advice(self, greenlet_fails):
        # An uninstalled greenlet is a different problem with a different fix,
        # and a redistributable would not touch it.
        greenlet_fails(ImportError("No module named 'greenlet'"))

        with pytest.raises(ImportError) as caught:
            explain_a_missing_runtime()

        assert not isinstance(caught.value, VisualCPPRuntimeMissingError)
        assert "No module named" in str(caught.value)

    def test_an_importable_greenlet_says_nothing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "greenlet", ModuleType("greenlet"))

        assert explain_a_missing_runtime() is None


class TestElsewhere:
    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_the_probe_does_not_run(self, monkeypatch, greenlet_fails, platform):
        # The manylinux and macOS wheels never grew this dependency, so paying
        # for an eager C-extension import off Windows would buy nothing.
        monkeypatch.setattr(sys, "platform", platform)
        greenlet_fails(ImportError(_REAL_MESSAGE))

        assert explain_a_missing_runtime() is None


class TestBothEntryPaths:
    def test_the_package_probes_before_patchright_is_reachable(self):
        # The failure happens while importing cli_main, so the check has to sit
        # somewhere both the console script and ``python -m`` pass through
        # first. Only the package __init__ does.
        source = importlib.import_module("linkedin_mcp_server").__file__
        assert source is not None
        text = open(source, encoding="utf-8").read()

        assert "explain_a_missing_runtime()" in text
        assert text.index("explain_a_missing_runtime()") < text.index("__version__")
