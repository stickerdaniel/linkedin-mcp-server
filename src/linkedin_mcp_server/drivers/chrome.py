# src/linkedin_mcp_server/drivers/chrome.py
"""
Chrome driver management for LinkedIn scraping.

This module handles the creation and management of Chrome WebDriver instances.
"""

from typing import Dict, Optional, List, Any
import os
import sys
import logging
from pathlib import Path
import inquirer
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import WebDriverException

from linkedin_mcp_server.credentials import get_credentials

# Global driver storage to reuse sessions
active_drivers: Dict[str, webdriver.Chrome] = {}
is_initialized: bool = False
driver_config: Dict[str, Any] = {
    "headless": True,
    "non_interactive": False,
}

logger = logging.getLogger(__name__)


def get_chromedriver_path() -> Optional[str]:
    """
    Get the ChromeDriver path from environment variable or default locations.

    Returns:
        Optional[str]: Path to the ChromeDriver executable if found, None otherwise
    """
    # First check environment variable
    chromedriver_path = os.getenv("CHROMEDRIVER")
    if chromedriver_path and os.path.exists(chromedriver_path):
        return chromedriver_path

    # Check common locations
    possible_paths: List[str] = [
        os.path.join(os.path.dirname(__file__), "../../../drivers/chromedriver"),
        os.path.join(os.path.expanduser("~"), "chromedriver"),
        "/usr/local/bin/chromedriver",
        "/usr/bin/chromedriver",
        # Common MacOS paths
        "/opt/homebrew/bin/chromedriver",
        "/Applications/chromedriver",
        # Common Windows paths
        "C:\\Program Files\\chromedriver.exe",
        "C:\\Program Files (x86)\\chromedriver.exe",
    ]

    for path in possible_paths:
        if os.path.exists(path) and (os.access(path, os.X_OK) or path.endswith(".exe")):
            return path

    return None


def configure_driver(headless: bool = True, non_interactive: bool = False) -> None:
    """
    Configure the driver settings without initializing it.

    Args:
        headless: Whether to run Chrome in headless mode
        non_interactive: Whether to run in non-interactive mode (for Docker/CI)
    """
    global driver_config
    driver_config["headless"] = headless
    driver_config["non_interactive"] = non_interactive
    logger.info(
        f"Driver configured: headless={headless}, non_interactive={non_interactive}"
    )


def get_or_create_driver() -> Optional[webdriver.Chrome]:
    """
    Get existing driver or create a new one using the configured settings.

    Returns:
        Optional[webdriver.Chrome]: Chrome WebDriver instance or None if initialization fails
                                   in non-interactive mode

    Raises:
        WebDriverException: If the driver cannot be created and not in non-interactive mode
    """
    global is_initialized
    session_id = "default"  # We use a single session for simplicity

    # Return existing driver if available
    if session_id in active_drivers:
        return active_drivers[session_id]

    headless = driver_config["headless"]
    non_interactive = driver_config["non_interactive"]

    # Set up Chrome options
    chrome_options = Options()
    if headless:
        logger.debug("Running Chrome in headless mode")
        chrome_options.add_argument("--headless=new")
    else:
        logger.debug("Running Chrome with visible browser window")

    # Add additional options for stability
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
    )

    # Initialize Chrome driver
    try:
        chromedriver_path = get_chromedriver_path()
        if chromedriver_path:
            logger.debug(f"Using ChromeDriver at path: {chromedriver_path}")
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            logger.debug("Using auto-detected ChromeDriver")
            driver = webdriver.Chrome(options=chrome_options)

        # Add a page load timeout for safety
        driver.set_page_load_timeout(60)

        # Try to log in if we haven't already
        if not is_initialized:
            if login_to_linkedin(driver, non_interactive):
                is_initialized = True
            elif non_interactive:
                # In non-interactive mode, if login fails, return None
                driver.quit()
                return None

        active_drivers[session_id] = driver
        return driver
    except Exception as e:
        error_msg = f"Error creating web driver: {e}"
        logger.error(error_msg)

        if non_interactive:
            logger.error("Failed to initialize driver in non-interactive mode")
            return None

        raise WebDriverException(error_msg)


