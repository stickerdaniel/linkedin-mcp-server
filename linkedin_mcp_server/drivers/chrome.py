# src/linkedin_mcp_server/drivers/chrome.py
"""
Chrome driver management for LinkedIn scraping.

This module handles the creation and management of Chrome WebDriver instances.
"""

import os
import sys
from typing import Dict, Optional

import inquirer  # type: ignore
from linkedin_scraper.exceptions import (
    CaptchaRequiredError,
    InvalidCredentialsError,
    LoginTimeoutError,
    RateLimitError,
    SecurityChallengeError,
    TwoFactorAuthError,
)
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.providers import clear_credentials_from_keyring
from linkedin_mcp_server.config.secrets import get_credentials
from linkedin_mcp_server.exceptions import (
    CredentialsNotFoundError,
    DriverInitializationError,
)

# Global driver storage to reuse sessions
active_drivers: Dict[str, webdriver.Chrome] = {}


def get_or_create_driver() -> Optional[webdriver.Chrome]:
    """
    Get existing driver or create a new one using the configured settings.

    Returns:
        Optional[webdriver.Chrome]: Chrome WebDriver instance or None if initialization fails
                                   in non-interactive mode

    Raises:
        WebDriverException: If the driver cannot be created and not in non-interactive mode
    """
    config = get_config()
    session_id = "default"  # We use a single session for simplicity

    # Return existing driver if available
    if session_id in active_drivers:
        return active_drivers[session_id]

    # Set up Chrome options
    chrome_options = Options()
    print(
        f"🌐 Running browser in {'headless' if config.chrome.headless else 'visible'} mode"
    )
    if config.chrome.headless:
        chrome_options.add_argument("--headless=new")

    # Add essential options for stability (compatible with both Grid and direct)
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
    )

    # Add any custom browser arguments from config
    for arg in config.chrome.browser_args:
        chrome_options.add_argument(arg)

    # Initialize Chrome driver
    try:
        print("🌐 Initializing Chrome WebDriver...")

        # Use ChromeDriver path from environment or config
        chromedriver_path = (
            os.environ.get("CHROMEDRIVER_PATH") or config.chrome.chromedriver_path
        )

        if chromedriver_path:
            print(f"🌐 Using ChromeDriver at path: {chromedriver_path}")
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            print("🌐 Using auto-detected ChromeDriver")
            driver = webdriver.Chrome(options=chrome_options)

        print("✅ Chrome WebDriver initialized successfully")

        # Add a page load timeout for safety
        driver.set_page_load_timeout(60)

        # Try to log in with retry loop
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if login_to_linkedin(driver):
                    print("Successfully logged in to LinkedIn")
                    active_drivers[session_id] = driver
                    return driver
            except (
                CaptchaRequiredError,
                InvalidCredentialsError,
                SecurityChallengeError,
                TwoFactorAuthError,
                RateLimitError,
                LoginTimeoutError,
                CredentialsNotFoundError,
            ) as e:
                if config.chrome.non_interactive:
                    # In non-interactive mode, propagate the error
                    driver.quit()
                    raise e
                else:
                    # In interactive mode, handle the error and potentially retry
                    should_retry = handle_login_error(e)
                    if should_retry and attempt < max_retries - 1:
                        print(f"🔄 Retry attempt {attempt + 2}/{max_retries}")
                        continue
                    else:
                        # Clean up driver on final failure
                        driver.quit()
                        return None
    except Exception as e:
        error_msg = f"🛑 Error creating web driver: {e}"
        print(error_msg)

        if config.chrome.non_interactive:
            raise DriverInitializationError(error_msg)
        else:
            raise WebDriverException(error_msg)


def login_to_linkedin(driver: webdriver.Chrome) -> bool:
    """
    Log in to LinkedIn using stored or provided credentials.

    Args:
        driver: Chrome WebDriver instance

    Returns:
        bool: True if login was successful, False otherwise

    Raises:
        Various login-related errors from linkedin-scraper
    """
    config = get_config()

    # Get LinkedIn credentials from config
    try:
        credentials = get_credentials()
    except CredentialsNotFoundError as e:
        if config.chrome.non_interactive:
            raise e
        # Only prompt if not in non-interactive mode
        from linkedin_mcp_server.config.secrets import prompt_for_credentials

        credentials = prompt_for_credentials()

    if not credentials:
        raise CredentialsNotFoundError("No credentials available")

    # Login to LinkedIn using enhanced linkedin-scraper
    print("🔑 Logging in to LinkedIn...")

    from linkedin_scraper import actions  # type: ignore

    # Use linkedin-scraper login but with simplified error handling
    try:
        actions.login(
            driver,
            credentials["email"],
            credentials["password"],
            interactive=not config.chrome.non_interactive,
        )

        print("✅ Successfully logged in to LinkedIn")
        return True

    except Exception:
        # Check current page to determine the real issue
        current_url = driver.current_url

        if "checkpoint/challenge" in current_url:
            # We're on a challenge page - this is the real issue, not credentials
            if "security check" in driver.page_source.lower():
                raise SecurityChallengeError(
                    challenge_url=current_url,
                    message="LinkedIn requires a security challenge. Please complete it manually and restart the application.",
                )
            else:
                raise CaptchaRequiredError(
                    captcha_url=current_url,
                )

        elif "feed" in current_url or "mynetwork" in current_url:
            # Actually logged in successfully despite the exception
            print("✅ Successfully logged in to LinkedIn")
            return True

        else:
            # Check for actual credential issues
            page_source = driver.page_source.lower()
            if any(
                pattern in page_source
                for pattern in ["wrong email", "wrong password", "incorrect", "invalid"]
            ):
                raise InvalidCredentialsError("Invalid LinkedIn email or password.")
            elif "too many" in page_source:
                raise RateLimitError(
                    "Too many login attempts. Please wait and try again later."
                )
            else:
                raise LoginTimeoutError(
                    "Login failed. Please check your credentials and network connection."
                )


