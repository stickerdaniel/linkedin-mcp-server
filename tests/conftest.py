import json

import pytest


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset global state for test isolation."""
    from linkedin_mcp_server.config import reset_config
    from linkedin_mcp_server.drivers.browser import reset_browser_for_testing

    reset_browser_for_testing()
    reset_config()
    yield
    reset_browser_for_testing()
    reset_config()


@pytest.fixture(autouse=True)
def isolate_user_data_dir(tmp_path, monkeypatch):
    """Redirect DEFAULT_USER_DATA_DIR to tmp_path."""
    fake_user_data_dir = tmp_path / "browser-profile"
    for module in [
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
    ]:
        try:
            monkeypatch.setattr(f"{module}.DEFAULT_USER_DATA_DIR", fake_user_data_dir)
        except AttributeError:
            pass  # Module may not be imported yet
    return fake_user_data_dir


@pytest.fixture(autouse=True)
def isolate_session_path(tmp_path, monkeypatch):
    """Redirect LEGACY_SESSION_PATH to tmp_path (for migration tests)."""
    fake_session = tmp_path / "session.json"
    for module in [
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
    ]:
        try:
            monkeypatch.setattr(f"{module}.LEGACY_SESSION_PATH", fake_session)
        except AttributeError:
            pass  # Module may not be imported yet
    return fake_session


@pytest.fixture
def browser_profile(isolate_user_data_dir):
    """Create a browser profile directory with indicators."""
    isolate_user_data_dir.mkdir(parents=True, exist_ok=True)
    # Create profile indicator files
    (isolate_user_data_dir / "Preferences").write_text(
        json.dumps({"browser": {"check_default_browser": False}})
    )
    (isolate_user_data_dir / "Local State").write_text(json.dumps({}))
    return isolate_user_data_dir


@pytest.fixture
def session_file(isolate_session_path):
    """Create valid legacy session file (for migration tests)."""
    isolate_session_path.parent.mkdir(parents=True, exist_ok=True)
    isolate_session_path.write_text(
        json.dumps(
            {"cookies": [{"name": "li_at", "value": "test", "domain": ".linkedin.com"}]}
        )
    )
    return isolate_session_path


@pytest.fixture
def mock_context():
    """Mock FastMCP Context."""
    from unittest.mock import AsyncMock, MagicMock

    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx
