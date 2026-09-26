"""LinkedIn MCP Server main CLI application entry point."""

import asyncio
import logging
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

import inquirer

from linkedin_mcp_server.bootstrap import (
    configure_browser_environment,
    ensure_browser_installed,
)
from linkedin_mcp_server.core import AuthenticationError
from linkedin_mcp_server.exceptions import (
    BrowserBusyError,
    BrowserDowngradeError,
    ProfileRootRefusedError,
)
from linkedin_mcp_server.authentication import clear_auth_state
from linkedin_mcp_server.config import get_config, set_config
from linkedin_mcp_server.config.loaders import load_config
from linkedin_mcp_server.config.schema import (
    PROFILE_HANDOVER_WAIT_SECONDS,
    AppConfig,
    ConfigurationError,
)
from linkedin_mcp_server.drivers.browser import (
    experimental_persist_derived_runtime,
    close_browser,
    get_or_create_browser,
    get_profile_dir,
    profile_exists,
    set_headless,
)
from linkedin_mcp_server.debug_trace import should_keep_traces
from linkedin_mcp_server.logging_config import configure_logging, teardown_trace_logging
from linkedin_mcp_server.login_viewer import (
    LoginViewerError,
    require_persistent_profile_mount,
)
from linkedin_mcp_server.profile_claim import ensure_profile_claim
from linkedin_mcp_server import session_state
from linkedin_mcp_server.session_state import (
    get_runtime_id,
    is_container_runtime,
    load_runtime_state,
    load_source_state,
    portable_cookie_path,
    runtime_profile_dir,
    runtime_storage_state_path,
    source_state_path,
)
from linkedin_mcp_server.server import ServerRole, create_mcp_server
from linkedin_mcp_server.setup import run_profile_creation

if TYPE_CHECKING:
    from linkedin_mcp_server.daemon import Attachment
    from linkedin_mcp_server.daemon_proxy import DaemonProxyBackend

logger = logging.getLogger(__name__)


def choose_transport_interactive() -> Literal["stdio", "streamable-http"]:
    """Prompt user for transport mode using inquirer."""
    questions = [
        inquirer.List(
            "transport",
            message="Choose mcp transport mode",
            choices=[
                ("stdio (Default CLI mode)", "stdio"),
                ("streamable-http (HTTP server mode)", "streamable-http"),
            ],
            default="stdio",
        )
    ]
    answers = inquirer.prompt(questions)

    if not answers:
        raise KeyboardInterrupt("Transport selection cancelled by user")

    return answers["transport"]


_RETIRE_PROMPT = (
    "A shared browser may be running in the background for this profile. "
    "Ask it to retire and continue? (y/N): "
)
_RETIRE_NEEDS_A_TERMINAL = (
    "ℹ️  A shared browser is recorded for this profile, and retiring it needs an "
    "interactive terminal to confirm. Continuing with the usual profile checks."
)
_RETIRE_UNREADABLE = (
    "ℹ️  A shared browser is recorded for this profile, but its record could not "
    "be read, so it cannot be asked to retire."
)
_RETIRE_OTHER_PROTOCOL = (
    "ℹ️  The shared browser recorded for this profile speaks another protocol, "
    "so this build cannot ask it to retire. If it is still running, wait for it "
    "to exit."
)
_RETIRE_NO_CONNECTION = (
    "ℹ️  No connection could be established to the recorded shared-browser "
    "endpoint. Continuing with the usual profile checks."
)
_RETIRE_BUSY = (
    "❌ A background shared browser for this profile is busy with another "
    "client's call. Wait for it to finish and retry. To keep future sessions "
    "from sharing a browser, start them with --no-daemon; that does not stop "
    "the one running now."
)
_RETIRE_REJECTED = (
    "❌ The shared browser did not accept this client's credentials; it may "
    "have been replaced. Retry."
)
# Scoped to this command's own operation. Never "nothing was changed": an owner
# that accepted and then answered badly may already be closing its browser,
# which exports the session as it goes.
_RETIRE_UNEXPECTED = (
    "❌ The shared browser answered in a way this build does not recognise. "
    "This command has not performed the requested profile operation, but the "
    "shared browser may have begun retiring. Retry in a moment."
)
_RETIRE_NO_ANSWER = (
    "❌ Asked the shared browser to retire but got no answer. It may be "
    "retiring now. This command has not performed the requested profile "
    "operation. Retry in a moment."
)
_RETIRE_INTERRUPTED = (
    "❌ Cancelled. The shared browser was asked to retire and may be retiring now."
)