def handle_login_error(error: Exception) -> bool:
    """Handle login errors in interactive mode.

    Returns:
        bool: True if user wants to retry, False if they want to exit
    """
    config = get_config()

    print(f"\n❌ {str(error)}")

    if config.chrome.headless:
        print(
            "🔍 Try running with visible browser window: uv run main.py --no-headless"
        )

    # Only allow retry for credential errors
    if isinstance(error, InvalidCredentialsError):
        retry = inquirer.prompt(
            [
                inquirer.Confirm(
                    "retry",
                    message="Would you like to try with different credentials?",
                    default=True,
                ),
            ]
        )
        if retry and retry.get("retry", False):
            clear_credentials_from_keyring()
            print("✅ Credentials cleared from keyring.")
            print("🔄 Retrying with new credentials...")
            return True

    return False


def initialize_driver() -> None:
    """
    Initialize the driver based on global configuration.
    """
    config = get_config()

    if config.server.lazy_init:
        print("Using lazy initialization - driver will be created on first tool call")
        if config.linkedin.email and config.linkedin.password:
            print("LinkedIn credentials found in configuration")
        else:
            print(
                "No LinkedIn credentials found - will look for stored credentials on first use"
            )
        return

    # Validate chromedriver can be found
    if config.chrome.chromedriver_path:
        print(f"✅ ChromeDriver found at: {config.chrome.chromedriver_path}")
        os.environ["CHROMEDRIVER"] = config.chrome.chromedriver_path
    else:
        print("⚠️ ChromeDriver not found in common locations.")
        print("⚡ Continuing with automatic detection...")
        print(
            "💡 Tip: install ChromeDriver and set the CHROMEDRIVER environment variable"
        )

    # Create driver and log in
    try:
        driver = get_or_create_driver()
        if driver:
            print("✅ Web driver initialized successfully")
        else:
            # Driver creation failed - always raise an error
            raise DriverInitializationError("Failed to initialize web driver")
    except (
        CaptchaRequiredError,
        InvalidCredentialsError,
        SecurityChallengeError,
        TwoFactorAuthError,
        RateLimitError,
        LoginTimeoutError,
        CredentialsNotFoundError,
    ) as e:
        # Always re-raise login-related errors so main.py can handle them
        raise e
    except WebDriverException as e:
        if config.chrome.non_interactive:
            raise DriverInitializationError(
                f"Failed to initialize web driver: {str(e)}"
            )
        print(f"❌ Failed to initialize web driver: {str(e)}")
        handle_driver_error()


def handle_driver_error() -> None:
    """
    Handle ChromeDriver initialization errors by providing helpful options.
    """
    config = get_config()

    # Skip interactive handling in non-interactive mode
    if config.chrome.non_interactive:
        print("❌ ChromeDriver is required for this application to work properly.")
        sys.exit(1)

    questions = [
        inquirer.List(
            "chromedriver_action",
            message="What would you like to do?",
            choices=[
                ("Specify ChromeDriver path manually", "specify"),
                ("Get help installing ChromeDriver", "help"),
                ("Exit", "exit"),
            ],
        ),
    ]
    answers = inquirer.prompt(questions)

    if answers["chromedriver_action"] == "specify":
        path = inquirer.prompt(
            [inquirer.Text("custom_path", message="Enter ChromeDriver path")]
        )["custom_path"]

        if os.path.exists(path):
            # Update config with the new path
            config.chrome.chromedriver_path = path
            os.environ["CHROMEDRIVER"] = path
            print(f"✅ ChromeDriver path set to: {path}")
            print("💡 Please restart the application to use the new ChromeDriver path.")
            print("   Example: uv run main.py")
            sys.exit(0)
        else:
            print(f"⚠️ Warning: The specified path does not exist: {path}")
            print("💡 Please check the path and restart the application.")
            sys.exit(1)

    elif answers["chromedriver_action"] == "help":
        print("\n📋 ChromeDriver Installation Guide:")
        print("1. Find your Chrome version: Chrome menu > Help > About Google Chrome")
        print(
            "2. Download matching ChromeDriver: https://chromedriver.chromium.org/downloads"
        )
        print("3. Place ChromeDriver in a location on your PATH")
        print("   - macOS/Linux: /usr/local/bin/ is recommended")
        print(
            "   - Windows: Add to a directory in your PATH or specify the full path\n"
        )

        if inquirer.prompt(
            [inquirer.Confirm("try_again", message="Try again?", default=True)]
        )["try_again"]:
            initialize_driver()

    print("❌ ChromeDriver is required for this application to work properly.")
    sys.exit(1)
