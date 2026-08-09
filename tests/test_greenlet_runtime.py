"""What a Windows machine whose loader cannot produce MSVCP140.dll is told."""

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


def _unwrapped(value: object) -> str:
    """One line and lowercased, so an assertion survives a rewrap or a recase.

    Neither changes what the message tells anyone, and Windows itself does not
    distinguish ``MSVCP140.dll`` from ``msvcp140.dll``.
    """
    return " ".join(str(value).split()).lower()


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

        message = _unwrapped(caught.value)
        assert "msvcp140.dll" in message
        assert "microsoft visual c++ redistributable" in message
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
        # too, and claiming a missing redistributable when the loader can
        # produce one would send that user after the wrong thing.
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

        # Chained, not swallowed: whoever reads the traceback still sees the
        # loader's own words, which are the only machine-specific detail here.
        # They name the extension being imported and never the dependency that
        # could not be found, which is why the second probe exists at all.
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

    def test_the_loader_has_to_be_the_one_talking(self, greenlet_fails):
        # A prefix, not a substring anywhere in the text. A wrapper mentioning
        # those words in a diagnostic of its own is not the loader.
        greenlet_fails(
            ImportError("wrapper failure mentions DLL load failed in its notes")
        )

        with pytest.raises(ImportError) as caught:
            explain_a_missing_runtime()

        assert not isinstance(caught.value, VisualCPPRuntimeUnavailableError)

    @pytest.mark.parametrize(
        "shape",
        [
            "DLL load failed while importing _greenlet: nope",
            "DLL load failed with error code 126 while importing _greenlet",
        ],
    )
    def test_both_shapes_cpython_writes_are_recognised(self, greenlet_fails, shape):
        greenlet_fails(ImportError(shape))

        with pytest.raises(VisualCPPRuntimeUnavailableError):
            explain_a_missing_runtime()

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

    @pytest.mark.parametrize("installed", ["3.2.4", "3.3.0", "3.2.5", "3.5.4"])
    def test_no_version_decides_whether_to_explain(
        self, monkeypatch, greenlet_fails, installed
    ):
        # Linking belongs to the built artifact, not the number. 3.2.5 publishes
        # no Windows wheel at all, so an install of it is built from the sdist
        # and is dynamic unless whoever built it set GREENLET_STATIC_RUNTIME. A
        # predicate on the version withholds the explanation from exactly that
        # person, which is the expensive direction to be wrong in.
        monkeypatch.setattr(greenlet_runtime, "version", lambda _name: installed)
        greenlet_fails(ImportError(_REAL_MESSAGE))

        with pytest.raises(VisualCPPRuntimeUnavailableError):
            explain_a_missing_runtime()

    @pytest.mark.parametrize("other", ["3.2.5", "3.3.0", "unknown"])
    def test_the_diagnosis_reads_the_same_at_every_version(self, monkeypatch, other):
        # The predicate was taken out of the control flow because a version
        # cannot say how an artifact was linked. Saying it in prose instead
        # would put the same wrong claim in front of the reader: telling a
        # 3.2.5 user that "its wheel" carries the runtime describes a wheel that
        # was never published. Only the reported version may differ, so the
        # version is masked rather than the line dropped, and a clause smuggled
        # in beside it still fails this.
        def masked(installed: str) -> str:
            monkeypatch.setattr(greenlet_runtime, "version", lambda _name: installed)
            body = greenlet_runtime._explain("DLL load failed while importing x: y")
            return " ".join(
                body.replace(
                    f"Installed greenlet: {installed}", "Installed greenlet: <version>"
                ).split()
            )

        assert masked(other) == masked("3.5.4")

    def test_the_remedy_comes_before_the_history(self):
        # Clients truncate and collapse a server's output. Someone who reads
        # only the first lines has to reach the thing to install.
        body = greenlet_runtime._explain("DLL load failed while importing x: y")

        # Not merely before the evidence: history prepended above it would keep
        # that true while pushing the instruction out of a truncated view.
        assert body.splitlines().index("To fix this:") < 4
        assert "latest-supported-vc-redist" in _unwrapped(body)
        assert "published wheels up to greenlet 3.3.0" in _unwrapped(body)

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
        assert "msvcp140.dll" in finished.stderr.lower()
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