#: How long the owner has to answer. It decides without waiting on anything, so
#: this bounds a hung process, not a slow decision.
_RETIRE_REPLY_SECONDS = 5.0


@dataclass
class _Retirement:
    """Whether this command has asked a shared browser to retire.

    Set once the user has confirmed and before the request is built, so from
    then on nothing may describe the request as unsent, whatever is interrupted:
    the send, the reply, reading it, or the command's own wait and operation.
    """

    requested: bool = False


@contextmanager
def _reporting_a_confirmed_retirement() -> Iterator[_Retirement]:
    """Say that retirement may have begun if the command is interrupted after asking.

    Wraps the whole of a profile command from the retirement lookup onwards, so
    there is no gap between the request and the command's own work for an
    interrupt to fall through. An interrupt before the request, including at
    the prompt, is not this one's to report: nothing was sent.
    """
    retirement = _Retirement()
    try:
        yield retirement
    except KeyboardInterrupt:
        if not retirement.requested:
            raise
        print(f"\n{_RETIRE_INTERRUPTED}")
        sys.exit(130)


def _retire_a_shared_browser(config: AppConfig, retirement: _Retirement) -> bool:
    """Ask an idle shared browser to retire, if there is one and the user agrees.

    For ``--logout``, ``--login`` and ``--import-from-browser``, which change a
    profile a shared browser may be holding. True means the owner agreed and is
    retiring, so the caller waits a bounded time for the profile instead of
    demanding it. False means there was nothing to ask, and the command goes on
    exactly as it would without a daemon: the profile lease is what keeps it off
    a browser that is still running. Every refusal exits here.

    Call it inside ``_reporting_a_confirmed_retirement``, with the command's
    own work, and pass that scope's *retirement*.

    Nothing is contacted before the user answers. The lookup reads files only,
    and a process that would never use a shared browser does not even do that.
    """
    from linkedin_mcp_server.daemon import daemon_would_be_used

    if not daemon_would_be_used(config):
        return False
    attachment = _owner_to_retire(config)
    if attachment is None:
        return False

    if not config.is_interactive:
        # Nothing to confirm with, so nothing is asked. Not a refusal: the record
        # may be all an exited owner left, and the profile lease still keeps this
        # command off a browser that is running, exactly as for a Direct server.
        print(_RETIRE_NEEDS_A_TERMINAL)
        return False
    try:
        answer = input(_RETIRE_PROMPT).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n❌ Operation cancelled")
        sys.exit(0)
    if answer not in ("y", "yes"):
        print("❌ Operation cancelled")
        sys.exit(0)

    retirement.requested = True
    return _ask_to_retire(attachment)


def _owner_to_retire(config: AppConfig) -> "Attachment | None":
    """The recorded owner of this profile that may be asked to retire, if any.

    A reading of files and nothing more, so it says what is recorded rather than
    what is running: an owner that exited leaves its record behind.
    """
    from linkedin_mcp_server.daemon import (
        Mismatch,
        OwnerState,
        look_up_owner,
    )
    from linkedin_mcp_server.session_state import auth_root_dir

    profile = get_profile_dir()
    try:
        lookup = look_up_owner(
            auth_root_dir(profile), profile, config, for_retirement=True
        )
    except Exception:
        logger.debug("The shared browser record could not be read", exc_info=True)
        print(_RETIRE_UNREADABLE)
        return None
    if lookup.state is OwnerState.UNTRUSTED:
        logger.debug("Shared browser record detail: %s", lookup.reason)
        print(_RETIRE_UNREADABLE)
        return None
    if lookup.mismatch is Mismatch.PROTOCOL:
        print(_RETIRE_OTHER_PROTOCOL)
        return None
    attachment = lookup.attachment
    if (
        lookup.state is not OwnerState.ATTACHABLE
        or attachment is None
        or attachment.control_only
    ):
        # Nothing recorded, or an owner of another runtime or profile: not the
        # browser this command is about to change.
        return None
    return attachment


