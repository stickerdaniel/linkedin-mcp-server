import pytest


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset global state for test isolation."""
    from linkedin_mcp_server.bootstrap import reset_bootstrap_for_testing
    from linkedin_mcp_server.config import reset_config
    from linkedin_mcp_server.daemon_liveness import reset_liveness_for_testing
    from linkedin_mcp_server.drivers.browser import reset_browser_for_testing
    from linkedin_mcp_server.profile_lease import reset_leases_for_testing
    from linkedin_mcp_server.server_role import reset_process_role_for_testing

    reset_bootstrap_for_testing()
    # The owner's call tracker is process state like the rest. Left standing, a
    # call one test watched is still in flight for the next, which reads as an
    # owner that is never idle, and the moment of one test's last expiry scan
    # becomes the baseline for the next, where a gap of seconds reads as an
    # owner that was not running.
    reset_liveness_for_testing()
    reset_browser_for_testing()
    reset_leases_for_testing()
    reset_config()
    # Every test that builds a server records a role, and the auth gates read it
    # from process state. Left standing, one OWNER would refuse logins in every
    # test after it, in a suite where most never mention a role.
    reset_process_role_for_testing()
    yield
    reset_bootstrap_for_testing()
    reset_browser_for_testing()
    # After the browser, so a lease the browser still held is released by its
    # own bookkeeping first rather than yanked out from under it.
    reset_leases_for_testing()
    reset_config()
    reset_process_role_for_testing()
    reset_liveness_for_testing()


@pytest.fixture(autouse=True)
def ignore_the_developers_environment(monkeypatch):
    """Run against an empty environment rather than whatever the shell holds.

    The configuration loader calls ``load_dotenv()`` at import, so a local
    ``.env`` is part of the environment too. A developer running a real proxy
    then fails a test that sets only ``PROXY_SERVER``, because the loader sees
    that server *and* the ambient credentials and refuses the pair. That failure
    says nothing about the code, reproduces on no other machine, and is
    invisible in CI, which is the worst combination a test can have.

    Two sources rather than a list kept here, because a hand-written list is the
    same bug one setting along: it has to be remembered whenever a setting is
    added, and forgetting it produces a test that passes or fails only on the
    machine that happens to define it.

    * every key the loader reads, from the loader's own table;
    * every variable this project names after itself. Those are read directly
      through ``os.environ`` all over the package — debug switches, the trace
      mode, the container hint, the update check — and none of them go through
      the table above. Measured: with ``LINKEDIN_DEBUG_BRIDGE_COOKIE_SET`` set,
      a cookie-import test fails, and a run with every one of them set failed 22
      tests.

    What is deliberately left alone is everything this project did not name:
    ``CI``, which pytest and the workflow both mean something by,
    ``PYTEST_CURRENT_TEST``, which pytest owns, ``PLAYWRIGHT_BROWSERS_PATH``,
    which points at an installed browser, and the platform's own
    ``LOCALAPPDATA``, ``APPDATA`` and ``XDG_CONFIG_HOME``. Clearing those would
    not be isolation, it would be pretending to run somewhere else.
    """
    import os

    from linkedin_mcp_server.config.loaders import EnvironmentKeys

    from_the_table = [
        value
        for name, value in vars(EnvironmentKeys).items()
        if not name.startswith("_") and isinstance(value, str)
    ]
    ours = [key for key in os.environ if key.startswith("LINKEDIN")]
    for key in (*from_the_table, *ours):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def isolate_profile_dir(ignore_the_developers_environment, tmp_path, monkeypatch):
    """Redirect profile directory to tmp_path via config and DEFAULT_PROFILE_DIR.

    Takes the clearing fixture as an argument rather than trusting the order two
    autouse fixtures happen to run in. Pytest orders by scope, dependency and
    autouse, and explicitly not by where or in what order a fixture was defined;
    the order these two run in today comes from ``dir()`` being alphabetical,
    which is a coincidence a rename would end. Reversed, the clearing would
    delete the ``USER_DATA_DIR`` this sets, and tests would fall back to the
    real profile at ``~/.linkedin-mcp/profile`` — including anything that spawns
    a subprocess, which the in-process patches below do not reach.
    """
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

    # Claim it, the same way `main()` does on the first run against a fresh
    # custom root. Without this every test that rotates, restores, resets or
    # clears would be refused: tmp_path is not the canonical default, and the
    # guard exists precisely to refuse roots nobody claimed. Tests that prove
    # the refusal use a *different* directory, which stays unclaimed.
    from linkedin_mcp_server.profile_claim import ensure_profile_claim

    ensure_profile_claim(fake_profile)

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