def login_to_linkedin(driver: webdriver.Chrome, non_interactive: bool = False) -> bool:
    """
    Log in to LinkedIn using stored or provided credentials.

    Args:
        driver: Chrome WebDriver instance
        non_interactive: Whether to run in non-interactive mode

    Returns:
        bool: True if login was successful, False otherwise
    """
    # Get credentials
    credentials = get_credentials(non_interactive=non_interactive)

    if not credentials:
        if non_interactive:
            logger.error("No credentials available in non-interactive mode")
            return False
        else:
            logger.error("Failed to obtain LinkedIn credentials")
            return False

    try:
        from linkedin_scraper import actions

        # Login to LinkedIn
        logger.info("Logging in to LinkedIn...")
        if not non_interactive:
            print("🔑 Logging in to LinkedIn...")

        actions.login(driver, credentials["email"], credentials["password"])

        if not non_interactive:
            print("✅ Successfully logged in to LinkedIn")
        logger.info("Successfully logged in to LinkedIn")
        return True
    except Exception as e:
        error_msg = f"Failed to login: {str(e)}"
        logger.error(error_msg)

        if not non_interactive:
            print(f"❌ {error_msg}")
            print(
                "⚠️ You might need to confirm the login in your LinkedIn mobile app. "
                "Please try again and confirm the login."
            )

            if driver_config["headless"]:
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
                # Remove old credentials and try again
                credentials_file = Path.home() / ".linkedin_mcp_credentials.json"
                if credentials_file.exists():
                    os.remove(credentials_file)
                # Try again with new credentials
                return login_to_linkedin(driver, non_interactive)

        return False


def initialize_driver(headless: bool = True, lazy_init: bool = False) -> None:
    """
    Initialize the driver configuration and optionally create driver and log in.

    Args:
        headless: Whether to run Chrome in headless mode
        lazy_init: If True, only configure the driver without creating it
                  (driver will be created on first tool call)
    """
    # Always configure the driver
    configure_driver(headless=headless, non_interactive=lazy_init)

    if lazy_init:
        logger.info(
            "Using lazy initialization - driver will be created on first tool call"
        )
        if "LINKEDIN_EMAIL" in os.environ and "LINKEDIN_PASSWORD" in os.environ:
            logger.info("LinkedIn credentials found in environment variables")
        else:
            logger.warning(
                "No LinkedIn credentials in environment variables - will look for stored credentials on first use"
            )
        return

    # Validate chromedriver can be found
    chromedriver_path = get_chromedriver_path()

    if chromedriver_path:
        print(f"✅ ChromeDriver found at: {chromedriver_path}")
        os.environ["CHROMEDRIVER"] = chromedriver_path
    else:
        print("⚠️ ChromeDriver not found in common locations.")
        print("⚡ Continuing with automatic detection...")
        print(
            "💡 Tip: For better results, install ChromeDriver and set the CHROMEDRIVER environment variable"
        )

    # Create driver and log in
    try:
        driver = get_or_create_driver()
        if driver:
            print("✅ Web driver initialized successfully")
            print(
                f"🌐 Browser is running in {'headless' if headless else 'visible'} mode"
            )
        else:
            print("❌ Failed to initialize web driver.")
    except WebDriverException as e:
        print(f"❌ Failed to initialize web driver: {str(e)}")
        handle_driver_error(headless)


def handle_driver_error(headless: bool) -> None:
    """
    Handle ChromeDriver initialization errors by providing helpful options.

    Args:
        headless: Whether Chrome is running in headless mode
    """
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
            os.environ["CHROMEDRIVER"] = path
            print(f"✅ ChromeDriver path set to: {path}")
            # Try again with the new path
            initialize_driver(headless=headless)
        else:
            print(f"⚠️ Warning: The specified path does not exist: {path}")
            initialize_driver(headless=headless)

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
            initialize_driver(headless=headless)

    print("❌ ChromeDriver is required for this application to work properly.")
    sys.exit(1)
