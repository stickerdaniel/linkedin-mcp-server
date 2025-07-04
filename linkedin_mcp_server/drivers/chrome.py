# src/linkedin_mcp_server/drivers/chrome.py
"""
Chrome driver management for LinkedIn scraping.

This module handles the creation and management of Chrome WebDriver instances.
"""

import os
import sys
from typing import Dict, Optional

import inquirer  # type: ignore
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.providers import clear_credentials_from_keyring
from linkedin_mcp_server.config.secrets import get_credentials

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

        # Try to log in
        if login_to_linkedin(driver):
            print("Successfully logged in to LinkedIn")
        elif config.chrome.non_interactive:
            # In non-interactive mode, if login fails, return None
            driver.quit()
            return None

        active_drivers[session_id] = driver
        return driver
    except Exception as e:
        error_msg = f"🛑 Error creating web driver: {e}"
        print(error_msg)

        if config.chrome.non_interactive:
            print("🛑 Failed to initialize driver in non-interactive mode")
            return None

        raise WebDriverException(error_msg)


def login_to_linkedin(driver: webdriver.Chrome) -> bool:
    """
    Log in to LinkedIn using stored or provided credentials.

    Args:
        driver: Chrome WebDriver instance

    Returns:
        bool: True if login was successful, False otherwise
    """
    config = get_config()

    # Get LinkedIn credentials from config
    credentials = get_credentials()

    if not credentials:
        print("❌ No credentials available")
        return False

    try:
        # Login to LinkedIn
        print("🔑 Logging in to LinkedIn...")

        from linkedin_scraper import actions  # type: ignore

        actions.login(driver, credentials["email"], credentials["password"])

        print("✅ Successfully logged in to LinkedIn")
        return True
    except Exception as e:
        error_msg = f"Failed to login: {str(e)}"
        print(f"❌ {error_msg}")

        if not config.chrome.non_interactive:
            print(
                "⚠️ You might need to confirm the login in your LinkedIn mobile app. "
                "Please try again and confirm the login."
            )

            if config.chrome.headless:
                print(
                    "🔍 Try running with visible browser window to see what's happening: "
                    "uv run main.py --no-headless"
                )

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
                # Clear credentials from keyring and try again
                clear_credentials_from_keyring()
                # Try again with new credentials
                return login_to_linkedin(driver)

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
            print("❌ Failed to initialize web driver.")
    except WebDriverException as e:
        print(f"❌ Failed to initialize web driver: {str(e)}")
        handle_driver_error()


def handle_driver_error() -> None:
    """
    Handle ChromeDriver initialization errors by providing helpful options.
    """
    config = get_config()

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
            # Try again with the new path
            initialize_driver()
        else:
            print(f"⚠️ Warning: The specified path does not exist: {path}")
            initialize_driver()

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
