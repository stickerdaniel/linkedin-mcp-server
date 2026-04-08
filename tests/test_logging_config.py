import logging

from linkedin_mcp_server.debug_trace import get_trace_dir, reset_trace_state_for_testing
from linkedin_mcp_server.logging_config import configure_logging, teardown_trace_logging


def setup_function():
    reset_trace_state_for_testing()


def teardown_function():
    teardown_trace_logging()
    reset_trace_state_for_testing()


def test_configure_logging_registers_trace_cleanup_once(monkeypatch, tmp_path):
    registrations = []

    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.logging_config.atexit.register",
        lambda fn: registrations.append(fn),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.logging_config._TRACE_CLEANUP_REGISTERED",
        False,
    )

    configure_logging()
    configure_logging()

    assert registrations == [teardown_trace_logging]


def test_registered_trace_cleanup_removes_ephemeral_trace_dir(monkeypatch, tmp_path):
    registrations = []

    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.logging_config.atexit.register",
        lambda fn: registrations.append(fn),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.logging_config._TRACE_CLEANUP_REGISTERED",
        False,
    )

    configure_logging()
    trace_dir = get_trace_dir()

    assert trace_dir is not None
    assert trace_dir.exists()
    assert registrations == [teardown_trace_logging]

    registrations[0]()

    assert not trace_dir.exists()
    assert not any(
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
    )