def _ask_to_retire(attachment: "Attachment") -> bool:
    """Send the idle-only retirement and act on the answer.

    Idle-only, and never anything else: a failure here is reported, not retried
    as the unconditional stand-down, which would cut off another client's call.
    An interrupt is left to the caller's ``_reporting_a_confirmed_retirement``,
    which covers reading the reply as well as sending the request.
    """
    from urllib.parse import urlsplit, urlunsplit

    import httpx

    from linkedin_mcp_server import daemon_owner

    instance = attachment.descriptor.instance_id
    parts = urlsplit(attachment.descriptor.url)
    url = urlunsplit((parts.scheme, parts.netloc, daemon_owner.STAND_DOWN_PATH, "", ""))
    try:
        with daemon_owner.direct_http_client(timeout=_RETIRE_REPLY_SECONDS) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {attachment.token}"},
                json=daemon_owner.idle_only_request(instance),
            )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # No connection was made, so this request was not delivered. That is
        # all it proves: not that the owner is gone, only that it was not asked.
        # Refusing here would refuse every run after an owner exits, since it
        # leaves its record behind, so the command goes on as it would without
        # a daemon and the lease decides whether the profile is free.
        logger.debug("The shared browser could not be reached", exc_info=True)
        print(_RETIRE_NO_CONNECTION)
        return False
    except Exception:
        logger.debug("The retirement request got no answer", exc_info=True)
        print(_RETIRE_NO_ANSWER)
        sys.exit(1)

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    if (
        response.status_code == 200
        and payload.get("standing_down") is True
        and payload.get("retiring") is True
        and payload.get("instance") == instance
    ):
        print("ℹ️  The shared browser is retiring; waiting for it to let go.")
        return True
    if response.status_code == 409 and payload.get("busy") is True:
        print(_RETIRE_BUSY)
        sys.exit(1)
    if response.status_code == 401:
        print(_RETIRE_REJECTED)
        sys.exit(1)
    logger.debug("Unexpected retirement answer: %s", response.status_code)
    print(_RETIRE_UNEXPECTED)
    sys.exit(1)


def clear_profile_and_exit() -> None:
    """Clear LinkedIn browser profile and exit."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Profile Clear mode")

    auth_root = get_profile_dir().parent

    if not (
        profile_exists(get_profile_dir())
        or portable_cookie_path(get_profile_dir()).exists()
        or source_state_path(get_profile_dir()).exists()
    ):
        print("ℹ️  No authentication state found")
        print("Nothing to clear.")
        sys.exit(0)

    print(f"🔑 Clear LinkedIn authentication state from {auth_root}?")

    try:
        confirmation = (
            input("Are you sure you want to clear the profile? (y/N): ").strip().lower()
        )
        if confirmation not in ("y", "yes"):
            print("❌ Operation cancelled")
            sys.exit(0)
    except KeyboardInterrupt:
        print("\n❌ Operation cancelled")
        sys.exit(0)

    # After the deletion prompt, which it does not replace: agreeing to retire a
    # browser is not agreeing to delete a session.
    with _reporting_a_confirmed_retirement() as retirement:
        if _retire_a_shared_browser(config, retirement):
            try:
                cleared = session_state.clear_auth_state(
                    get_profile_dir(), wait_seconds=PROFILE_HANDOVER_WAIT_SECONDS
                )
            except RuntimeError as e:
                # The profile did not come free in time: a successor may have
                # taken it, or the retiring browser is slow to close. Nothing
                # was deleted.
                print(f"❌ {e}")
                sys.exit(1)
        else:
            cleared = clear_auth_state(get_profile_dir())

    if cleared:
        print("✅ LinkedIn authentication state cleared successfully!")
    else:
        print("❌ Failed to clear authentication state")
        sys.exit(1)

    sys.exit(0)


def get_profile_and_exit() -> None:
    """Create profile interactively and exit."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Session Creation mode")

    # The login already waits for the profile (`setup.interactive_login`), which
    # is the whole of what a retiring owner needs from it.
    user_data_dir = config.browser.user_data_dir
    with _reporting_a_confirmed_retirement() as retirement:
        _retire_a_shared_browser(config, retirement)
        success = run_profile_creation(
            user_data_dir, login_viewer=config.server.login_viewer
        )

    sys.exit(0 if success else 1)


