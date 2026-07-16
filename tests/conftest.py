import pytest


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset global state for test isolation."""
    from linkedin_mcp_server.bootstrap import reset_bootstrap_for_testing
    from linkedin_mcp_server.config import reset_config
    from linkedin_mcp_server.core.ip_monitor import reset_ip_drift_monitor_for_testing
    from linkedin_mcp_server.core.opsec import reset_opsec_gate_for_testing
    from linkedin_mcp_server.core.rate_limit import reset_rate_limiter_for_testing
    from linkedin_mcp_server.core.telemetry import reset_telemetry_for_testing
    from linkedin_mcp_server.dependencies import (
        _reset_ip_drift_call_counter_for_testing,
    )
    from linkedin_mcp_server.drivers.browser import reset_browser_for_testing
    from linkedin_mcp_server.session_state import (
        reset_pending_profile_leases_for_testing,
    )

    reset_bootstrap_for_testing()
    reset_browser_for_testing()
    reset_pending_profile_leases_for_testing()
    reset_config()
    reset_rate_limiter_for_testing()
    reset_opsec_gate_for_testing()
    reset_ip_drift_monitor_for_testing()
    reset_telemetry_for_testing()
    _reset_ip_drift_call_counter_for_testing()
    yield
    reset_bootstrap_for_testing()
    reset_browser_for_testing()
    reset_pending_profile_leases_for_testing()
    reset_config()
    reset_rate_limiter_for_testing()
    reset_opsec_gate_for_testing()
    reset_ip_drift_monitor_for_testing()
    reset_telemetry_for_testing()
    _reset_ip_drift_call_counter_for_testing()


@pytest.fixture(autouse=True)
def isolate_opsec_db(tmp_path, monkeypatch):
    """Redirect the opsec gate's default SQLite path to tmp_path.

    Without this, any test that exercises get_opsec_gate() for real (e.g.
    a tool-level send_message/connect_with_person test) would create and
    write to the real ~/.linkedin-mcp/opsec.db on the machine running the
    suite.
    """
    monkeypatch.setattr(
        "linkedin_mcp_server.core.opsec._DEFAULT_DB_PATH",
        tmp_path / "opsec.db",
    )


@pytest.fixture(autouse=True)
def isolate_telemetry_path(tmp_path, monkeypatch):
    """Redirect the telemetry buffer's default flush path to tmp_path.

    Without this, any test that exercises the real scrape_person() (most
    of test_scraping.py) records into the process-wide ScrapeTelemetry
    singleton -- reset_telemetry_for_testing() (see reset_singletons above)
    gives each test a fresh singleton, but a fresh singleton still reads
    DEFAULT_TELEMETRY_PATH at construction time, and every stealth profile
    defaults telemetry=True. Left unpatched, once 20 records accumulate
    across a test file's run the batch flush would write to the real
    ~/.linkedin-mcp/telemetry.jsonl on the machine running the suite.
    """
    monkeypatch.setattr(
        "linkedin_mcp_server.core.telemetry.DEFAULT_TELEMETRY_PATH",
        tmp_path / "telemetry.jsonl",
    )


@pytest.fixture(autouse=True)
def isolate_profile_dir(tmp_path, monkeypatch):
    """Redirect profile directory to tmp_path via config and DEFAULT_PROFILE_DIR."""
    fake_profile = tmp_path / "profile"
    monkeypatch.setenv("USER_DATA_DIR", str(fake_profile))

    # Patch DEFAULT_PROFILE_DIR for any code still referencing the constant
    for module in [
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
        "linkedin_mcp_server.session_state",
    ]:
        try:
            monkeypatch.setattr(f"{module}.DEFAULT_PROFILE_DIR", fake_profile)
        except AttributeError:
            pass  # Module may not be imported yet

    # Patch get_profile_dir() in all modules that import it
    for gp_module in [
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
    ]:
        try:
            monkeypatch.setattr(f"{gp_module}.get_profile_dir", lambda: fake_profile)
        except AttributeError:
            pass

    try:
        monkeypatch.setattr(
            "linkedin_mcp_server.session_state.get_source_profile_dir",
            lambda: fake_profile,
        )
    except AttributeError:
        pass

    for source_module in [
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.debug_trace",
        "linkedin_mcp_server.error_diagnostics",
    ]:
        try:
            monkeypatch.setattr(
                f"{source_module}.get_source_profile_dir",
                lambda: fake_profile,
            )
        except AttributeError:
            pass

    return fake_profile


@pytest.fixture
def profile_dir(isolate_profile_dir):
    """Create a non-empty profile directory."""
    isolate_profile_dir.mkdir(parents=True, exist_ok=True)
    # Create a marker file so profile_exists() returns True
    (isolate_profile_dir / "Default" / "Cookies").parent.mkdir(
        parents=True, exist_ok=True
    )
    (isolate_profile_dir / "Default" / "Cookies").write_text("placeholder")
    return isolate_profile_dir


@pytest.fixture
def mock_context():
    """Mock FastMCP Context."""
    from unittest.mock import AsyncMock, MagicMock

    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx
