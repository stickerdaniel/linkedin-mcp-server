# linkedin_mcp_server/drivers/chrome.py
"""
Chrome driver management for LinkedIn scraping.

This module handles the creation and management of Chrome WebDriver instances.
Simplified to focus only on driver management without authentication setup.
"""

import logging
import os
import shutil
import tempfile
from typing import Dict, Optional

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
from linkedin_mcp_server.exceptions import DriverInitializationError

# Constants
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"

# Global driver storage to reuse sessions
active_drivers: Dict[str, webdriver.Chrome] = {}

# Store user data directories for cleanup
user_data_dirs: Dict[str, str] = {}

logger = logging.getLogger(__name__)


def create_chrome_driver(session_id: str = "default") -> webdriver.Chrome:
    """
    Create a new Chrome WebDriver instance with proper configuration.

    Args:
        session_id: Unique identifier for the session (used for cleanup)

    Returns:
        webdriver.Chrome: Configured Chrome WebDriver instance

    Raises:
        WebDriverException: If driver creation fails
    """
    config = get_config()

    # Set up Chrome options
    chrome_options = Options()
    logger.info(
        f"Running browser in {'headless' if config.chrome.headless else 'visible'} mode"
    )
    if config.chrome.headless:
        chrome_options.add_argument("--headless=new")

    # Add essential options for stability
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-background-timer-throttling")

    # Create a unique user data directory to avoid conflicts
    user_data_dir = tempfile.mkdtemp(prefix="linkedin_mcp_chrome_")
    chrome_options.add_argument(f"--user-data-dir={user_data_dir}")
    logger.debug(f"Using Chrome user data directory: {user_data_dir}")

    # Store the user data directory for cleanup
    user_data_dirs[session_id] = user_data_dir

    # Set user agent (configurable with sensible default)
    user_agent = getattr(config.chrome, "user_agent", DEFAULT_USER_AGENT)
    chrome_options.add_argument(f"--user-agent={user_agent}")

    # Add any custom browser arguments from config
    for arg in config.chrome.browser_args:
        chrome_options.add_argument(arg)

    # Initialize Chrome driver
    logger.info("Initializing Chrome WebDriver...")

    # Use ChromeDriver path from environment or config
    chromedriver_path = (
        os.environ.get("CHROMEDRIVER_PATH") or config.chrome.chromedriver_path
    )

    if chromedriver_path:
        logger.info(f"Using ChromeDriver at path: {chromedriver_path}")
        service = Service(executable_path=chromedriver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)
    else:
        logger.info("Using auto-detected ChromeDriver")
        driver = webdriver.Chrome(options=chrome_options)

    logger.info("Chrome WebDriver initialized successfully")

    # Add a page load timeout for safety
    driver.set_page_load_timeout(60)

    # Set shorter implicit wait for faster cookie validation
    driver.implicitly_wait(10)

    return driver


def login_with_cookie(driver: webdriver.Chrome, cookie: str) -> bool:
    """
    Log in to LinkedIn using session cookie.

    Args:
        driver: Chrome WebDriver instance
        cookie: LinkedIn session cookie

    Returns:
        bool: True if login was successful, False otherwise
    """
    try:
        from linkedin_scraper import actions  # type: ignore

        logger.info("Attempting cookie authentication...")

        # Set shorter timeout for faster failure detection
        driver.set_page_load_timeout(15)

        actions.login(driver, cookie=cookie)

        # Quick check - if we're on login page, cookie is invalid
        current_url = driver.current_url
        if "login" in current_url or "uas/login" in current_url:
            logger.warning("Cookie authentication failed - redirected to login page")
            return False
        elif (
            "feed" in current_url
            or "mynetwork" in current_url
            or "linkedin.com/in/" in current_url
        ):
            logger.info("Cookie authentication successful")
            return True
        else:
            logger.warning("Cookie authentication failed - unexpected page")
            return False

    except Exception as e:
        logger.warning(f"Cookie authentication failed: {e}")
        return False
    finally:
        # Restore normal timeout
        driver.set_page_load_timeout(60)


