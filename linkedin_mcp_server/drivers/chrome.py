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
    import time

    start_time = time.time()

    try:
        from linkedin_scraper import actions  # type: ignore
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.support.ui import WebDriverWait

        logger.info("Attempting cookie authentication...")
        logger.debug(f"Cookie value (first 20 chars): {cookie[:20]}...")
        logger.debug(f"Cookie length: {len(cookie)}")

        # Log initial state
        try:
            initial_url = driver.current_url
            logger.debug(f"Initial URL before login: {initial_url}")
        except Exception as e:
            logger.debug(f"Could not get initial URL: {e}")

        # Set longer timeout to handle slow LinkedIn loading
        driver.set_page_load_timeout(45)
        logger.debug("Set page load timeout to 45 seconds")

        # Attempt login with detailed logging
        login_start_time = time.time()
        login_exception = None
        try:
            logger.debug("Calling actions.login() with cookie...")
            actions.login(driver, cookie=cookie)
            login_duration = time.time() - login_start_time
            logger.debug(
                f"actions.login() completed successfully in {login_duration:.2f} seconds"
            )
        except TimeoutException as e:
            login_duration = time.time() - login_start_time
            login_exception = e
            logger.info(
                f"Page load timeout during login after {login_duration:.2f}s - will check if authentication succeeded anyway"
            )
            logger.debug(f"TimeoutException details: {e}")
        except Exception as e:
            login_duration = time.time() - login_start_time
            login_exception = e
            logger.warning(
                f"actions.login() threw exception after {login_duration:.2f}s: {e}"
            )
            logger.debug(f"Exception type: {type(e).__name__}")

            # Special handling for InvalidCredentialsError from linkedin-scraper
            # This library sometimes incorrectly reports authentication failure even when login succeeds
            if "InvalidCredentialsError" in str(
                type(e)
            ) or "Cookie login failed" in str(e):
                logger.info(
                    "Detected InvalidCredentialsError from linkedin-scraper library"
                )
                logger.info(
                    "This is a known issue - will verify authentication status manually"
                )
                logger.info(
                    "The browser may have logged in successfully despite the exception"
                )
            else:
                logger.info(
                    "Will check authentication status despite the exception - the browser may have logged in successfully"
                )

            # Don't raise the exception immediately - check if login actually worked first

        # Log URL immediately after login attempt
        try:
            post_login_url = driver.current_url
            logger.debug(f"URL immediately after login attempt: {post_login_url}")
        except Exception as e:
            logger.debug(f"Could not get URL after login attempt: {e}")

        # Wait for page to stabilize after login attempt
        # Give LinkedIn time to redirect and load properly
        logger.debug("Waiting 3 seconds for page stabilization...")
        time.sleep(3)

        # Log URL after stabilization wait
        try:
            stabilized_url = driver.current_url
            logger.debug(f"URL after 3-second stabilization wait: {stabilized_url}")
        except Exception as e:
            logger.debug(f"Could not get URL after stabilization: {e}")

        # Try multiple times to check authentication status
        max_retries = 3
        logger.debug(f"Starting authentication verification with {max_retries} retries")

        for attempt in range(max_retries):
            attempt_start_time = time.time()
            logger.debug(
                f"=== Authentication check attempt {attempt + 1}/{max_retries} ==="
            )

            try:
                current_url = driver.current_url
                logger.debug(f"Current URL: {current_url}")

                # Log page title for additional context
                try:
                    page_title = driver.title
                    logger.debug(f"Page title: {page_title}")
                except Exception as e:
                    logger.debug(f"Could not get page title: {e}")

                # Check if we're on login page (authentication failed)
                if "login" in current_url or "uas/login" in current_url:
                    logger.warning(
                        f"Cookie authentication failed - redirected to login page: {current_url}"
                    )
                    logger.debug("Checking page source for login indicators...")
                    try:
                        page_source_snippet = driver.page_source[:500]
                        logger.debug(f"Page source snippet: {page_source_snippet}")
                    except Exception as e:
                        logger.debug(f"Could not get page source: {e}")
                    return False

                # Check if we're on authenticated pages (authentication succeeded)
                elif (
                    "feed" in current_url
                    or "mynetwork" in current_url
                    or "linkedin.com/in/" in current_url
                    or "/feed/" in current_url
                ):
                    attempt_duration = time.time() - attempt_start_time
                    total_duration = time.time() - start_time
                    logger.info(
                        f"Cookie authentication successful! (attempt {attempt + 1}, {attempt_duration:.2f}s, total {total_duration:.2f}s)"
                    )
                    logger.debug(f"Successfully authenticated to: {current_url}")
                    return True

                # If we're on an unexpected page, wait a bit more and retry
                else:
                    logger.debug(
                        f"Unexpected page during authentication check: {current_url}"
                    )

                    # Log more details about the unexpected page
                    try:
                        page_source_snippet = driver.page_source[:1000]
                        logger.debug(
                            f"Unexpected page source snippet: {page_source_snippet}"
                        )
                    except Exception as e:
                        logger.debug(
                            f"Could not get page source for unexpected page: {e}"
                        )

                    if attempt < max_retries - 1:
                        logger.info(
                            f"Waiting for page to stabilize (attempt {attempt + 1}/{max_retries})..."
                        )
                        time.sleep(2)
                        continue
                    else:
                        logger.debug(
                            "Final attempt - using WebDriverWait for definitive check..."
                        )
                        # Try to wait for any LinkedIn authenticated page elements
                        try:
                            wait_start_time = time.time()
                            WebDriverWait(driver, 10).until(
                                lambda d: any(
                                    indicator in d.current_url
                                    for indicator in [
                                        "feed",
                                        "mynetwork",
                                        "linkedin.com/in/",
                                        "/feed/",
                                    ]
                                )
                                or "login" in d.current_url
                            )
                            wait_duration = time.time() - wait_start_time
                            logger.debug(
                                f"WebDriverWait completed in {wait_duration:.2f} seconds"
                            )

                            final_url = driver.current_url
                            logger.debug(f"Final URL after WebDriverWait: {final_url}")

                            if "login" in final_url or "uas/login" in final_url:
                                logger.warning(
                                    f"Cookie authentication failed - final check shows login page: {final_url}"
                                )
                                return False
                            else:
                                total_duration = time.time() - start_time
                                logger.info(
                                    f"Cookie authentication successful - final check passed! (total {total_duration:.2f}s)"
                                )
                                logger.debug(f"Final successful URL: {final_url}")
                                return True
                        except TimeoutException:
                            wait_duration = time.time() - wait_start_time
                            logger.warning(
                                f"Cookie authentication failed - WebDriverWait timed out after {wait_duration:.2f}s"
                            )
                            logger.debug(
                                "Could not verify successful login within timeout period"
                            )

                            # Log final state for debugging
                            try:
                                timeout_url = driver.current_url
                                timeout_title = driver.title
                                logger.debug(
                                    f"Final state - URL: {timeout_url}, Title: {timeout_title}"
                                )
                            except Exception as e:
                                logger.debug(f"Could not get final state info: {e}")

                            return False

            except Exception as e:
                attempt_duration = time.time() - attempt_start_time
                logger.debug(
                    f"Error during authentication check attempt {attempt + 1} after {attempt_duration:.2f}s: {e}"
                )
                logger.debug(f"Exception type: {type(e).__name__}")

                if attempt < max_retries - 1:
                    logger.debug(
                        f"Retrying after error (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(2)
                    continue
                else:
                    logger.error(f"Final attempt failed with error: {e}")
                    raise e

        # If we exit the retry loop without returning, authentication failed
        total_duration = time.time() - start_time

        # If we had a login exception but got here, it means the authentication checks failed
        # but we should provide more context about the original exception
        if login_exception:
            logger.warning(
                f"Cookie authentication failed - exhausted all retry attempts after {total_duration:.2f}s"
            )
            logger.warning(f"Original actions.login() exception was: {login_exception}")
            logger.info(
                "Despite the browser appearing to log in successfully, authentication verification failed"
            )
        else:
            logger.warning(
                f"Cookie authentication failed - exhausted all retry attempts after {total_duration:.2f}s"
            )
        return False

    except TimeoutException as e:
        total_duration = time.time() - start_time
        logger.warning(
            f"Cookie authentication failed due to timeout after {total_duration:.2f}s: {e}"
        )
        return False
    except Exception as e:
        total_duration = time.time() - start_time
        logger.warning(f"Cookie authentication failed after {total_duration:.2f}s: {e}")
        logger.debug(f"Exception type: {type(e).__name__}")
        return False
    finally:
        # Restore normal timeout
        driver.set_page_load_timeout(60)
        total_duration = time.time() - start_time
        logger.debug(f"login_with_cookie() completed in {total_duration:.2f} seconds")


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
