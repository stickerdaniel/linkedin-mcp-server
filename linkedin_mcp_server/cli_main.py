"""
LinkedIn MCP Server - Main CLI application entry point.

Implements a simplified two-phase startup:
1. Authentication Check - Verify browser profile is available
2. Server Runtime - MCP server startup with transport selection
"""

import asyncio
import logging
import sys
from typing import Literal

import inquirer

from linkedin_mcp_server.core import AuthenticationError, RateLimitError

from linkedin_mcp_server.authentication import (
    clear_auth_state,
    get_authentication_source,
)
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.browser import (
    experimental_persist_derived_runtime,
    close_browser,
    get_or_create_browser,
    get_profile_dir,
    profile_exists,
    set_headless,
)
from linkedin_mcp_server.debug_trace import should_keep_traces
from linkedin_mcp_server.exceptions import CredentialsNotFoundError
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
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.setup import run_interactive_setup, run_profile_creation

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


def ensure_authentication_ready() -> None:
    """
    Phase 1: Ensure authentication is ready.

    Checks for existing browser profile.
    If not found, runs interactive setup in interactive mode.

    Raises:
        CredentialsNotFoundError: If authentication setup fails
    """
    config = get_config()

    # Check for existing profile
    try:
        get_authentication_source()
        return

    except CredentialsNotFoundError:
        pass

    # No authentication found - try interactive setup if possible
    if not config.is_interactive:
        raise CredentialsNotFoundError(
            "No LinkedIn profile found.\n"
            "Options:\n"
            "  1. Run with --login to create a profile\n"
            "  2. Run with --no-headless to login interactively"
        )

    # Run interactive setup
    logger.info("No authentication found, starting interactive setup...")
    success = run_interactive_setup()

    if not success:
        raise CredentialsNotFoundError("Interactive setup was cancelled or failed")


def get_version() -> str:
    """Get version from installed metadata with a source fallback."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        for package_name in ("linkedin-scraper-mcp", "linkedin-mcp-server"):
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
        # Set headless mode from config
        set_headless(config.browser.headless)

        # Handle --logout flag
        if config.server.logout:
            clear_profile_and_exit()

        # Handle --login flag
        if config.server.login:
            get_profile_and_exit()

        # Handle --status flag
        if config.server.status:
            profile_info_and_exit()

        logger.debug(f"Server configuration: {config}")

        # Phase 1: Ensure Authentication is Ready
        try:
            ensure_authentication_ready()
            if config.is_interactive:
                print("✅ Authentication ready")
            logger.info("Authentication ready")

        except CredentialsNotFoundError as e:
            logger.error(f"Authentication setup failed: {e}")
            if config.is_interactive:
                print("\n❌ Authentication required")
                print(str(e))
            sys.exit(1)

        except KeyboardInterrupt:
            if config.is_interactive:
                print("\n\n👋 Setup cancelled by user")
            sys.exit(0)

        except (AuthenticationError, RateLimitError) as e:
            logger.error(f"LinkedIn error during setup: {e}")
            if config.is_interactive:
                print(f"\n❌ {str(e)}")
            sys.exit(1)

        except Exception as e:
            logger.exception(f"Unexpected error during authentication setup: {e}")
            if config.is_interactive:
                print(f"\n❌ Setup failed: {e}")
            sys.exit(1)

        # Phase 2: Server Runtime
        try:
            transport = config.server.transport

            # Prompt for transport in interactive mode if not explicitly set
            if config.is_interactive and not config.server.transport_explicitly_set:
                print("\n🚀 Server ready! Choose transport mode:")
                transport = choose_transport_interactive()

            # Create and run the MCP server
            mcp = create_mcp_server()

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
