"""The local write path must put UTF-8 on disk, not the host's locale codec.

``secure_write_text`` is handed text by callers that cannot know the host's
ANSI code page or ``LANG``: the local diagnostic note carries a LinkedIn error
message verbatim, and the daemon descriptor and portable cookie file carry
whatever a profile path or a cookie value holds. Reading them back as UTF-8 is
what the rest of the server does, so a write in the locale codec either raises
inside the caller or leaves bytes no UTF-8 reader can decode.

Both directions are pinned here, because only one of them is loud. A character
the locale codec cannot encode raises where the caller is already handling a
failure; a character it can encode succeeds and corrupts the file silently.
"""

from pathlib import Path

from linkedin_mcp_server.common_utils import secure_write_text
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics


def test_secure_write_text_writes_a_character_the_locale_codec_cannot_encode(tmp_path):
    """An emoji in an error message is outside every ANSI code page."""
    content = "- Error: ToolError: profile \U0001f680 unavailable\n"
    path = tmp_path / "note.md"

    secure_write_text(path, content)

    assert path.read_text(encoding="utf-8") == content


def test_secure_write_text_does_not_leave_the_locale_codec_s_bytes(tmp_path):
    """A CJK profile name encodes in cp936, so the write succeeds and lies."""
    content = "- Error: \u4e2d\u6587 profile name\n"
    path = tmp_path / "note.md"

    secure_write_text(path, content)

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
