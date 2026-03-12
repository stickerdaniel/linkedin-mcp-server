from linkedin_mcp_server.debug_trace import (
    cleanup_trace_dir,
    get_trace_dir,
    mark_trace_for_retention,
    reset_trace_state_for_testing,
)


def setup_function():
    reset_trace_state_for_testing()


def teardown_function():
    reset_trace_state_for_testing()


def test_get_trace_dir_creates_ephemeral_dir_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    trace_dir = get_trace_dir()

    assert trace_dir is not None
    assert trace_dir.exists()
    assert "trace-runs" in str(trace_dir)


def test_cleanup_trace_dir_removes_ephemeral_dir_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    trace_dir = get_trace_dir()
    assert trace_dir is not None

    cleanup_trace_dir()

    assert not trace_dir.exists()


def test_mark_trace_for_retention_keeps_trace_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    trace_dir = mark_trace_for_retention()
    assert trace_dir is not None

    cleanup_trace_dir()

    assert trace_dir.exists()


def test_explicit_trace_dir_is_preserved(monkeypatch, tmp_path):
    trace_dir = tmp_path / "explicit-trace"
    monkeypatch.setenv("LINKEDIN_DEBUG_TRACE_DIR", str(trace_dir))

    resolved = get_trace_dir()
    assert resolved == trace_dir
    trace_dir.mkdir(parents=True, exist_ok=True)

    cleanup_trace_dir()

    assert trace_dir.exists()


def test_trace_mode_off_disables_trace_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("LINKEDIN_TRACE_MODE", "off")

    assert get_trace_dir() is None
