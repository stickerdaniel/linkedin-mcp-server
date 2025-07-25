# linkedin_mcp_server/drivers/chrome.py
"""
Chrome WebDriver management for LinkedIn scraping with session persistence.

Handles Chrome WebDriver creation, configuration, authentication, and lifecycle management.
Implements singleton pattern for driver reuse across tools with automatic cleanup.
Provides cookie-based authentication and comprehensive error handling.
"""

import logging
import os
import platform
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
def get_default_user_agent() -> str:
    """Get platform-specific default user agent to reduce fingerprinting."""
    system = platform.system()

    if system == "Windows":
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    elif system == "Darwin":  # macOS
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    else:  # Linux and others
        return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"


# Global driver storage to reuse sessions
active_drivers: Dict[str, webdriver.Chrome] = {}


logger = logging.getLogger(__name__)


def create_chrome_options(config) -> Options:
    """
    Create Chrome options with all necessary configuration for LinkedIn scraping.

    Args:
        config: AppConfig instance with Chrome configuration

    Returns:
        Options: Configured Chrome options object
    """
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
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees")
    chrome_options.add_argument("--aggressive-cache-discard")
    chrome_options.add_argument("--disable-ipc-flooding-protection")

    # Set user agent (configurable with platform-specific default)
    user_agent = config.chrome.user_agent or get_default_user_agent()
    chrome_options.add_argument(f"--user-agent={user_agent}")

    # Add any custom browser arguments from config
    for arg in config.chrome.browser_args:
        chrome_options.add_argument(arg)

    return chrome_options


def create_chrome_service(config):
    """
    Create Chrome service with ChromeDriver path resolution.

    Args:
        config: AppConfig instance with Chrome configuration

    Returns:
        Service or None: Chrome service if path is configured, None for auto-detection
    """
    # Use ChromeDriver path from environment or config
    chromedriver_path = (
        os.environ.get("CHROMEDRIVER_PATH") or config.chrome.chromedriver_path
    )

    if chromedriver_path:
        logger.info(f"Using ChromeDriver at path: {chromedriver_path}")
        return Service(executable_path=chromedriver_path)
    else:
        logger.info("Using auto-detected ChromeDriver")
        return None


def create_temporary_chrome_driver() -> webdriver.Chrome:
    """
    Create a temporary Chrome WebDriver instance for one-off operations.

    This driver is NOT stored in the global active_drivers dict and should be
    manually cleaned up by the caller.

    Returns:
        webdriver.Chrome: Configured Chrome WebDriver instance

    Raises:
        WebDriverException: If driver creation fails
    """
    config = get_config()

    logger.info("Creating temporary Chrome WebDriver...")

    # Create Chrome options using shared function
    chrome_options = create_chrome_options(config)

    # Create Chrome service using shared function
    service = create_chrome_service(config)

    # Initialize Chrome driver
    if service:
        driver = webdriver.Chrome(service=service, options=chrome_options)
    else:
        driver = webdriver.Chrome(options=chrome_options)

    logger.info("Temporary Chrome WebDriver created successfully")

    # Add a page load timeout for safety
    driver.set_page_load_timeout(60)

    # Set shorter implicit wait for faster operations
    driver.implicitly_wait(10)

    return driver


def create_chrome_driver() -> webdriver.Chrome:
    """
    Create a new Chrome WebDriver instance with proper configuration.

    Returns:
        webdriver.Chrome: Configured Chrome WebDriver instance

    Raises:
        WebDriverException: If driver creation fails
    """
    config = get_config()

    logger.info("Initializing Chrome WebDriver...")

    # Create Chrome options using shared function
    chrome_options = create_chrome_options(config)

    # Create Chrome service using shared function
    service = create_chrome_service(config)

    # Initialize Chrome driver
    if service:
        driver = webdriver.Chrome(service=service, options=chrome_options)
    else:
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

        # Set timeout for cookie authentication - longer to handle LinkedIn's slow redirects
        driver.set_page_load_timeout(30)

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
        driver = create_chrome_driver()

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
    global active_drivers

    for session_id, driver in active_drivers.items():
        try:
            logger.info(f"Closing Chrome WebDriver session: {session_id}")
            driver.quit()
        except Exception as e:
            logger.warning(f"Error closing driver {session_id}: {e}")

    active_drivers.clear()
    logger.info("All Chrome WebDriver sessions closed")


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
