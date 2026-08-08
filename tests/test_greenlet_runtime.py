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

import subprocess
import sys
import tomllib
from ctypes.util import find_library
from importlib.metadata import version
from pathlib import Path
from types import ModuleType

import pytest

from linkedin_mcp_server import greenlet_runtime
from linkedin_mcp_server.exceptions import VisualCPPRuntimeUnavailableError
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
        # Stated rather than inherited from the host. Off Windows the real probe
        # answers True because no machine here has an msvcp140.dll at all, which
        # is the right answer for the wrong reason; on a healthy Windows box it
        # answers False and every test below would fail.
        monkeypatch.setattr(
            greenlet_runtime, "_the_runtime_cannot_be_loaded", lambda: True
        )

    def test_a_dll_failure_names_the_runtime(self, greenlet_fails):
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(VisualCPPRuntimeUnavailableError) as caught:
            explain_a_missing_runtime()

        message = str(caught.value)
        assert "MSVCP140.dll" in message
        assert "Microsoft Visual C++ Redistributable" in message
        assert "latest-supported-vc-redist" in message

    def test_the_loader_is_quoted(self, greenlet_fails):
        greenlet_fails(ImportError("DLL load failed while importing _greenlet: bogus"))

        with pytest.raises(VisualCPPRuntimeUnavailableError) as caught:
            explain_a_missing_runtime()

        assert "bogus" in str(caught.value)

    def test_a_present_runtime_means_this_is_a_different_problem(
        self, monkeypatch, greenlet_fails
    ):
        # ``DLL load failed`` is what CPython writes for every LoadLibraryExW
        # that fails, so a corrupt or architecture-mismatched .pyd reaches here
        # too. Claiming a missing redistributable when the loader can produce it
        # would send that user after the wrong thing, and it would be worst on
        # the 3.3.0 stopgap this very message suggests, which links the runtime
        # statically and needs none of it.
        monkeypatch.setattr(
            greenlet_runtime, "_the_runtime_cannot_be_loaded", lambda: False
        )
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(ImportError) as caught:
            explain_a_missing_runtime()

        assert not isinstance(caught.value, VisualCPPRuntimeUnavailableError)
        assert str(caught.value) == _REAL_MESSAGE

    def test_an_unreadable_version_does_not_mask_the_failure(
        self, monkeypatch, greenlet_fails
    ):
        # This runs inside an import that is already failing. A broken METADATA
        # raising through the message builder would replace the useful error
        # with a decoding one.
        def boom(_name: str) -> str:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "broken METADATA")

        monkeypatch.setattr(greenlet_runtime, "version", boom)
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(VisualCPPRuntimeUnavailableError) as caught:
            explain_a_missing_runtime()

        assert "Installed greenlet: unknown" in str(caught.value)

    def test_the_installed_version_is_named(self, greenlet_fails):
        # The measured range is 3.3.1 through 3.5.4, so the reader needs to know
        # which version is on disk to place it against that.
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(VisualCPPRuntimeUnavailableError) as caught:
            explain_a_missing_runtime()

        assert f"Installed greenlet: {version('greenlet')}" in str(caught.value)

    def test_the_original_error_is_kept(self, greenlet_fails):
        original = ImportError(_REAL_MESSAGE)
        greenlet_fails(original)

        with pytest.raises(VisualCPPRuntimeUnavailableError) as caught:
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

        with pytest.raises(VisualCPPRuntimeUnavailableError) as caught:
            explain_a_missing_runtime()

        assert "greenlet<=3.3.0" in str(caught.value)

    def test_a_missing_greenlet_is_not_given_the_wrong_advice(self, greenlet_fails):
        # An uninstalled greenlet is a different problem with a different fix,
        # and a redistributable would not touch it.
        greenlet_fails(ImportError("No module named 'greenlet'"))

        with pytest.raises(ImportError) as caught:
            explain_a_missing_runtime()

        assert not isinstance(caught.value, VisualCPPRuntimeUnavailableError)
        assert "No module named" in str(caught.value)

    def test_an_importable_greenlet_says_nothing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "greenlet", ModuleType("greenlet"))

        assert explain_a_missing_runtime() is None

    @pytest.mark.parametrize("installed", ["3.2.4", "3.3.0", "3.3.0.post1"])
    def test_a_statically_linked_greenlet_is_left_alone(
        self, monkeypatch, greenlet_fails, installed
    ):
        # 3.3.0 is the stopgap this very message recommends. It carries its own
        # C++ runtime, so a DLL failure there is never about the redistributable
        # and saying so would send that user after the wrong thing.
        monkeypatch.setattr(greenlet_runtime, "version", lambda _name: installed)
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(ImportError) as caught:
            explain_a_missing_runtime()

        assert not isinstance(caught.value, VisualCPPRuntimeUnavailableError)

    @pytest.mark.parametrize(
        "installed",
        [
            # The commit that dropped GREENLET_STATIC_RUNTIME carried this
            # version, and PEP 440 sorts it below 3.3.1, so a boundary written
            # as >= 3.3.1 would let the very first affected build through.
            "3.3.1.dev0",
            "3.3.1rc1",
            "3.3.1",
            "3.5.4",
            # Not a claim that this links dynamically. The check excludes the
            # builds measured to be static and leaves everything else in.
            "4.0.0",
        ],
    )
    def test_a_greenlet_that_is_not_known_to_be_static_is_explained(
        self, monkeypatch, greenlet_fails, installed
    ):
        monkeypatch.setattr(greenlet_runtime, "version", lambda _name: installed)
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(VisualCPPRuntimeUnavailableError):
            explain_a_missing_runtime()

    @pytest.mark.parametrize("installed", ["unknown", "not-a-version"])
    def test_an_unusable_version_still_gets_the_message(
        self, monkeypatch, greenlet_fails, installed
    ):
        # Withholding the explanation because the metadata is damaged would
        # withhold it exactly where the install is already in trouble.
        monkeypatch.setattr(greenlet_runtime, "version", lambda _name: installed)
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(VisualCPPRuntimeUnavailableError):
            explain_a_missing_runtime()


