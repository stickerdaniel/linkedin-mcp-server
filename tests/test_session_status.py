"""`get_session_status`: what is stored, read without a browser or LinkedIn."""

import asyncio
import json
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client

import linkedin_mcp_server.bootstrap as bootstrap
import linkedin_mcp_server.server as server_module
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.core.exceptions import AccountRestrictedError, RateLimitError
from linkedin_mcp_server.exceptions import (
    AuthenticationInProgressError,
    BrowserSetupFailedError,
)
from linkedin_mcp_server.drivers import browser as driver
from linkedin_mcp_server.profile_lease import ProfileLease
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.session_state import (
    get_runtime_id,
    get_source_profile_dir,
    portable_cookie_path,
    source_state_path,
    write_source_state,
)


@pytest.fixture
def quiet_lifespan(monkeypatch):
    """The production server, with a lifespan that touches no browser."""
    for name in (
        "initialize_bootstrap",
        "get_runtime_policy",
        "report_retained_browser_revisions_if_ready",
    ):
        monkeypatch.setattr(server_module, name, MagicMock())
    for name in (
        "start_background_browser_setup_if_needed",
        "watch_for_handoff_requests",
        "stop_background_browser_setup",
        "close_browser",
    ):
        monkeypatch.setattr(server_module, name, AsyncMock())


@pytest.fixture
def server(quiet_lifespan):
    return create_mcp_server()


