# linkedin_mcp_server/setup.py
"""
Interactive setup flows for LinkedIn MCP Server authentication configuration.

Handles credential collection, cookie extraction, validation, and secure storage
with multiple authentication methods including cookie input and credential-based login.
Provides temporary driver management and comprehensive retry logic.
"""

import logging
from contextlib import contextmanager
from typing import Dict, Iterator

import inquirer
from selenium import webdriver

from linkedin_mcp_server.authentication import store_authentication
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.messages import ErrorMessages, InfoMessages
from linkedin_mcp_server.config.providers import (
    get_credentials_from_keyring,
    save_credentials_to_keyring,
)
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.exceptions import CredentialsNotFoundError

logger = logging.getLogger(__name__)


def get_credentials_for_setup() -> Dict[str, str]:
    """
    Get LinkedIn credentials for setup purposes.

    Returns:
        Dict[str, str]: Dictionary with email and password

    Raises:
        CredentialsNotFoundError: If credentials cannot be obtained
    """
    config = get_config()

    # First, try configuration (includes environment variables)
    if config.linkedin.email and config.linkedin.password:
        logger.info("Using LinkedIn credentials from configuration")
        return {"email": config.linkedin.email, "password": config.linkedin.password}

    # Second, try keyring
    credentials = get_credentials_from_keyring()
    if credentials["email"] and credentials["password"]:
        logger.info("Using LinkedIn credentials from keyring")
        return {"email": credentials["email"], "password": credentials["password"]}

    # If in non-interactive mode and no credentials found, raise error
    if not config.is_interactive:
        raise CredentialsNotFoundError(ErrorMessages.no_credentials_found())

    # Otherwise, prompt for credentials
    return prompt_for_credentials()


def prompt_for_credentials() -> Dict[str, str]:
    """
    Prompt user for LinkedIn credentials.

    Returns:
        Dict[str, str]: Dictionary with email and password

    Raises:
        KeyboardInterrupt: If user cancels input
    """
    print("🔑 LinkedIn credentials required for setup")
    questions = [
        inquirer.Text("email", message="LinkedIn Email"),
        inquirer.Password("password", message="LinkedIn Password"),
    ]
    credentials: Dict[str, str] = inquirer.prompt(questions)

    if not credentials:
        raise KeyboardInterrupt("Credential input was cancelled")

    # Store credentials securely in keyring
    if save_credentials_to_keyring(credentials["email"], credentials["password"]):
        logger.info(InfoMessages.credentials_stored_securely())
    else:
        logger.warning(InfoMessages.keyring_storage_failed())

    return credentials


@contextmanager
def temporary_chrome_driver() -> Iterator[webdriver.Chrome]:
    """
    Context manager for creating temporary Chrome driver with automatic cleanup.

    Yields:
        webdriver.Chrome: Configured Chrome WebDriver instance

    Raises:
        Exception: If driver creation fails
    """
    from linkedin_mcp_server.drivers.chrome import create_temporary_chrome_driver

    driver = None
    try:
        # Create temporary driver using shared function
        driver = create_temporary_chrome_driver()
        yield driver
    finally:
        if driver:
            driver.quit()


def capture_cookie_from_credentials(email: str, password: str) -> str:
    """
    Login with credentials and capture session cookie using temporary driver.

    Args:
        email: LinkedIn email
        password: LinkedIn password

    Returns:
        str: Captured session cookie

    Raises:
        Exception: If login or cookie capture fails
    """
    with temporary_chrome_driver() as driver:
        # Login using linkedin-scraper
        from linkedin_scraper import actions

        config: AppConfig = get_config()
        interactive: bool = config.is_interactive
        logger.info(f"Logging in to LinkedIn... Interactive: {interactive}")
        actions.login(
            driver,
            email,
            password,
            timeout=60,  # longer timeout for login (captcha, mobile verification, etc.)
            interactive=interactive,  # type: ignore  # Respect configuration setting
        )

        # Capture cookie
        cookie_obj: Dict[str, str] = driver.get_cookie("li_at")
        if cookie_obj and cookie_obj.get("value"):
            cookie: str = cookie_obj["value"]
            logger.info("Successfully captured session cookie")
            return cookie
        else:
            raise Exception("Failed to capture session cookie from browser")