class TestTheProbeItself:
    """The one part that talks to the real loader, so no test may stub it out."""

    def test_a_library_that_is_not_there_reads_as_unloadable(self, monkeypatch):
        monkeypatch.setattr(
            greenlet_runtime, "_RUNTIME_DLL", "no-such-library-4c1f9a.dll"
        )

        assert greenlet_runtime._the_runtime_cannot_be_loaded() is True

    def test_a_library_that_is_there_reads_as_loadable(self, monkeypatch):
        # Whatever this host can actually load, so the False branch is exercised
        # on every platform rather than only where msvcp140.dll exists.
        # ``find_library("c")`` answers None on musl, so it cannot be the only
        # candidate: on Alpine an assert would turn a covered branch into a
        # broken test.
        candidates = (
            ["kernel32.dll"]
            if sys.platform == "win32"
            else [find_library("c"), "libc.so.6", "libc.musl-x86_64.so.1"]
        )
        for present in filter(None, candidates):
            monkeypatch.setattr(greenlet_runtime, "_RUNTIME_DLL", present)
            if greenlet_runtime._the_runtime_cannot_be_loaded() is False:
                return
        pytest.skip("no reference library this platform will load")


class TestElsewhere:
    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_the_probe_does_not_run(self, monkeypatch, greenlet_fails, platform):
        # The manylinux and macOS wheels never grew this dependency, so paying
        # for an eager C-extension import off Windows would buy nothing.
        monkeypatch.setattr(sys, "platform", platform)
        greenlet_fails(ImportError(_REAL_MESSAGE))

        assert explain_a_missing_runtime() is None


#: Refuses greenlet the way the loader would, then takes one of the two routes
#: an installed server is started by. Run in a child process because both routes
#: import this package for real, and because ``sys.platform`` has to be a lie
#: the whole process believes.
_ENTRY_PATH_SCRIPT = """
import sys

# Loaded before the platform is renamed. Both branch on it at import time and
# reach for _winapi, which does not exist on the machine running this test. The
# server's own import chain raises in the package __init__ before it can get far
# enough to care.
import runpy
import shutil
import importlib.metadata

class Refusing:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "greenlet":
            raise ImportError(
                "DLL load failed while importing _greenlet: "
                "The specified module could not be found."
            )
        return None

# The runtime's presence is stated, not inherited from the machine running
# this test. Without it the child would agree for the wrong reason off
# Windows, and disagree on a healthy Windows host.
import ctypes
_real_cdll = ctypes.CDLL

def _cdll(name, *args, **kwargs):
    if str(name).lower() == "msvcp140.dll":
        if {runtime_loads}:
            return _real_cdll(_reference_library)
        raise OSError("[WinError 126] The specified module could not be found")
    return _real_cdll(name, *args, **kwargs)

_reference_library = "kernel32.dll" if {on_windows} else __import__(
    "ctypes.util", fromlist=["find_library"]
).find_library("c")
ctypes.CDLL = _cdll

sys.platform = "win32"
sys.meta_path.insert(0, Refusing())
{body}
"""

_ENTRY_PATHS = {
    # What the console script generated from pyproject.toml does.
    "console_script": "from linkedin_mcp_server.cli_main import main",
    # What ``python -m linkedin_mcp_server`` does, which is also the MCPB
    # manifest command and the Dockerfile CMD.
    "python_m": "runpy.run_module('linkedin_mcp_server', run_name='__main__')",
}


class TestBothEntryPaths:
    """Started for real, because the point is an import that happens too early.

    A test that reads ``__init__`` and looks for the call proves the call is
    written down, not that it runs before patchright is reached. Someone adding
    an import above it would keep such a test green and the server would die on
    the bare DLL error again.
    """

    def _run(self, route: str, runtime_loads: bool) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                _ENTRY_PATH_SCRIPT.format(
                    body=_ENTRY_PATHS[route],
                    runtime_loads=runtime_loads,
                    on_windows=repr(sys.platform == "win32"),
                ),
            ],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
        )

    @pytest.mark.parametrize("route", sorted(_ENTRY_PATHS))
    def test_the_guard_speaks_before_patchright_is_reached(self, route):
        finished = self._run(route, runtime_loads=False)

        assert finished.returncode != 0
        assert "VisualCPPRuntimeUnavailableError" in finished.stderr
        assert "MSVCP140.dll" in finished.stderr
        # The loader's own words survive, which is the only machine-specific
        # detail and the only way to tell this cause from another DLL failure.
        assert "DLL load failed while importing _greenlet" in finished.stderr

    @pytest.mark.parametrize("route", sorted(_ENTRY_PATHS))
    def test_a_loadable_runtime_leaves_the_error_alone(self, route):
        # What a healthy Windows machine looks like: the extension fails for
        # some other reason and the redistributable has nothing to do with it.
        finished = self._run(route, runtime_loads=True)

        assert finished.returncode != 0
        assert "VisualCPPRuntimeUnavailableError" not in finished.stderr
        assert "DLL load failed while importing _greenlet" in finished.stderr

    def test_the_console_script_still_goes_through_the_package(self):
        # The route above is only the real one while the entry point names a
        # module inside this package, which is what makes __init__ run first.
        pyproject = tomllib.loads(
            (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
        )
        target = pyproject["project"]["scripts"]["mcp-server-linkedin"]

        assert target.split(":")[0].startswith("linkedin_mcp_server.")
