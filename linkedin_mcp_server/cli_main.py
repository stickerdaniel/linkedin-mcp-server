"""LinkedIn MCP Server main CLI application entry point."""

import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Literal

import inquirer

from linkedin_mcp_server.bootstrap import (
    configure_browser_environment,
    ensure_browser_installed,
)
from linkedin_mcp_server.core import AuthenticationError
from linkedin_mcp_server.authentication import clear_auth_state
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.schema import AppConfig
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
from linkedin_mcp_server.session_state import (
    get_runtime_id,
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

    if clear_auth_state(get_profile_dir()):
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

    user_data_dir = config.browser.user_data_dir
    success = run_profile_creation(user_data_dir)

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

    if config.is_interactive:
        print(
            "ℹ️  macOS may prompt to allow keychain access to the browser's "
            "Safe Storage."
        )
    try:
        ok = asyncio.run(
            import_session_from_browser(selector, user_data_dir=user_data_dir)
        )
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
        # a *replacement* would take and nothing downstream has them: the proxy
        # layer receives no configuration, and reading the singleton there would
        # parse whatever `sys.argv` holds.
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


def main() -> None:
    """Main application entry point."""
    config = get_config()

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

        # Set headless mode from config
        set_headless(config.browser.headless)

        # Handle --logout flag
        if config.server.logout:
            clear_profile_and_exit()

        # Ensure browser is installed for CLI modes that launch it.
        # Normal server startup uses async background setup instead. --login is
        # headed and needs full chromium; --status and --import-from-browser run
        # headless and need only the shell.
        if (
            config.server.login
            or config.server.status
            or config.server.import_from_browser
        ):
            ensure_browser_installed(full=config.server.login)

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
                config.validate()

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