def test_cookie_validity(cookie: str) -> bool:
    """
    Test if a cookie is valid by attempting to use it with a temporary driver.

    Args:
        cookie: LinkedIn session cookie to test

    Returns:
        bool: True if cookie is valid, False otherwise
    """
    try:
        with temporary_chrome_driver() as driver:
            from linkedin_mcp_server.drivers.chrome import login_with_cookie

            return login_with_cookie(driver, cookie)
    except Exception as e:
        logger.warning(f"Cookie validation failed: {e}")
        return False


def prompt_for_cookie() -> str:
    """
    Prompt user to input LinkedIn cookie directly.

    Returns:
        str: LinkedIn session cookie

    Raises:
        KeyboardInterrupt: If user cancels input
        ValueError: If cookie format is invalid
    """
    print("🍪 Please provide your LinkedIn session cookie")
    cookie = inquirer.text("LinkedIn Cookie")

    if not cookie:
        raise KeyboardInterrupt("Cookie input was cancelled")

    # Normalize cookie format
    if cookie.startswith("li_at="):
        cookie: str = cookie.split("li_at=")[1]

    return cookie


def run_interactive_setup() -> str:
    """
    Run interactive setup to configure authentication.

    Returns:
        str: Configured LinkedIn session cookie

    Raises:
        Exception: If setup fails
    """
    print("🔗 LinkedIn MCP Server Setup")
    print("Choose how you'd like to authenticate:")

    # Ask user for setup method
    setup_method = inquirer.list_input(
        "Setup method",
        choices=[
            ("I have a LinkedIn cookie", "cookie"),
            ("Login with email/password to get cookie", "credentials"),
        ],
        default="cookie",
    )

    if setup_method == "cookie":
        # User provides cookie directly
        cookie = prompt_for_cookie()

        # Test the cookie with a temporary driver
        print("🔍 Testing provided cookie...")
        if test_cookie_validity(cookie):
            # Store the valid cookie
            store_authentication(cookie)
            logger.info("✅ Authentication configured successfully")
            return cookie
        else:
            print("❌ The provided cookie is invalid or expired")
            retry = inquirer.confirm(
                "Would you like to try with email/password instead?", default=True
            )
            if not retry:
                raise Exception("Setup cancelled - invalid cookie provided")

            # Fall through to credentials flow
            setup_method = "credentials"

    if setup_method == "credentials":
        # Get credentials and attempt login with retry
        max_retries = 3
        for attempt in range(max_retries):
            try:
                credentials = get_credentials_for_setup()

                print("🔑 Logging in to capture session cookie...")
                cookie = capture_cookie_from_credentials(
                    credentials["email"], credentials["password"]
                )

                # Store the captured cookie
                store_authentication(cookie)
                logger.info("✅ Authentication configured successfully")
                return cookie

            except Exception as e:
                logger.error(f"Login failed: {e}")
                print(f"❌ Login failed: {e}")

                if attempt < max_retries - 1:
                    retry = inquirer.confirm(
                        "Would you like to try with different credentials?",
                        default=True,
                    )
                    if not retry:
                        break
                    # Clear stored credentials to prompt for new ones
                    from linkedin_mcp_server.config.providers import (
                        clear_credentials_from_keyring,
                    )

                    clear_credentials_from_keyring()
                else:
                    raise Exception(f"Setup failed after {max_retries} attempts")

        raise Exception("Setup cancelled by user")

    # This should never be reached, but ensures type checker knows all paths are covered
    raise Exception("Unexpected setup flow completion")


def run_cookie_extraction_setup() -> str:
    """
    Run setup specifically for cookie extraction (--get-cookie mode).

    Returns:
        str: Captured LinkedIn session cookie for display

    Raises:
        Exception: If setup fails
    """
    logger.info("🔗 LinkedIn MCP Server - Cookie Extraction mode started")
    print("🔗 LinkedIn MCP Server - Cookie Extraction")

    # Get credentials
    credentials: Dict[str, str] = get_credentials_for_setup()

    # Capture cookie
    cookie: str = capture_cookie_from_credentials(
        credentials["email"], credentials["password"]
    )

    return cookie
