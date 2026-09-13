import logging

from linkedin_mcp_server.debug_trace import get_trace_dir, reset_trace_state_for_testing
from linkedin_mcp_server.logging_config import (
    _BROWSER_LIFECYCLE_LOGGERS,
    configure_logging,
    teardown_trace_logging,
)


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


def test_error_log_level_keeps_browser_loggers_at_error(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    configure_logging(log_level="ERROR")

    for name in _BROWSER_LIFECYCLE_LOGGERS:
        assert logging.getLogger(name).level == logging.ERROR


def test_warning_log_level_raises_browser_loggers_to_info(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    configure_logging(log_level="WARNING")

    for name in _BROWSER_LIFECYCLE_LOGGERS:
        assert logging.getLogger(name).level == logging.INFO


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


def _persisted_log(monkeypatch, tmp_path, log_level: str = "WARNING"):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr("linkedin_mcp_server.logging_config.atexit.register", id)
    configure_logging(log_level=log_level)
    trace_dir = get_trace_dir()
    assert trace_dir is not None
    return trace_dir / "server.log"


def test_the_persisted_log_keeps_browser_lifecycle_info(monkeypatch, tmp_path):
    """The lines around a browser exit are INFO, and the default level is not.

    Both browser modules, because the launch is logged by one and the close
    by the other; an unrelated module at INFO stays out, so this is not a
    global raise.
    """
    log_path = _persisted_log(monkeypatch, tmp_path)

    logging.getLogger("linkedin_mcp_server.drivers.browser").info("Closing browser")
    logging.getLogger("linkedin_mcp_server.core.browser").info("Browser closed")
    logging.getLogger("linkedin_mcp_server.scraping.extractor").info("scraped")
    for handler in logging.getLogger().handlers:
        handler.flush()

    persisted = log_path.read_text(encoding="utf-8")
    assert "Closing browser" in persisted
    assert "Browser closed" in persisted
    assert "scraped" not in persisted


def test_a_debug_run_is_not_raised_to_info(monkeypatch, tmp_path):
    log_path = _persisted_log(monkeypatch, tmp_path, log_level="DEBUG")

    logging.getLogger("linkedin_mcp_server.drivers.browser").debug("fine detail")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "fine detail" in log_path.read_text(encoding="utf-8")
