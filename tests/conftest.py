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
def isolate_profile_dir(tmp_path, monkeypatch):
    """Redirect DEFAULT_PROFILE_DIR to tmp_path."""
    fake_profile = tmp_path / "profile"
    for module in [
        "linkedin_mcp_server.drivers.browser",
        "linkedin_mcp_server.authentication",
        "linkedin_mcp_server.cli_main",
        "linkedin_mcp_server.setup",
    ]:
        try:
            monkeypatch.setattr(f"{module}.DEFAULT_PROFILE_DIR", fake_profile)
        except AttributeError:
            pass  # Module may not be imported yet
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