def import_from_browser_and_exit() -> None:
    """Import a LinkedIn session from a local browser, validate, persist, exit."""
    config = get_config()
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )
    logger.info("LinkedIn MCP Server v%s - Browser Import mode", get_version())

    configure_browser_environment()
    set_headless(True)  # validation runs headless
    user_data_dir = get_profile_dir()
    selector = (
        None
        if config.server.import_from_browser == "auto"
        else config.server.import_from_browser
    )

    from linkedin_mcp_server.browser_import.orchestrate import (
        import_session_from_browser,
    )
    from linkedin_mcp_server.exceptions import (
        CookieDecryptionError,
        NoLinkedInSessionFoundError,
    )

    with _reporting_a_confirmed_retirement() as retirement:
        # Only a retiring owner is waited for, at the import's own first claim
        # on the profile. Without one the import demands the profile as it
        # always has.
        retiring = _retire_a_shared_browser(config, retirement)
        if config.is_interactive:
            print(
                "ℹ️  macOS may prompt to allow keychain access to the browser's "
                "Safe Storage."
            )
        try:
            ok = asyncio.run(
                import_session_from_browser(
                    selector,
                    user_data_dir=user_data_dir,
                    profile_wait_seconds=(
                        PROFILE_HANDOVER_WAIT_SECONDS if retiring else 0.0
                    ),
                )
            )
        except BrowserBusyError as e:
            if not retiring:
                raise
            # The profile did not come free in time. Nothing was imported.
            print(f"❌ {e}")
            sys.exit(1)
        except NoLinkedInSessionFoundError as e:
            print(f"❌ {e}")
            print("   Log into LinkedIn in your browser first, or run with --login.")
            sys.exit(1)
        except (CookieDecryptionError, AuthenticationError) as e:
            print(f"❌ Could not import session: {e}")
            sys.exit(1)

    if ok:
        print(f"✅ Imported and validated LinkedIn session into {user_data_dir}")
        sys.exit(0)
    print("❌ Imported cookies did not produce a valid session.")
    print("   The browser session may be expired. Re-login there or use --login.")
    sys.exit(1)


