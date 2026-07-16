"""LinkedIn MCP Server main CLI application entry point."""

import asyncio
import logging
import sys
from typing import Literal

import inquirer

from linkedin_mcp_server.bootstrap import (
    configure_browser_environment,
    ensure_browser_installed,
)
from linkedin_mcp_server.core import AuthenticationError, NetworkError
from linkedin_mcp_server.authentication import clear_auth_state
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    get_or_create_browser,
    get_profile_dir,
    set_headless,
)
from linkedin_mcp_server.debug_trace import should_keep_traces
from linkedin_mcp_server.logging_config import configure_logging, teardown_trace_logging
from linkedin_mcp_server.session_state import (
    get_runtime_id,
    load_source_state,
    portable_cookie_is_valid,
    runtime_profile_dir,
    runtime_profiles_root,
)
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.setup import run_profile_creation

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
        runtime_root = runtime_profiles_root(get_profile_dir())
        if runtime_root.exists():
            print(
                "⚠️  Isolated runtime profiles were retained because another "
                "MCP process may still own them. Stop those processes before "
                f"removing {runtime_root}."
            )
    else:
        print(
            "❌ Failed to clear authentication state; another login/import "
            "transaction may still be active"
        )
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
    source_state = load_source_state(profile_dir)
    current_runtime = get_runtime_id()

    if not source_state or not portable_cookie_is_valid(profile_dir):
        print(f"❌ No valid source session found at {profile_dir}")
        print("   Run with --login to create a source session")
        sys.exit(1)

    print(f"Current runtime: {current_runtime}")
    print(f"Source runtime: {source_state.source_runtime_id}")
    print(f"Login generation: {source_state.login_generation}")

    runtime_profile = runtime_profile_dir(current_runtime, profile_dir)
    relation = (
        "same-platform runtime"
        if current_runtime == source_state.source_runtime_id
        else "foreign runtime"
    )
    print(f"Profile mode: isolated {relation} (fresh bridge each startup)")
    if runtime_profile.exists():
        print(f"Previous runtime artifact present but not reused: {runtime_profile}")

    async def check_session() -> bool:
        valid = False
        try:
            set_headless(True)  # Always check headless
            browser = await get_or_create_browser()
            valid = browser.is_authenticated
        except AuthenticationError:
            return False
        except Exception as e:
            logger.exception(f"Unexpected error checking session: {e}")
            raise
        finally:
            if not await close_browser():
                raise NetworkError(
                    "Session validation finished, but browser teardown was not confirmed"
                )
        return valid

    try:
        valid = asyncio.run(check_session())
    except Exception as e:
        print(f"❌ Could not validate session: {e}")
        print("   Check logs and browser configuration.")
        sys.exit(1)

    if valid:
        print(
            f"✅ Session is valid (verified through isolated profile: {runtime_profile})"
        )
        sys.exit(0)

    print(f"❌ Session expired or invalid (isolated profile: {runtime_profile})")
    print("   Run with --login to re-authenticate")
    sys.exit(1)


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

            # Create and run the MCP server
            mcp = create_mcp_server(tool_timeout=config.server.tool_timeout_seconds)

            if transport == "streamable-http":
                mcp.run(
                    transport=transport,
                    host=config.server.host,
                    port=config.server.port,
                    path=config.server.path,
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
