"""The local write path must put UTF-8 on disk, not the host's locale codec.

``secure_write_text`` is handed text by callers that cannot know the host's
ANSI code page or ``LANG``: the local diagnostic note carries a LinkedIn error
message verbatim, and the daemon descriptor and portable cookie file carry
whatever a profile path or a cookie value holds. Reading them back as UTF-8 is
what the rest of the server does, so a write in the locale codec either raises
inside the caller or leaves bytes no UTF-8 reader can decode.

A character the locale codec cannot encode raises where the caller is already
handling a failure; a character it can encode succeeds and corrupts the file
silently. Both come from the same missing ``encoding``, so the write test runs
under an ASCII locale, where it fails on every POSIX host instead of only on
Windows.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics

# A C locale with UTF-8 mode and locale coercion off makes the locale codec
# ASCII on macOS and Linux, so a write without an explicit encoding fails here
# as it does under a Windows ANSI code page.
_ASCII_LOCALE = {
    "LC_ALL": "C",
    "LANG": "C",
    "PYTHONUTF8": "0",
    "PYTHONCOERCECLOCALE": "0",
}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX locale variables")
@pytest.mark.parametrize(
    "content",
    [
        pytest.param("- Error: profile \U0001f680 unavailable\n", id="emoji"),
        pytest.param("- Error: \u4e2d\u6587 profile name\n", id="cjk"),
    ],
)
def test_secure_write_text_writes_utf8_under_a_non_utf8_locale(tmp_path, content):
    """The bytes on disk do not depend on the host's locale codec."""
    path = tmp_path / "note.md"
    write = (
        "from pathlib import Path\n"
        "from linkedin_mcp_server.common_utils import secure_write_text\n"
        f"secure_write_text(Path({str(path)!a}), {content!a})\n"
    )

    run = subprocess.run(
        [sys.executable, "-c", write],
        env={**os.environ, **_ASCII_LOCALE},
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stderr
    assert path.read_text(encoding="utf-8") == content


def test_diagnostic_note_survives_a_non_ascii_error_message(monkeypatch, tmp_path):
    """The note is written while a tool failure is being shaped, not after it."""
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics._find_existing_issues",
        lambda payload: [],
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("profile \U0001f680 unavailable"),
        context="extract-page",
        section_name="posts",
    )

    note = Path(diagnostics["issue_template_path"]).read_text(encoding="utf-8")
    assert "\U0001f680" in note
    assert diagnostics["error_message"] == "profile \U0001f680 unavailable"