def profile_info_and_exit() -> None:
    """Check profile validity and display info, then exit."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Session Info mode")

    profile_dir = get_profile_dir()
    cookies_path = portable_cookie_path(profile_dir)
    source_state = load_source_state(profile_dir)
    current_runtime = get_runtime_id()

    if not source_state or not profile_exists(profile_dir) or not cookies_path.exists():
        print(f"❌ No valid source session found at {profile_dir}")
        print("   Run with --login to create a source session")
        sys.exit(1)

    print(f"Current runtime: {current_runtime}")
    print(f"Source runtime: {source_state.source_runtime_id}")
    print(f"Login generation: {source_state.login_generation}")

    runtime_state = None
    runtime_profile = None
    runtime_storage_state = None
    bridge_required = False

    if current_runtime == source_state.source_runtime_id:
        print(f"Profile mode: source ({profile_dir})")
    else:
        runtime_state = load_runtime_state(current_runtime, profile_dir)
        runtime_profile = runtime_profile_dir(current_runtime, profile_dir)
        runtime_storage_state = runtime_storage_state_path(current_runtime, profile_dir)
        if not experimental_persist_derived_runtime():
            bridge_required = True
            print("Profile mode: foreign runtime (fresh bridge each startup)")
            if runtime_profile.exists():
                print(
                    f"Derived runtime cache present but ignored by default: {runtime_profile}"
                )
        else:
            if (
                runtime_state
                and runtime_state.source_login_generation
                == source_state.login_generation
                and profile_exists(runtime_profile)
                and runtime_storage_state.exists()
            ):
                print(
                    f"Profile mode: derived (committed, current generation) ({runtime_profile})"
                )
            else:
                bridge_required = True
                state = "stale generation" if runtime_state else "missing"
                print(f"Profile mode: derived ({state})")
            print(
                "Storage snapshot: "
                f"{runtime_storage_state if runtime_storage_state and runtime_storage_state.exists() else 'missing'}"
            )

    async def check_session() -> bool:
        try:
            set_headless(True)  # Always check headless
            browser = await get_or_create_browser()
            return browser.is_authenticated
        except AuthenticationError:
            return False
        except BrowserDowngradeError:
            # Not "unexpected", and no traceback. This is the guard doing its
            # job, the message already says which two versions and what to do,
            # and `--status` is the first thing a puzzled user runs. The tool
            # path treats it the same way, in `error_handler`.
            raise
        except Exception as e:
            logger.exception(f"Unexpected error checking session: {e}")
            raise
        finally:
            await close_browser()

    if bridge_required:
        if experimental_persist_derived_runtime():
            print(
                "ℹ️  A derived runtime profile will be created and checkpoint-committed on the next server startup."
            )
        else:
            print(
                "ℹ️  A fresh bridged foreign-runtime session will be created on the next server startup."
            )
        print(
            "ℹ️  Source cookie validity is not verified in this mode. Run the server to test the bridge end-to-end."
        )
        sys.exit(0)

    try:
        valid = asyncio.run(check_session())
    except BrowserDowngradeError as e:
        # Ahead of the generic handler, which would add "Check logs and browser
        # configuration" to a message that already names the fix exactly.
        print(f"\n❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Could not validate session: {e}")
        print("   Check logs and browser configuration.")
        sys.exit(1)

    active_profile = profile_dir if runtime_profile is None else runtime_profile
    if valid:
        print(f"✅ Session is valid (profile: {active_profile})")
        sys.exit(0)

    print(f"❌ Session expired or invalid (profile: {active_profile})")
    print("   Run with --login to re-authenticate")
    sys.exit(1)


def _obtain_shared_owner(config: AppConfig) -> "DaemonProxyBackend | None":
    """Return the shared owner to forward to, starting one if nobody has.

    Off by default, and inert when off. ``None`` means this process serves its
    own client the way it always has: the flag is off, the transport is HTTP (one
    server for many clients already, so there is nothing to deduplicate), or no
    owner could be established at all.

    Never fatal, and that is a decision rather than an oversight. A client that
    refused to start because a shared browser could not be elected would fail
    where nobody can read the reason — an MCP client reports "server failed to
    start" and the explanation sits in a log the user has not opened. A working
    server with a warning beats a dead one with a better excuse while the flag is
    opt-in. It is a real trade, though: falling back means two clients can hand
    the profile back and forth per call again, which is the cost #606 exists to
    remove, so the warning is the only thing that says the feature was lost. The
    balance changes when the flag becomes the default.

    What keeps two processes off one profile on that fallback path is the profile
    lease from #598, not anything here: the owner takes it when it opens Chromium
    and hands it over when another process announces itself
    (``drivers/browser.watch_for_handoff_requests``). The daemon lock decides who
    *owns* the browser; the lease decides who is driving it right now.

    That safety net carries more weight on Windows than elsewhere, which is worth
    knowing before the flag becomes the default. A frontend there takes no lock
    at all and starts a child that competes for one
    (``daemon_election._start_contending_for_the_lock``), so a local spawn
    failure is indistinguishable from a rival owner that holds the lock and has
    not published yet: ``obtain_owner`` returns at once and this process falls
    back while an owner may in fact be coming up. On POSIX the equivalent
    outcomes are terminal, because the frontend held the lock itself. Windows
    already carries platform gaps that gate the default flip, and this is one of
    them.
    """
    from linkedin_mcp_server.daemon import daemon_would_be_used

    if not daemon_would_be_used(config):
        return None

    try:
        from linkedin_mcp_server.daemon_election import obtain_owner
        from linkedin_mcp_server.daemon_proxy import DaemonProxyBackend
        from linkedin_mcp_server.session_state import auth_root_dir

        profile = get_profile_dir()
        auth_root = auth_root_dir(profile)
        outcome = obtain_owner(auth_root, profile, config)
    except Exception:
        logger.warning("The shared browser owner is unavailable", exc_info=True)
        return None

    attachment = outcome.attachment_lookup.attachment
    if outcome.worth_connecting and attachment is not None:
        logger.info("Forwarding to the shared browser owner")
        # Handed on as the election verified it. Re-reading the descriptor or the
        # token from disk would be a second, unproven read of a pair this one
        # already matched and reached.
        #
        # The election's own inputs travel with it, because they are what finding
        # a *replacement* would take. Reading process-global state downstream
        # would hide that dependency instead of carrying the verified inputs.
        return DaemonProxyBackend(
            attachment=attachment,
            auth_root=auth_root,
            profile=profile,
            config=config,
        )

    logger.warning(
        "No shared browser owner could be started (%s); this server will "
        "drive its own browser",
        outcome.attachment_lookup.state.value,
    )
    return None


def get_version() -> str:
    """Get version from installed metadata with a source fallback."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        for package_name in (
            "mcp-server-linkedin",
            "linkedin-scraper-mcp",
            "linkedin-mcp-server",
        ):
            try:
                return version(package_name)
            except PackageNotFoundError:
                continue
    except Exception:
        pass

    try:
        import os
        import tomllib

        pyproject_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "pyproject.toml"
        )
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            return data["project"]["version"]
    except Exception:
        return "unknown"