def login_to_linkedin(driver: webdriver.Chrome, authentication: str) -> None:
    """
    Log in to LinkedIn using provided authentication.

    Args:
        driver: Chrome WebDriver instance
        authentication: LinkedIn session cookie

    Raises:
        Various login-related errors from linkedin-scraper or this module
    """
    # Try cookie authentication
    if login_with_cookie(driver, authentication):
        logger.info("Successfully logged in to LinkedIn using cookie")
        return

    # If we get here, cookie authentication failed
    logger.error("Cookie authentication failed")

    # Clear invalid cookie from keyring
    from linkedin_mcp_server.authentication import clear_authentication

    clear_authentication()
    logger.info("Cleared invalid cookie from authentication storage")

    # Check current page to determine the issue
    try:
        current_url: str = driver.current_url

        if "checkpoint/challenge" in current_url:
            if "security check" in driver.page_source.lower():
                raise SecurityChallengeError(
                    challenge_url=current_url,
                    message="LinkedIn requires a security challenge. Please complete it manually and restart the application.",
                )
            else:
                raise CaptchaRequiredError(captcha_url=current_url)
        else:
            raise InvalidCredentialsError(
                "Cookie authentication failed - cookie may be expired or invalid"
            )

    except Exception as e:
        # If we can't determine the specific error, raise a generic one
        raise LoginTimeoutError(f"Login failed: {str(e)}")


def get_or_create_driver(authentication: str) -> webdriver.Chrome:
    """
    Get existing driver or create a new one and login.

    Args:
        authentication: LinkedIn session cookie for login

    Returns:
        webdriver.Chrome: Chrome WebDriver instance, logged in and ready

    Raises:
        DriverInitializationError: If driver creation fails
        Various login-related errors: If login fails
    """
    session_id = "default"  # We use a single session for simplicity

    # Return existing driver if available
    if session_id in active_drivers:
        logger.info("Using existing Chrome WebDriver session")
        return active_drivers[session_id]

    try:
        # Create new driver
        driver = create_chrome_driver(session_id)

        # Login to LinkedIn
        login_to_linkedin(driver, authentication)

        # Store successful driver
        active_drivers[session_id] = driver
        logger.info("Chrome WebDriver session created and authenticated successfully")

        return driver

    except WebDriverException as e:
        error_msg = f"Error creating web driver: {e}"
        logger.error(error_msg)
        raise DriverInitializationError(error_msg)
    except (
        CaptchaRequiredError,
        InvalidCredentialsError,
        SecurityChallengeError,
        TwoFactorAuthError,
        RateLimitError,
        LoginTimeoutError,
    ) as e:
        # Login-related errors - clean up driver if it was created
        if session_id in active_drivers:
            active_drivers[session_id].quit()
            del active_drivers[session_id]
        raise e


def close_all_drivers() -> None:
    """Close all active drivers and clean up resources."""
    global active_drivers, user_data_dirs

    for session_id, driver in active_drivers.items():
        try:
            logger.info(f"Closing Chrome WebDriver session: {session_id}")
            driver.quit()
        except Exception as e:
            logger.warning(f"Error closing driver {session_id}: {e}")

        # Clean up user data directory
        if session_id in user_data_dirs:
            try:
                user_data_dir = user_data_dirs[session_id]
                if os.path.exists(user_data_dir):
                    shutil.rmtree(user_data_dir)
                    logger.debug(f"Cleaned up user data directory: {user_data_dir}")
            except Exception as e:
                logger.warning(
                    f"Error cleaning up user data directory for session {session_id}: {e}"
                )

    active_drivers.clear()
    user_data_dirs.clear()
    logger.info("All Chrome WebDriver sessions closed and cleaned up")


def get_active_driver() -> Optional[webdriver.Chrome]:
    """
    Get the currently active driver without creating a new one.

    Returns:
        Optional[webdriver.Chrome]: Active driver if available, None otherwise
    """
    session_id = "default"
    return active_drivers.get(session_id)


def capture_session_cookie(driver: webdriver.Chrome) -> Optional[str]:
    """
    Capture LinkedIn session cookie from driver.

    Args:
        driver: Chrome WebDriver instance

    Returns:
        Optional[str]: Session cookie if found, None otherwise
    """
    try:
        # Get li_at cookie which is the main LinkedIn session cookie
        cookie = driver.get_cookie("li_at")
        if cookie and cookie.get("value"):
            return f"li_at={cookie['value']}"
        return None
    except Exception as e:
        logger.warning(f"Failed to capture session cookie: {e}")
        return None
