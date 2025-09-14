# linkedin_mcp_server/scrapers/person_extended.py
"""
Extended Person scraper with contact information extraction.

Extends the linkedin_scraper Person class to extract detailed contact information
from the Contact Info modal including email, phone, websites, birthday, etc.
"""

import logging
import time
from typing import Dict, List, Optional, Any

from linkedin_scraper import Person
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)


class PersonExtended(Person):
    """Extended Person class that also extracts contact information."""

    def __init__(self, *args, **kwargs):
        """Initialize with additional contact info fields."""
        # Add new fields for contact information
        self.contact_info = {
            "email": None,
            "phone": None,
            "birthday": None,
            "connected_date": None,
            "websites": [],
            "linkedin_url": None,
        }
        super().__init__(*args, **kwargs)

    def scrape_logged_in(self, close_on_complete=True):
        """Override to include contact info extraction."""
        # Call parent method first
        super().scrape_logged_in(close_on_complete=False)

        # Then extract contact info
        try:
            self.extract_contact_info()
        except Exception as e:
            logger.warning(f"Failed to extract contact info: {e}")

        if close_on_complete:
            self.driver.quit()

    def extract_contact_info(self):
        """Click Contact Info button and extract contact details from modal."""
        driver = self.driver

        # Navigate back to the profile page if needed
        if not self.linkedin_url in driver.current_url:
            driver.get(self.linkedin_url)
            time.sleep(2)

        try:
            # Wait for the main profile section to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "main"))
            )

            # Log current page for debugging
            logger.info(f"Current URL: {driver.current_url}")

            # Find and click the Contact Info link
            # The link is usually next to the location, with text "Contact info"
            contact_info_link = None

            # Try multiple selectors to find the Contact Info link
            # Updated selectors based on current LinkedIn UI
            selectors = [
                "//a[contains(@href, 'overlay/contact-info')]",
                "//button[contains(@aria-label, 'Contact info')]",
                "//a[contains(text(), 'Contact info')]",
                "//span[contains(text(), 'Contact info')]/parent::a",
                "//span[contains(text(), 'Contact info')]/parent::button",
                # New selectors for current LinkedIn UI
                "//a[@id='top-card-text-details-contact-info']",
                "//span[@aria-label='View contact info']/parent::a",
                "//div[contains(@class, 'pv-text-details__left-panel')]//a[contains(@href, 'contact-info')]",
                # Try finding by partial href
                "//a[contains(@href, 'contact-info')]"
            ]

            for selector in selectors:
                try:
                    contact_info_link = driver.find_element(By.XPATH, selector)
                    logger.info(f"Found Contact Info link with selector: {selector}")
                    break
                except NoSuchElementException:
                    continue

            if not contact_info_link:
                logger.warning("Could not find Contact Info link")
                return

            # Click the Contact Info link
            driver.execute_script("arguments[0].scrollIntoView(true);", contact_info_link)
            time.sleep(1)

            # Try regular click first, then JavaScript click if that fails
            try:
                contact_info_link.click()
                logger.info("Clicked Contact Info link (regular click)")
            except Exception as e:
                logger.info(f"Regular click failed ({e}), trying JavaScript click...")
                driver.execute_script("arguments[0].click();", contact_info_link)
                logger.info("Clicked Contact Info link (JavaScript click)")

            # Wait for modal to appear
            time.sleep(2)

            # Extract data from the modal
            self._extract_modal_data(driver)

            # Close the modal
            self._close_modal(driver)

        except TimeoutException:
            logger.warning("Timeout waiting for Contact Info elements")
        except Exception as e:
            logger.error(f"Error extracting contact info: {e}")

    def _extract_modal_data(self, driver):
        """Extract data from the Contact Info modal."""
        try:
            # Wait for modal content to load
            modal = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[role='dialog'], .artdeco-modal"))
            )
            logger.info("Modal found, extracting data...")

            # Try to get all text from modal for debugging
            modal_text = modal.text
            logger.debug(f"Modal text preview: {modal_text[:200] if modal_text else 'No text found'}...")

            # Extract LinkedIn Profile URL
            try:
                # Try multiple approaches
                profile_links = modal.find_elements(By.XPATH, ".//a[contains(@href, 'linkedin.com/in/')]")
                if profile_links:
                    self.contact_info["linkedin_url"] = profile_links[0].get_attribute("href")
                    logger.info(f"Found LinkedIn URL: {self.contact_info['linkedin_url']}")
            except Exception as e:
                logger.debug(f"LinkedIn URL extraction failed: {e}")

            # Extract Websites
            try:
                websites_section = modal.find_element(By.XPATH, ".//section[contains(., 'Websites')]")
                website_links = websites_section.find_elements(By.TAG_NAME, "a")
                self.contact_info["websites"] = [
                    {
                        "url": link.get_attribute("href"),
                        "type": link.text.strip() if link.text else "Website"
                    }
                    for link in website_links
                ]
                logger.info(f"Found {len(self.contact_info['websites'])} websites")
            except NoSuchElementException:
                pass

            # Extract Phone
            try:
                # Try different approaches for phone
                phone_selectors = [
                    ".//section[contains(., 'Phone')]//span[not(contains(text(), 'Phone'))]",
                    ".//div[contains(text(), 'Phone')]/following-sibling::*",
                    ".//span[contains(@class, 'contact-info__phone')]",
                    ".//a[contains(@href, 'tel:')]"
                ]
                for selector in phone_selectors:
                    try:
                        phone_elem = modal.find_element(By.XPATH, selector)
                        text = phone_elem.text.strip() if selector != ".//a[contains(@href, 'tel:')]" else phone_elem.get_attribute("href").replace("tel:", "")
                        if text and text != "Phone":
                            self.contact_info["phone"] = text
                            logger.info(f"Found phone: {self.contact_info['phone']}")
                            break
                    except NoSuchElementException:
                        continue
            except Exception as e:
                logger.debug(f"Phone extraction failed: {e}")

            # Extract Email
            try:
                # Try different approaches for email
                email_selectors = [
                    ".//a[contains(@href, 'mailto:')]",
                    ".//section[contains(., 'Email')]//a",
                    ".//div[contains(text(), 'Email')]/following-sibling::*//a"
                ]
                for selector in email_selectors:
                    try:
                        email_elem = modal.find_element(By.XPATH, selector)
                        if email_elem.get_attribute("href") and "mailto:" in email_elem.get_attribute("href"):
                            email = email_elem.get_attribute("href").replace("mailto:", "")
                        else:
                            email = email_elem.text.strip()
                        if email and "@" in email:
                            self.contact_info["email"] = email
                            logger.info(f"Found email: {self.contact_info['email']}")
                            break
                    except NoSuchElementException:
                        continue
            except Exception as e:
                logger.debug(f"Email extraction failed: {e}")

            # Extract Birthday
            try:
                birthday_section = modal.find_element(By.XPATH, ".//section[contains(., 'Birthday')]")
                # Birthday is usually in a time element or the text after Birthday label
                birthday_elements = birthday_section.find_elements(By.XPATH, ".//time | .//span[not(contains(text(), 'Birthday'))]")
                for elem in birthday_elements:
                    text = elem.text.strip()
                    if text and not text == "Birthday":
                        self.contact_info["birthday"] = text
                        logger.info(f"Found birthday: {self.contact_info['birthday']}")
                        break
            except NoSuchElementException:
                pass

            # Extract Connected Date
            try:
                connected_section = modal.find_element(By.XPATH, ".//section[contains(., 'Connected')]")
                # Look for date after "Connected" text
                time_element = connected_section.find_element(By.TAG_NAME, "time")
                self.contact_info["connected_date"] = time_element.text.strip()
                logger.info(f"Found connected date: {self.contact_info['connected_date']}")
            except NoSuchElementException:
                pass

        except TimeoutException:
            logger.warning("Modal did not load in time")
        except Exception as e:
            logger.error(f"Error extracting modal data: {e}")

    def _close_modal(self, driver):
        """Close the Contact Info modal."""
        try:
            # Try to find and click the close button (X)
            close_buttons = [
                "//button[@aria-label='Dismiss']",
                "//button[contains(@class, 'artdeco-modal__dismiss')]",
                "//button[contains(@data-test-modal-close-btn, '')]"
            ]

            for selector in close_buttons:
                try:
                    close_btn = driver.find_element(By.XPATH, selector)
                    close_btn.click()
                    logger.info("Closed Contact Info modal")
                    time.sleep(1)
                    return
                except NoSuchElementException:
                    continue

            # If close button not found, try pressing ESC
            from selenium.webdriver.common.keys import Keys
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            logger.info("Closed modal with ESC key")

        except Exception as e:
            logger.warning(f"Could not close modal: {e}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert person data to dictionary including contact info."""
        # Get basic person data
        data = {
            "name": self.name,
            "about": self.about,
            "company": self.company,
            "job_title": self.job_title,
            "location": getattr(self, "location", None),
            "open_to_work": getattr(self, "open_to_work", False),
            "contact_info": self.contact_info,
            "experiences": [
                {
                    "position_title": exp.position_title,
                    "company": exp.institution_name,
                    "from_date": exp.from_date,
                    "to_date": exp.to_date,
                    "duration": exp.duration,
                    "location": exp.location,
                    "description": exp.description,
                }
                for exp in self.experiences
            ],
            "educations": [
                {
                    "institution": edu.institution_name,
                    "degree": edu.degree,
                    "from_date": edu.from_date,
                    "to_date": edu.to_date,
                    "description": edu.description,
                }
                for edu in self.educations
            ],
            "interests": [interest.title for interest in self.interests],
            "accomplishments": [
                {"category": acc.category, "title": acc.title}
                for acc in self.accomplishments
            ],
            "contacts": [
                {
                    "name": contact.name,
                    "occupation": contact.occupation,
                    "url": contact.url,
                }
                for contact in self.contacts
            ],
        }

        return data