def _exit_on_a_bad_setting(error: ConfigurationError) -> NoReturn:
    """Report a configuration error the way a user can act on it.

    Printed, because logging is configured from the configuration that may be
    what failed. To stderr for the same reason it carries every other
    diagnostic here: stdout belongs to the protocol.
    """
    print(f"❌ Configuration error: {error}", file=sys.stderr)
    sys.exit(1)


def _preflight_login_viewer(config: AppConfig) -> None:
    """Validate the container boundary and persistent mount before mutation."""
    if not config.server.login_viewer:
        return
    if not is_container_runtime():
        raise ConfigurationError("--login-viewer is available only inside a container")
    try:
        require_persistent_profile_mount(Path(config.browser.user_data_dir))
    except LoginViewerError as exc:
        raise ConfigurationError(str(exc)) from exc


def main(argv: Sequence[str] | None = None) -> None:
    """Main application entry point."""
    try:
        config = load_config(sys.argv[1:] if argv is None else argv)
        set_config(config)
        _preflight_login_viewer(config)
    except ConfigurationError as e:
        # A bad setting used to leave the loader as an exception nothing
        # caught, so Python printed the whole stack down through the loader and
        # the process died. Under a stdio host that stack is all the user sees
        # behind "Server disconnected", with the setting at fault on its last
        # line and everything above it looking like a crash.
        _exit_on_a_bad_setting(e)

    # Configure logging
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()

    # Print banner in interactive mode
    if config.is_interactive:
        print(f"🔗 LinkedIn MCP Server v{version} 🔗")
        print("=" * 40)

    logger.info(f"LinkedIn MCP Server v{version}")

    try:
        configure_browser_environment()

        # Establish, once, that this process may move and delete what
        # USER_DATA_DIR names. Before everything: the logout below deletes the
        # whole auth root, bootstrap downloads a browser into it, and daemon
        # election spawns an owner that would repeat this check anyway. Against
        # the *configured* root only — a derived runtime profile has its own
        # nested auth root, and claiming that one would protect the wrong
        # directory while looking like protection.
        #
        # Read off the validated config already in hand rather than reaching
        # through `get_source_profile_dir()` to process-global state.
        ensure_profile_claim(
            Path(config.browser.user_data_dir),
            claim_anyway=config.server.claim_profile_root,
        )

        # Set headless mode from config
        set_headless(config.browser.headless)

        # Handle --logout flag
        if config.server.logout:
            clear_profile_and_exit()

        # Ensure the browser is installed for CLI modes that launch one. Normal
        # server startup uses async background setup instead. All three modes
        # need the same binary: headed and headless are modes of it, not
        # separate products.
        if (
            config.server.login
            or config.server.status
            or config.server.import_from_browser
        ):
            ensure_browser_installed()

        # Handle --import-from-browser flag
        if config.server.import_from_browser:
            import_from_browser_and_exit()

        # Handle --login flag
        if config.server.login:
            get_profile_and_exit()

        # Handle --status flag
        if config.server.status:
            profile_info_and_exit()

        logger.debug(f"Server configuration: {config}")

        # Phase 1: Server Runtime
        try:
            transport = config.server.transport

            # Prompt for transport in interactive mode if not explicitly set
            if config.is_interactive and not config.server.transport_explicitly_set:
                print("\n🚀 Server ready! Choose transport mode:")
                transport = choose_transport_interactive()
                # Record the answer rather than keeping it in a local. Two
                # checks read the stored transport to decide how exposed this
                # process is: the bind-address warning, and the gate that
                # decides whether reading the local browser's LinkedIn cookie
                # is safe. Leaving it at stdio told them a listening HTTP
                # server was a private one. Re-validating applies the HTTP
                # rules that were skipped when the value said stdio.
                config.server.transport = transport
                try:
                    config.validate()
                except ConfigurationError as e:
                    # Inside the runtime `try` below, whose handler calls
                    # logger.exception. A setting that only applies to HTTP
                    # fails here and nowhere else, and it deserves the same
                    # answer as one caught at startup.
                    _exit_on_a_bad_setting(e)

            # Get a shared owner running before building this process's server.
            # It has to come first now: whether this process drives a browser or
            # forwards to one that does depends on the answer.
            #
            # Reads the stored transport rather than the local above, which is
            # why the interactive prompt writes its answer back into the config
            # before this runs: `daemon_would_be_used` asks whether this is a
            # stdio process, and an interactively chosen HTTP server must not
            # elect a daemon.
            proxy_backend = _obtain_shared_owner(config)

            # Create and run the MCP server
            if proxy_backend is None:
                mcp = create_mcp_server(tool_timeout=config.server.tool_timeout_seconds)
            else:
                mcp = create_mcp_server(
                    tool_timeout=config.server.tool_timeout_seconds,
                    role=ServerRole.PROXY,
                    proxy_backend=proxy_backend,
                )

            if transport == "streamable-http":
                # Validate Host and Origin. Without this a website the user
                # merely visits can point a hostname at this server's address
                # and have the user's own browser drive tools with the
                # logged-in LinkedIn session. The request comes from inside, so
                # a firewall does not help. The MCP specification requires this
                # for local HTTP servers, and it is off unless asked for.
                #
                # Both checks are needed, and the Host one carries most of the
                # weight. A rebinding attack sends its own domain as *both*
                # Host and Origin, so those agree and origin validation alone
                # lets it through; what gives it away is that the Host is not a
                # name this server answers to. Requests carrying no Origin at
                # all stay allowed, which is every non-browser client.
                #
                # True rather than "auto": "auto" only validates when the
                # accepted connection landed on a loopback address, so a server
                # bound to 0.0.0.0 and reached over its LAN address checked
                # nothing at all, which is the exposed case where it matters
                # most. Measured before this: an attacker Host and Origin over
                # the LAN address were served, while the same request to
                # 127.0.0.1 was refused.
                #
                # Strict accepts localhost and the address the connection
                # arrived on, which covers the documented flows. It does not
                # accept a DNS name such as a machine name or a public name in
                # front of a proxy, so those need the proxy to rewrite the
                # upstream Host, or the name listed explicitly. The README says
                # so next to the exposed-bind example, because a 421 nobody can
                # explain is how a guard like this ends up switched off.
                #
                # Deliberately no host wildcard: it would accept any Host and
                # reopen the same hole from the other side.
                mcp.run(
                    transport=transport,
                    host=config.server.host,
                    port=config.server.port,
                    path=config.server.path,
                    host_origin_protection=True,
                )
            else:
                mcp.run(transport=transport)

        except KeyboardInterrupt:
            exit_gracefully(0)

        except Exception as e:
            logger.exception(f"Server runtime error: {e}")
            if config.is_interactive:
                print(f"\n❌ Server error: {e}")
            exit_gracefully(1)

    except ProfileRootRefusedError as e:
        # Printed rather than raised through, because a traceback is the wrong
        # shape for this: nothing went wrong in the code, a path was named that
        # this server will not delete, and the message already says what to do
        # about it.
        logger.error(str(e))
        if config.is_interactive:
            print(f"\n❌ {e}")
        sys.exit(1)

    finally:
        teardown_trace_logging(keep_traces=should_keep_traces())


def exit_gracefully(exit_code: int = 0) -> None:
    """Exit the application gracefully with browser cleanup."""
    try:
        asyncio.run(close_browser())
    except Exception:
        pass  # Best effort cleanup
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        exit_gracefully(0)
    except Exception as e:
        logger.exception(
            f"Error running MCP server: {e}",
            extra={"exception_type": type(e).__name__, "exception_message": str(e)},
        )
        exit_gracefully(1)
