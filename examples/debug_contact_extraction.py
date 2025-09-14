#!/usr/bin/env python
"""
Debug script for contact info extraction.

This script runs the contact extraction with visible browser and detailed logging
to see exactly what's happening when trying to extract contact information.
"""

import os
import logging
import time

# Set up detailed logging
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


def debug_contact_extraction():
    """Debug the contact info extraction with visible browser."""

    # Get LinkedIn cookie from environment variable
    cookie = os.getenv("LINKEDIN_COOKIE")

    if not cookie:
        print("❌ LINKEDIN_COOKIE environment variable not set")
        print("Please set your LinkedIn cookie with:")
        print("export LINKEDIN_COOKIE='your_cookie_here'")
        return

    print("🔧 Debug: Contact Info Extraction")
    print("=" * 50)

    from linkedin_mcp_server.drivers.chrome import (
        create_chrome_options,
        login_with_cookie,
    )
    from linkedin_mcp_server.config import get_config
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    # Create config and force non-headless mode for debugging
    config = get_config()

    # Create Chrome options (non-headless)
    chrome_options = create_chrome_options(config)
    # Remove headless mode for debugging
    # Remove headless arguments for visible debugging
    filtered_args = [arg for arg in chrome_options.arguments if "--headless" not in arg]
    chrome_options._arguments = filtered_args  # Direct access to private attribute

    print("✅ Creating visible Chrome browser for debugging...")

    from linkedin_mcp_server.drivers.chrome import create_chrome_service

    service = create_chrome_service(config)

    from selenium import webdriver

    driver = webdriver.Chrome(service=service, options=chrome_options)

    try:
        # Test cookie login
        print("🔧 Testing cookie authentication...")
        success = login_with_cookie(driver, cookie)

        if not success:
            print("❌ Cookie authentication failed")
            return

        print("✅ Cookie authentication successful")

        # Navigate to Nick Hargreaves profile
        test_url = "https://www.linkedin.com/in/nick-hargreaves/"
        print(f"🔧 Navigating to: {test_url}")
        driver.get(test_url)

        # Wait for page to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "main"))
        )

        print("✅ Profile page loaded")
        print("🔍 Looking for Contact Info link...")

        # Try to find Contact Info link
        selectors = [
            "//a[contains(@href, 'overlay/contact-info')]",
            "//a[contains(@href, 'contact-info')]",
            "//a[contains(text(), 'Contact info')]",
            "//span[contains(text(), 'Contact info')]/parent::a",
            "//button[contains(@aria-label, 'Contact info')]",
        ]

        contact_link = None
        for i, selector in enumerate(selectors):
            try:
                elements = driver.find_elements(By.XPATH, selector)
                if elements:
                    contact_link = elements[0]
                    print(
                        f"✅ Found Contact Info link with selector {i + 1}: {selector}"
                    )
                    print(f"   Element text: '{contact_link.text.strip()}'")
                    print(
                        f"   Element href: '{contact_link.get_attribute('href') or 'N/A'}'"
                    )
                    break
            except Exception as e:
                print(f"   Selector {i + 1} failed: {e}")

        if not contact_link:
            print("❌ No Contact Info link found")
            print("🔍 Let's see what links are available on the page...")

            # Show all links on the page for debugging
            all_links = driver.find_elements(By.TAG_NAME, "a")[:20]  # First 20 links
            for i, link in enumerate(all_links):
                text = link.text.strip()[:50]
                href = link.get_attribute("href") or "No href"
                if text or "contact" in href.lower():
                    print(f"   Link {i}: '{text}' -> {href[:100]}")

            # Also check for any element containing "contact"
            contact_elements = driver.find_elements(
                By.XPATH,
                "//*[contains(translate(text(), 'CONTACT', 'contact'), 'contact')]",
            )[:10]
            if contact_elements:
                print("\\n🔍 Found elements containing 'contact':")
                for elem in contact_elements:
                    print(f"   - {elem.tag_name}: '{elem.text.strip()[:100]}'")

            input("\\nPress Enter to continue and close browser...")
            return

        print("🔧 Clicking Contact Info link...")

        # Scroll to element and click
        driver.execute_script("arguments[0].scrollIntoView(true);", contact_link)
        time.sleep(1)

        try:
            contact_link.click()
            print("✅ Clicked successfully (regular click)")
        except Exception as e:
            print(f"Regular click failed: {e}")
            print("🔧 Trying JavaScript click...")
            driver.execute_script("arguments[0].click();", contact_link)
            print("✅ JavaScript click executed")

        print("⏳ Waiting for modal to appear...")
        time.sleep(3)

        # Look for modal
        modal_selectors = [
            "[role='dialog']",
            ".artdeco-modal",
            "[data-test-modal]",
            ".contact-info",
        ]

        modal = None
        for selector in modal_selectors:
            try:
                modal = driver.find_element(By.CSS_SELECTOR, selector)
                print(f"✅ Found modal with selector: {selector}")
                break
            except Exception:
                continue

        if not modal:
            print("❌ No modal found")
            print("🔍 Current page title:", driver.title)
            print("🔍 Current URL:", driver.current_url)
        else:
            print("✅ Modal found!")
            modal_text = modal.text[:500]
            print(f"📄 Modal content preview: {modal_text}")

            # Try to find email
            email_elements = modal.find_elements(
                By.XPATH, ".//a[contains(@href, 'mailto:')]"
            )
            if email_elements:
                email = email_elements[0].get_attribute("href").replace("mailto:", "")
                print(f"📧 Found email: {email}")
            else:
                print("❌ No email found in modal")

            # Try to find phone
            phone_elements = modal.find_elements(
                By.XPATH, ".//a[contains(@href, 'tel:')]"
            )
            if phone_elements:
                phone = phone_elements[0].get_attribute("href").replace("tel:", "")
                print(f"📱 Found phone: {phone}")
            else:
                print("❌ No phone found in modal")

        input("\\nPress Enter to close browser...")

    finally:
        driver.quit()


if __name__ == "__main__":
    debug_contact_extraction()
