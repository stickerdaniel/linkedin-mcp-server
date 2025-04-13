# src/linkedin_mcp_server/drivers/chrome.py
"""
Chrome driver management for LinkedIn scraping.

This module handles the creation and management of Chrome WebDriver instances.
"""
from typing import Dict, Optional, List
import os
from pathlib import Path
import sys
import inquirer
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import WebDriverException

from src.linkedin_mcp_server.credentials import setup_credentials

# Global driver storage to reuse sessions
active_drivers: Dict[str, webdriver.Chrome] = {}


def get_chromedriver_path() -> Optional[str]:
    """
    Get the ChromeDriver path from environment variable or default locations.
    
    Returns:
        Optional[str]: Path to the ChromeDriver executable if found, None otherwise
    """
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


def get_or_create_driver(headless: bool = True) -> webdriver.Chrome:
    """
    Get existing driver or create a new one.
    
    Args:
        headless (bool): Whether to run Chrome in headless mode
        
    Returns:
        webdriver.Chrome: Chrome WebDriver instance
        
    Raises:
        WebDriverException: If the driver cannot be created
    """
    session_id = "default"  # We use a single session for simplicity

    if session_id in active_drivers:
        return active_drivers[session_id]

    # Set up Chrome options
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")

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
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            driver = webdriver.Chrome(options=chrome_options)

        # Add a page load timeout for safety
        driver.set_page_load_timeout(60)

        active_drivers[session_id] = driver
        return driver
    except Exception as e:
        print(f"Error creating web driver: {e}")
        raise


def initialize_driver() -> None:
    """
    Initialize the driver and log in at server start.
    
    This function is called at server startup to set up the Chrome driver
    and log in to LinkedIn.
    """
    credentials = setup_credentials()

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

    try:
        driver = get_or_create_driver(headless=True)

        # status on driver
        if driver is None:
            print("❌ Failed to create or retrieve the web driver.")
            return
        print("✅ Web driver initialized successfully")

        # Import here to avoid circular imports
        from linkedin_scraper import actions

        # Login to LinkedIn
        try:
            # login start
            print("🔑 Logging in to LinkedIn...")
            actions.login(driver, credentials["email"], credentials["password"])
            print("✅ Successfully logged in to LinkedIn")
        except Exception as e:
            print(f"❌ Failed to login: {str(e)}")
            # confirm in mobile app might be required
            print(
                "⚠️ You might need to confirm the login in your LinkedIn mobile app. "
                "Please try again and confirm the login. You can also try to run this script "
                "with headless mode disabled for easier debugging."
            )

            questions = [
                inquirer.Confirm(
                    "retry",
                    message="Would you like to try with different credentials?",
                    default=True,
                ),
            ]
            answers = inquirer.prompt(questions)

            if answers["retry"]:
                # Remove old credentials and try again
                credentials_file = Path.home() / ".linkedin_mcp_credentials.json"
                if credentials_file.exists():
                    os.remove(credentials_file)
                # Start over with new credentials
                return initialize_driver()
            else:
                print("Please check your credentials and try again.")
                sys.exit(1)

    except WebDriverException as e:
        print(f"❌ Failed to initialize web driver: {str(e)}")

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
                return initialize_driver()
            else:
                print(f"⚠️ Warning: The specified path does not exist: {path}")
                return initialize_driver()

        elif answers["chromedriver_action"] == "help":
            print("\n📋 ChromeDriver Installation Guide:")
            print(
                "1. Find your Chrome version: Chrome menu > Help > About Google Chrome"
            )
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
                return initialize_driver()

        print("❌ ChromeDriver is required for this application to work properly.")
        sys.exit(1)