@pytest.fixture
def nothing_may_start(monkeypatch):
    """Fail the call if it launches Chromium, takes the profile, or reaches out."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("get_session_status reached for a browser or the network")

    for target, name in (
        (BrowserManager, "start"),
        (ProfileLease, "try_acquire"),
        (ProfileLease, "acquire"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ):
        monkeypatch.setattr(target, name, refuse)
    for name in (
        "ensure_tool_ready_or_raise",
        "start_background_browser_setup_if_needed",
        "start_login_if_needed",
    ):
        monkeypatch.setattr(f"linkedin_mcp_server.bootstrap.{name}", refuse)


async def _status(server) -> dict:
    async with Client(server) as client:
        result = await client.call_tool("get_session_status", {})
    assert result.structured_content is not None
    return result.structured_content


async def test_a_stored_session_is_reported_from_its_files(server, nothing_may_start):
    profile = get_source_profile_dir()
    profile.mkdir(parents=True)
    (profile / "Local State").write_text("{}", encoding="utf-8")
    portable_cookie_path(profile).write_text("[]", encoding="utf-8")
    write_source_state(profile)

    status = await _status(server)

    assert status["session"] == {
        "stored": True,
        "profile": True,
        "cookies": True,
    }
    assert status["runtime"] == get_runtime_id()
    assert status["browser"]["manager_present"] is False
    assert status["browser"]["idle_seconds"] is None
    assert str(profile) not in str(status)


async def test_no_session_is_reported_as_none_stored(server, nothing_may_start):
    status = await _status(server)

    assert status["session"]["stored"] is False
    assert (status["setup"], status["login"]) == ("not_started", "idle")
    assert status["account_restricted"] is False


async def test_a_cookie_export_alone_is_not_a_stored_session(server):
    profile = get_source_profile_dir()
    profile.mkdir(parents=True)
    portable_cookie_path(profile).write_text("[]", encoding="utf-8")

    session = (await _status(server))["session"]

    assert (session["stored"], session["profile"], session["cookies"]) == (
        False,
        False,
        True,
    )


async def test_it_answers_while_another_call_holds_the_browser(
    server, monkeypatch, forbid_effects
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def holding(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise RateLimitError("done")

    ready = AsyncMock(side_effect=holding)
    with patch("linkedin_mcp_server.tools.person.get_ready_extractor", ready):
        async with Client(server) as client:
            running = asyncio.create_task(
                client.call_tool(
                    "get_person_profile",
                    {"linkedin_username": "someone"},
                    raise_on_error=False,
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            try:
                ended = driver._last_activity
                with monkeypatch.context() as guarded:
                    forbid_effects(guarded)
                    status = await asyncio.wait_for(
                        client.call_tool("get_session_status", {}), 5
                    )
                    assert driver._last_activity == ended
                    assert driver._calls_in_flight == 1
                assert status.structured_content["scope"] == "serving_process"
                assert status.structured_content["role"] == "direct"
            finally:
                release.set()
                await running

    assert status.structured_content["browser"]["calls_in_flight"] == 1


async def test_asking_does_not_reset_the_idle_clock(server, monkeypatch):
    ended = time.monotonic() - 42.0
    monkeypatch.setattr(driver, "_last_activity", ended)
    monkeypatch.setattr(driver, "_browser", BrowserManager())

    status = await _status(server)

    assert driver._last_activity == ended
    assert driver._calls_in_flight == 0
    assert status["browser"]["manager_present"] is True
    assert status["browser"]["idle_seconds"] >= 42.0


async def test_the_answer_leaves_the_update_notice_for_a_browser_tool(
    server, monkeypatch
):
    import linkedin_mcp_server.update_check as update_check

    monkeypatch.setattr(update_check, "__version__", "4.16.1")
    monkeypatch.setattr(update_check, "_latest_known", "4.18.0")
    prime = MagicMock()
    monkeypatch.setattr(update_check, "prime_from_cache", prime)
    refresh = AsyncMock()
    monkeypatch.setattr(update_check, "refresh_latest_version", refresh)
    notice = update_check.pending_update_notice()
    assert notice is not None

    async with Client(server) as client:
        status = await client.call_tool("get_session_status", {})

    assert notice not in [getattr(part, "text", None) for part in status.content]
    # Neither the cache on disk nor PyPI is asked.
    prime.assert_not_called()
    refresh.assert_not_called()


@pytest.fixture
def forbid_effects(monkeypatch):
    attempted = []

    def install(patcher=monkeypatch):
        for target, names in (
            (BrowserManager, ("start",)),
            (ProfileLease, ("try_acquire", "acquire")),
            (
                driver,
                (
                    "get_or_create_browser",
                    "ensure_authenticated",
                    "note_call_started",
                    "note_activity",
                ),
            ),
            (
                bootstrap,
                (
                    "initialize_bootstrap",
                    "ensure_tool_ready_or_raise",
                    "start_background_browser_setup_if_needed",
                    "start_login_if_needed",
                    "_start_login_if_needed",
                    "_refresh_background_task_state",
                    "_raise_if_auth_quiescent",
                    "interactive_login",
                    "_try_auto_import_session",
                ),
            ),
        ):
            for name in names:
                trap = MagicMock(side_effect=AssertionError(f"status invoked {name}"))
                patcher.setattr(target, name, trap)
                attempted.append(trap)
        for name in (
            "linkedin_mcp_server.update_check.prime_from_cache",
            "linkedin_mcp_server.update_check.refresh_latest_version",
            "linkedin_mcp_server.daemon.auth_repair.start_sign_in",
            "linkedin_mcp_server.daemon.auth_repair.wait_for_sign_in",
        ):
            trap = MagicMock(side_effect=AssertionError(f"status invoked {name}"))
            patcher.setattr(name, trap)
            attempted.append(trap)

    yield install
    for trap in attempted:
        trap.assert_not_called()


def _store_session():
    profile = get_source_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Local State").write_text("{}")
    portable_cookie_path(profile).write_text("[]")
    return write_source_state(profile)


@pytest.mark.parametrize(
    "metadata", [b"{", b"[]", b"{}", b'{"login_generation": "partial"}', b"\xff"]
)
async def test_partial_or_malformed_metadata_is_not_stored(
    server, nothing_may_start, metadata
):
    source = _store_session()
    bootstrap.go_auth_quiescent(source.login_generation)
    source_state_path().write_bytes(metadata)

    status = await _status(server)

    assert status["session"] == {"stored": False, "profile": True, "cookies": True}
    assert status["auth_quiescent"] is True


async def test_unvalidated_metadata_is_never_returned(server, nothing_may_start):
    _store_session()
    path = source_state_path()
    data = json.loads(path.read_text())
    for key in data:
        data[key] = {"private": "DO-NOT-RETURN"}
    path.write_text(json.dumps(data))

    status = await _status(server)

    # Stored means the same artifact-presence gate as readiness, not validated fields.
    assert status["session"] == {"stored": True, "profile": True, "cookies": True}
    assert "DO-NOT-RETURN" not in json.dumps(status)


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        (kind, outcome)
        for kind in ("setup", "login", "import")
        for outcome in ("success", "failure", "cancelled")
    ]
    + [("login", "restricted"), ("import", "restricted"), ("import", "no_session")],
)
async def test_finished_tasks_are_observed_without_reconciliation(
    server, forbid_effects, kind, outcome
):
    state = bootstrap.get_bootstrap_state()
    state.setup_state = bootstrap.SetupState.RUNNING
    state.auth_state = bootstrap.AuthState.IN_PROGRESS
    state.last_error = "previous private error"
    released = asyncio.Event()

    async def work():
        await released.wait()
        if outcome == "failure":
            raise RuntimeError("private task failure")
        if outcome == "restricted":
            raise AccountRestrictedError()
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return outcome == "success" if kind == "import" else None

    task = asyncio.create_task(work())
    setattr(state, f"{kind}_task", task)
    field = "auto_import" if kind == "import" else kind
    pending = (
        "in_progress"
        if kind == "import"
        else ("installing" if kind == "setup" else "login_in_progress")
    )
    forbid_effects()
    try:
        assert (await _status(server))[field] == pending
        released.set()
        await asyncio.wait({task})
        expected = {
            "success": {"setup": "ready", "login": "auth_ready", "import": "succeeded"}[
                kind
            ],
            "failure": "failed",
            "restricted": "failed",
            "cancelled": "cancelled",
            "no_session": "no_session",
        }[outcome]
        for _ in range(2):
            status = await _status(server)
            assert status[field] == expected
            assert status["account_restricted"] is (outcome == "restricted")
            assert "private" not in json.dumps(status)
        assert getattr(state, f"{kind}_task") is task
        assert state.setup_state is bootstrap.SetupState.RUNNING
        assert state.auth_state is bootstrap.AuthState.IN_PROGRESS
        assert state.account_restricted is False
        assert state.last_error == "previous private error"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_status_preserves_the_next_setup_failure(server, monkeypatch, cancelled):
    async def setup():
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("installer detail")

    state = bootstrap.get_bootstrap_state()
    state.setup_state = bootstrap.SetupState.RUNNING
    state.setup_task = asyncio.create_task(setup())
    await asyncio.wait({state.setup_task})
    assert (await _status(server))["setup"] == ("cancelled" if cancelled else "failed")

    monkeypatch.setattr(bootstrap, "initialize_bootstrap", lambda: None)
    with pytest.raises(
        BrowserSetupFailedError, match="cancelled" if cancelled else "installer detail"
    ):
        await bootstrap.ensure_tool_ready_or_raise("get_person_profile")
    assert state.setup_task is None


@pytest.mark.parametrize("kind", ["login", "import"])
async def test_status_preserves_restriction_for_the_next_ordinary_call(
    server, monkeypatch, kind
):
    async def restricted():
        raise AccountRestrictedError()

    state = bootstrap.get_bootstrap_state()
    state.setup_state = bootstrap.SetupState.READY
    task = asyncio.create_task(restricted())
    setattr(state, f"{kind}_task", task)
    await asyncio.wait({task})
    assert (await _status(server))["account_restricted"] is True
    monkeypatch.setattr(bootstrap, "initialize_bootstrap", lambda: None)
    monkeypatch.setattr(
        bootstrap, "start_background_browser_setup_if_needed", AsyncMock()
    )
    with pytest.raises(AccountRestrictedError):
        await bootstrap.ensure_tool_ready_or_raise("get_person_profile")


@pytest.mark.parametrize("cancelled", [False, True])
async def test_status_preserves_previous_login_failure_in_the_retry(
    server, monkeypatch, cancelled
):
    async def login():
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("previous login failure")

    state = bootstrap.get_bootstrap_state()
    state.setup_state = bootstrap.SetupState.READY
    state.login_task = asyncio.create_task(login())
    await asyncio.wait({state.login_task})
    assert (await _status(server))["login"] == ("cancelled" if cancelled else "failed")
    get_config().browser.login_inline_wait_seconds = 0
    monkeypatch.setattr(bootstrap, "initialize_bootstrap", lambda: None)
    monkeypatch.setattr(
        bootstrap, "start_background_browser_setup_if_needed", AsyncMock()
    )
    monkeypatch.setattr(bootstrap, "_auto_import_allowed", lambda: False)
    monkeypatch.setattr(bootstrap, "_move_invalid_auth_state_aside", lambda *_: None)
    hold = asyncio.Event()
    monkeypatch.setattr(bootstrap, "_run_login_flow", hold.wait)
    try:
        with pytest.raises(
            AuthenticationInProgressError,
            match="cancelled" if cancelled else "previous login failure",
        ):
            await bootstrap.ensure_tool_ready_or_raise("get_person_profile")
    finally:
        state.login_task.cancel()
        await asyncio.gather(state.login_task, return_exceptions=True)


async def test_replacement_generation_does_not_consume_quiescence(
    server, forbid_effects
):
    old = _store_session()
    bootstrap.get_bootstrap_state().auth_state = bootstrap.AuthState.READY
    bootstrap.go_auth_quiescent(old.login_generation)
    forbid_effects()

    stale = await _status(server)
    assert stale["session"]["stored"] is True
    assert stale["login"] == "auth_ready"
    assert stale["auth_quiescent"] is True

    # A frontend can rotate or partially write storage before committing a login.
    source_state_path().write_text("{}")
    assert (await _status(server))["auth_quiescent"] is True
    write_source_state()
    portable_cookie_path().unlink()
    assert (await _status(server))["auth_quiescent"] is True
    portable_cookie_path().write_text("[]")
    replaced = await _status(server)
    assert replaced["session"]["stored"] is True
    assert replaced["auth_quiescent"] is False
    assert bootstrap._auth_quiescent is True
    assert bootstrap._auth_quiescent_generation == old.login_generation


async def test_known_crash_is_reported_without_draining_the_browser(
    server, forbid_effects, monkeypatch
):
    browser = BrowserManager()
    page = MagicMock()
    browser._page = page
    monkeypatch.setattr(driver, "_browser", browser)
    forbid_effects()
    assert (await _status(server))["browser"]["page_crashed"] is False
    browser._note_page_crash(page)

    status = await _status(server)

    assert status["browser"]["manager_present"] is True
    assert status["browser"]["page_crashed"] is True
    assert driver._browser is browser
    page.assert_not_called()
    assert page.mock_calls == []


@pytest.mark.parametrize("confirmed", [False, True])
async def test_closing_and_unconfirmed_close_keep_lease_evidence(
    server, monkeypatch, confirmed
):
    from linkedin_mcp_server.profile_lease import get_profile_lease

    lease = get_profile_lease()
    assert lease.try_acquire()
    lease.mark_browser_open()
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()
        return confirmed

    browser = BrowserManager()
    monkeypatch.setattr(browser, "close", close)
    monkeypatch.setattr(driver, "_browser", browser)
    monkeypatch.setattr(driver, "_browser_lease", lease)
    closing = asyncio.create_task(driver.close_browser())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        status = (await _status(server))["browser"]
        assert status["manager_present"] is False
        assert status["lifecycle_busy"] is True
        assert status["lease_retained"] is True
    finally:
        release.set()
        await closing

    status = (await _status(server))["browser"]
    assert status["manager_present"] is False
    assert status["page_crashed"] is None
    assert status["lifecycle_busy"] is False
    assert status["lease_retained"] is (not confirmed)
    assert lease.held is (not confirmed)


@pytest.mark.parametrize("quiescent", [False, True])
async def test_authenticated_proxy_status_bypasses_browser_work_not_owner_admission(
    quiet_lifespan, monkeypatch, forbid_effects, quiescent
):
    from linkedin_mcp_server import __version__, update_check
    from linkedin_mcp_server.daemon import owner
    from linkedin_mcp_server.daemon.descriptor import build, new_instance_id, new_token
    from linkedin_mcp_server.daemon.discovery import Attachment
    from linkedin_mcp_server.daemon.liveness import get_liveness
    from linkedin_mcp_server.daemon.proxy import DaemonProxyBackend
    from linkedin_mcp_server.server_role import (
        ServerRole,
        reset_process_role_for_testing,
    )

    # Real loopback transport is required; every non-loopback destination is refused.
    connect, resolve = socket.socket.connect, socket.getaddrinfo
    connections = []

    def loopback_only(sock, address):
        assert address[0] == "127.0.0.1"
        connections.append(address)
        return connect(sock, address)

    def resolve_loopback(host, *args, **kwargs):
        assert host in ("127.0.0.1", b"127.0.0.1")
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(socket, "getaddrinfo", resolve_loopback)
    monkeypatch.setattr(update_check, "prime_from_cache", MagicMock())
    monkeypatch.setattr(update_check, "refresh_latest_version", AsyncMock())
    profile = get_source_profile_dir()
    config = get_config()
    config.server.tool_timeout_seconds = 10
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    token = new_token()
    attachment = Attachment(
        descriptor=build(
            instance_id=new_instance_id(),
            package_version=__version__,
            runtime_id=get_runtime_id(),
            profile=profile,
            host="127.0.0.1",
            port=port,
            path="/mcp",
            token=token,
            config=config,
            log_path=profile.parent / "owner.log",
        ),
        token=token,
    )
    backend = DaemonProxyBackend(
        attachment=attachment,
        auth_root=profile.parent,
        profile=profile,
        config=config,
    )
    frontend = create_mcp_server(
        role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=10
    )
    # Both servers share this test interpreter, but only the owner executes local tools.
    reset_process_role_for_testing()
    liveness = get_liveness()
    liveness.serving_as(attachment.descriptor.instance_id)
    serving_owner = owner.create_owner_server(
        config=config, token=token, host="127.0.0.1", port=port
    )
    serving = asyncio.create_task(serving_owner.serve(sockets=[listener]))
    entered, release = asyncio.Event(), asyncio.Event()

    async def holding(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise RateLimitError("done")

    monkeypatch.setattr("linkedin_mcp_server.tools.person.get_ready_extractor", holding)
    admissions = MagicMock(wraps=liveness.admit)
    monkeypatch.setattr(liveness, "admit", admissions)
    try:
        await owner._await_started(serving_owner, serving)
        liveness.the_endpoint_is_live()
        # Wrong credentials still fail before the collector runs.
        async with owner.direct_async_http_client() as http:
            denied = await http.post(
                attachment.descriptor.url,
                headers={
                    "Authorization": "Bearer wrong",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_session_status", "arguments": {}},
                },
            )
            assert denied.status_code == 401
        async with Client(frontend) as client:
            running = asyncio.create_task(
                client.call_tool(
                    "get_person_profile",
                    {"linkedin_username": "someone"},
                    raise_on_error=False,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                ended = driver._last_activity
                admitted_before = admissions.call_count
                if quiescent:
                    source = _store_session()
                    bootstrap.go_auth_quiescent(source.login_generation)
                with monkeypatch.context() as guarded:
                    forbid_effects(guarded)
                    status = await asyncio.wait_for(
                        client.call_tool("get_session_status", {}), 5
                    )
                    assert driver._last_activity == ended
                    assert driver._calls_in_flight == 1
                facts = status.structured_content
                assert facts["scope"] == "serving_process"
                assert facts["role"] == "owner"
                assert facts["login"] == "idle"
                assert facts["account_restricted"] is False
                assert facts["auth_quiescent"] is quiescent
                assert facts["browser"]["calls_in_flight"] == 1
                assert admissions.call_count == admitted_before + 1
                assert connections
            finally:
                release.set()
                await running
    finally:
        serving_owner.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
