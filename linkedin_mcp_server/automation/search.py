"""
LinkedIn search automation for people and companies.

Provides automation classes for searching and extracting search results
from LinkedIn's people and company search pages.
"""

import logging
import urllib.parse
from typing import Any

from .base import BaseAutomation
from .selectors import CommonSelectors, SearchSelectors

logger = logging.getLogger(__name__)


class SearchResult:
    """Represents a single search result."""

    def __init__(
        self,
        name: str,
        url: str,
        headline: str | None = None,
        location: str | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self.name = name
        self.url = url
        self.headline = headline
        self.location = location
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "url": self.url,
            "headline": self.headline,
            "location": self.location,
            **self.extra,
        }


class PeopleSearchAutomation(BaseAutomation):
    """
    Automation for LinkedIn people search.

    Searches for people using various filters and extracts profile information
    from search results.
    """

    BASE_URL = "https://www.linkedin.com/search/results/people/"

    async def execute(
        self,
        keywords: str | None = None,
        title: str | None = None,
        company: str | None = None,
        location: str | None = None,
        industry: str | None = None,
        limit: int = 25,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Execute people search.

        Args:
            keywords: General search keywords
            title: Job title filter
            company: Company name filter
            location: Location filter
            industry: Industry filter
            limit: Maximum results to return

        Returns:
            Dictionary with search results and metadata
        """
        # Build search URL with parameters
        url = self._build_search_url(
            keywords=keywords,
            title=title,
            company=company,
            location=location,
            industry=industry,
        )

        logger.info(f"Searching people: {url}")
        await self.navigate(url, wait_for="load")

        # Check for rate limiting
        if await self._check_rate_limit():
            return {
                "error": "rate_limited",
                "message": "LinkedIn rate limit detected. Please try again later.",
                "results": [],
                "count": 0,
            }

        # Extract results
        results = await self._extract_results(limit)

        return {
            "query": {
                "keywords": keywords,
                "title": title,
                "company": company,
                "location": location,
                "industry": industry,
            },
            "results": [r.to_dict() for r in results],
            "count": len(results),
        }

    def _build_search_url(
        self,
        keywords: str | None = None,
        title: str | None = None,
        company: str | None = None,
        location: str | None = None,
        industry: str | None = None,
    ) -> str:
        """Build the search URL with filters."""
        params = {}

        if keywords:
            params["keywords"] = keywords
        if title:
            params["titleFreeText"] = title
        if company:
            params["company"] = company
        if location:
            params["geoUrn"] = location  # Would need geo URN lookup
        if industry:
            params["industry"] = industry

        query_string = urllib.parse.urlencode(params)
        return f"{self.BASE_URL}?{query_string}" if params else self.BASE_URL

    async def _check_rate_limit(self) -> bool:
        """Check if we've hit a rate limit page."""
        return await self.exists(CommonSelectors.RATE_LIMIT_PAGE, timeout=2000)

    async def _extract_results(self, limit: int) -> list[SearchResult]:
        """Extract search results from the current page."""
        results: list[SearchResult] = []
        page = await self.get_page()

        # Wait for page to fully render - LinkedIn uses dynamic loading
        await self.random_delay(2.0, 3.0)

        # LinkedIn has moved to obfuscated CSS class names, so we use profile links
        # as the anchor point and navigate to parent containers
        profile_links = page.locator("a[href*='/in/']")
        link_count = await profile_links.count()
        logger.info(f"Found {link_count} profile links on page")

        if link_count == 0:
            logger.warning("No profile links found")
            try:
                await self.take_screenshot("debug_search_page.png")
                logger.info("Screenshot saved to debug_search_page.png")
            except Exception:
                pass
            return results

        # Track seen URLs to avoid duplicates
        seen_urls = set()

        # Process each profile link
        for i in range(link_count):
            if len(results) >= limit:
                break

            try:
                link = profile_links.nth(i)
                href = await link.get_attribute("href")

                if not href or "/in/" not in href:
                    continue

                # Clean and check URL
                url = href.split("?")[0]
                if not url.startswith("https://"):
                    url = f"https://www.linkedin.com{url}"

                if url in seen_urls:
                    continue

                # Get name from the link
                name = None
                try:
                    # First try span with aria-hidden (common pattern)
                    name_span = link.locator("span[aria-hidden='true']").first
                    if await name_span.count() > 0:
                        name = await name_span.text_content()
                except Exception:
                    pass

                if not name:
                    try:
                        name = await link.text_content()
                    except Exception:
                        pass

                if name:
                    name = name.strip().replace("\n", " ").strip()
                    # Clean up bullet points and extra text
                    if "\u2022" in name:
                        name = name.split("\u2022")[0].strip()
                    # Take first part before connection info
                    if "?" in name:
                        name = name.split("?")[0].strip()
                    name = name.strip()[:50]

                # Skip non-name text
                if not name or len(name) < 2:
                    continue
                if name.lower().startswith(("view", "connect", "follow", "message")):
                    continue
                # Skip if looks like navigation text
                if name.lower() in (
                    "home",
                    "my network",
                    "jobs",
                    "messaging",
                    "notifications",
                ):
                    continue

                seen_urls.add(url)

                # Try to get headline/location from parent container
                # Navigate up to find the card container (LI element)
                headline = None
                location = None

                try:
                    # Get the parent LI element (the card)
                    card = link.locator("xpath=ancestor::li[1]")
                    if await card.count() > 0:
                        # Get all text from the card and try to extract headline
                        card_text = await card.text_content()
                        if card_text:
                            lines = [
                                line.strip()
                                for line in card_text.split("\n")
                                if line.strip()
                            ]
                            # Headline is usually after the name
                            for j, line in enumerate(lines):
                                if name in line and j + 1 < len(lines):
                                    potential_headline = lines[j + 1]
                                    # Skip if it's a button text
                                    if potential_headline.lower() not in (
                                        "connect",
                                        "follow",
                                        "message",
                                        "pending",
                                    ):
                                        headline = potential_headline[:100]
                                        break
                except Exception:
                    pass

                result = SearchResult(
                    name=name,
                    url=url,
                    headline=headline,
                    location=location,
                )
                results.append(result)
                logger.debug(f"Extracted: {name} - {url}")

            except Exception as e:
                logger.debug(f"Failed to extract link {i}: {e}")
                continue

        logger.info(f"Extracted {len(results)} unique results")
        return results[:limit]

    async def _extract_person_card(self, card: Any) -> SearchResult | None:
        """Extract data from a single person result card."""
        try:
            # Try multiple selectors for profile link
            link_selectors = [
                SearchSelectors.PERSON_PROFILE_LINK,
                "a[href*='/in/']",
                "a.app-aware-link",
                "a.entity-result__title-link",
            ]

            url = None
            for selector in link_selectors:
                try:
                    link = card.locator(selector).first
                    url = await link.get_attribute("href")
                    if url and "/in/" in url:
                        break
                except Exception:
                    continue

            if not url:
                return None

            # Clean URL
            url = url.split("?")[0]
            if not url.startswith("https://"):
                url = f"https://www.linkedin.com{url}"

            # Try multiple selectors for name
            name_selectors = [
                SearchSelectors.PERSON_NAME,
                "span[aria-hidden='true']",
                "span.entity-result__title-text",
                "a[href*='/in/'] span",
            ]

            name = None
            for selector in name_selectors:
                try:
                    name_elem = card.locator(selector).first
                    name = await name_elem.text_content()
                    if name and name.strip():
                        name = name.strip()
                        # Skip if it looks like "View profile" or similar
                        if len(name) > 2 and not name.startswith("View"):
                            break
                        name = None
                except Exception:
                    continue

            if not name:
                return None

            # Get headline (optional)
            headline = None
            headline_selectors = [
                SearchSelectors.PERSON_HEADLINE,
                "div.entity-result__primary-subtitle",
                "p.entity-result__summary",
            ]
            for selector in headline_selectors:
                try:
                    headline_elem = card.locator(selector).first
                    headline = await headline_elem.text_content()
                    if headline:
                        headline = headline.strip()
                        break
                except Exception:
                    continue

            # Get location (optional)
            location = None
            location_selectors = [
                SearchSelectors.PERSON_LOCATION,
                "div.entity-result__secondary-subtitle",
            ]
            for selector in location_selectors:
                try:
                    location_elem = card.locator(selector).first
                    location = await location_elem.text_content()
                    if location:
                        location = location.strip()
                        break
                except Exception:
                    continue

            return SearchResult(
                name=name,
                url=url,
                headline=headline,
                location=location,
            )

        except Exception as e:
            logger.debug(f"Error extracting person card: {e}")
            return None

    async def _go_to_next_page(self) -> bool:
        """Navigate to the next page of results."""
        try:
            next_button = await self.wait_for_selector(
                SearchSelectors.NEXT_PAGE, timeout=3000
            )
            is_disabled = await next_button.is_disabled()
            if is_disabled:
                return False

            await next_button.click()
            await self.random_delay(1.0, 2.0)
            return True
        except Exception:
            return False


class CompanySearchAutomation(BaseAutomation):
    """
    Automation for LinkedIn company search.

    Searches for companies using various filters and extracts company information
    from search results.
    """

    BASE_URL = "https://www.linkedin.com/search/results/companies/"

    async def execute(
        self,
        keywords: str | None = None,
        industry: str | None = None,
        company_size: str | None = None,
        location: str | None = None,
        limit: int = 25,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Execute company search.

        Args:
            keywords: Search keywords
            industry: Industry filter
            company_size: Company size filter (e.g., "1-10", "51-200")
            location: Location filter
            limit: Maximum results to return

        Returns:
            Dictionary with search results and metadata
        """
        # Build search URL
        url = self._build_search_url(
            keywords=keywords,
            industry=industry,
            company_size=company_size,
            location=location,
        )

        logger.info(f"Searching companies: {url}")
        await self.navigate(url, wait_for="load")

        # Check for rate limiting
        if await self._check_rate_limit():
            return {
                "error": "rate_limited",
                "message": "LinkedIn rate limit detected. Please try again later.",
                "results": [],
                "count": 0,
            }

        # Extract results
        results = await self._extract_results(limit)

        return {
            "query": {
                "keywords": keywords,
                "industry": industry,
                "company_size": company_size,
                "location": location,
            },
            "results": [r.to_dict() for r in results],
            "count": len(results),
        }

    def _build_search_url(
        self,
        keywords: str | None = None,
        industry: str | None = None,
        company_size: str | None = None,
        location: str | None = None,
    ) -> str:
        """Build the search URL with filters."""
        params = {}

        if keywords:
            params["keywords"] = keywords
        if industry:
            params["industry"] = industry
        if company_size:
            params["companySize"] = company_size
        if location:
            params["geoUrn"] = location

        query_string = urllib.parse.urlencode(params)
        return f"{self.BASE_URL}?{query_string}" if params else self.BASE_URL

    async def _check_rate_limit(self) -> bool:
        """Check if we've hit a rate limit page."""
        return await self.exists(CommonSelectors.RATE_LIMIT_PAGE, timeout=2000)

    async def _extract_results(self, limit: int) -> list[SearchResult]:
        """Extract search results from the current page."""
        results: list[SearchResult] = []
        page = await self.get_page()

        # Wait for results to load
        try:
            await self.wait_for_selector(SearchSelectors.COMPANY_RESULTS, timeout=10000)
        except Exception:
            logger.warning("No company search results found")
            return results

        # Find all company cards
        cards = page.locator(SearchSelectors.COMPANY_CARD)
        count = await cards.count()

        for i in range(min(count, limit)):
            try:
                card = cards.nth(i)
                result = await self._extract_company_card(card)
                if result:
                    results.append(result)
                    if len(results) >= limit:
                        break
            except Exception as e:
                logger.debug(f"Failed to extract company card {i}: {e}")
                continue

        # Handle pagination if needed
        while len(results) < limit:
            if not await self._go_to_next_page():
                break

            cards = page.locator(SearchSelectors.COMPANY_CARD)
            count = await cards.count()

            for i in range(count):
                try:
                    card = cards.nth(i)
                    result = await self._extract_company_card(card)
                    if result:
                        results.append(result)
                        if len(results) >= limit:
                            break
                except Exception as e:
                    logger.debug(f"Failed to extract company card {i}: {e}")
                    continue

        return results[:limit]

    async def _extract_company_card(self, card: Any) -> SearchResult | None:
        """Extract data from a single company result card."""
        try:
            # Get company link
            link = card.locator(SearchSelectors.COMPANY_PROFILE_LINK).first
            url = await link.get_attribute("href")
            if not url:
                return None

            # Clean URL
            url = url.split("?")[0]
            if not url.startswith("https://"):
                url = f"https://www.linkedin.com{url}"

            # Get name
            name_elem = card.locator(SearchSelectors.COMPANY_NAME).first
            name = await name_elem.text_content()
            if not name:
                return None
            name = name.strip()

            # Get industry (optional)
            industry = None
            try:
                industry_elem = card.locator(SearchSelectors.COMPANY_INDUSTRY).first
                industry = await industry_elem.text_content()
                if industry:
                    industry = industry.strip()
            except Exception:
                pass

            # Get location (optional)
            location = None
            try:
                location_elem = card.locator(SearchSelectors.COMPANY_LOCATION).first
                location = await location_elem.text_content()
                if location:
                    location = location.strip()
            except Exception:
                pass

            return SearchResult(
                name=name,
                url=url,
                headline=industry,  # Use industry as headline
                location=location,
            )

        except Exception as e:
            logger.debug(f"Error extracting company card: {e}")
            return None

    async def _go_to_next_page(self) -> bool:
        """Navigate to the next page of results."""
        try:
            next_button = await self.wait_for_selector(
                SearchSelectors.NEXT_PAGE, timeout=3000
            )
            is_disabled = await next_button.is_disabled()
            if is_disabled:
                return False

            await next_button.click()
            await self.random_delay(1.0, 2.0)
            return True
        except Exception:
            return False
