# linkedin_mcp_server/scraper_adapter.py
"""
Adapter to handle both linkedin-scraper and fast-linkedin-scraper libraries.

Provides a unified interface for LinkedIn scraping that can switch between
different scraper implementations based on configuration.
"""

import logging
from typing import Any, Dict, List, Protocol, runtime_checkable

from linkedin_mcp_server.authentication import ensure_authentication
from linkedin_mcp_server.config import get_config

logger = logging.getLogger(__name__)


@runtime_checkable
class ScraperAdapter(Protocol):
    """Protocol for LinkedIn scraper adapters."""

    def get_person_profile(self, linkedin_username: str) -> Dict[str, Any]:
        """Get a person's LinkedIn profile."""
        ...

    def get_company_profile(
        self, company_name: str, get_employees: bool = False
    ) -> Dict[str, Any]:
        """Get a company's LinkedIn profile."""
        ...

    def get_job_details(self, job_id: str) -> Dict[str, Any]:
        """Get job details for a specific job posting."""
        ...

    def search_jobs(self, search_term: str) -> List[Dict[str, Any]]:
        """Search for jobs on LinkedIn."""
        ...

    def get_recommended_jobs(self) -> List[Dict[str, Any]]:
        """Get personalized job recommendations."""
        ...


class FastLinkedInScraperAdapter:
    """Adapter for fast-linkedin-scraper library."""

    session: Any

    def _execute_with_session(self, func, *args):
        """Helper method to execute functions with proper session management."""
        try:
            # Import here to check availability
            import fast_linkedin_scraper  # noqa: F401
        except ImportError as e:
            raise ImportError(
                f"fast-linkedin-scraper is not installed. Please install it: pip install fast-linkedin-scraper. Error: {e}"
            )

        cookie = ensure_authentication()

        # Check if we're in an async context and need to run in thread pool
        import asyncio

        try:
            asyncio.get_running_loop()
            # We're in an async context, run the entire session operation in a new thread
            # This is necessary because fast-linkedin-scraper uses sync Playwright APIs
            logger.debug(
                "Async context detected, executing scraper session in dedicated thread"
            )

            def _sync_session_execution():
                # Don't pre-initialize Playwright - let fast-linkedin-scraper handle it completely
                # This avoids the _connection attribute issues
                logger.debug(
                    "Executing scraper session in sync thread (no pre-initialization)"
                )
                return self._sync_execute_with_session(func, cookie, *args)

            import threading

            # Use a dedicated thread rather than a thread pool to avoid threading issues
            result_container = {}
            exception_container = {}

            def thread_target():
                try:
                    result_container["result"] = _sync_session_execution()
                except Exception as e:
                    exception_container["exception"] = e

            thread = threading.Thread(target=thread_target)
            thread.start()
            thread.join(timeout=120)  # 2 minute timeout

            if thread.is_alive():
                raise Exception("Scraping operation timed out after 2 minutes")

            if "exception" in exception_container:
                raise exception_container["exception"]

            return result_container.get("result")

        except RuntimeError:
            # No async context, execute directly
            logger.debug(
                "No async context, executing scraper session directly (no pre-initialization)"
            )
            return self._sync_execute_with_session(func, cookie, *args)

    def _sync_execute_with_session(self, func, cookie, *args):
        """Execute session operations synchronously."""
        from fast_linkedin_scraper import LinkedInSession
        from fast_linkedin_scraper.exceptions import InvalidCredentialsError

        # Handle common fast-linkedin-scraper errors with retry logic
        max_retries = 2
        for attempt in range(max_retries):
            try:
                logger.debug(
                    f"Attempt {attempt + 1}/{max_retries}: Creating LinkedInSession with context manager"
                )

                # Add a small delay for subsequent attempts
                if attempt > 0:
                    import time

                    time.sleep(1)
                    logger.debug("Retrying after delay...")

                with LinkedInSession.from_cookie(cookie) as session:
                    logger.debug(f"Session created successfully, type: {type(session)}")
                    return func(session, *args)

            except InvalidCredentialsError as cred_error:
                logger.error(f"LinkedIn authentication failed: {cred_error}")
                raise Exception(
                    f"LinkedIn authentication failed. Cookie may be invalid or expired: {cred_error}"
                )

            except AttributeError as attr_error:
                attr_error_str = str(attr_error)
                if (
                    "'PlaywrightContextManager' object has no attribute '_connection'"
                    in attr_error_str
                ):
                    logger.error(
                        f"Playwright context manager error: {attr_error}. "
                        f"This is a known issue with fast-linkedin-scraper library."
                    )
                    raise Exception(
                        "fast-linkedin-scraper has Playwright context management issues. "
                        "This may be due to version compatibility or installation problems. "
                        "Try: pip install --upgrade fast-linkedin-scraper playwright && playwright install"
                    )
                else:
                    raise attr_error

            except Exception as e:
                error_msg = str(e).lower()

                # Handle specific error patterns
                if "net::err_too_many_redirects" in error_msg:
                    raise Exception(
                        "LinkedIn is redirecting too much - possible rate limiting or bot detection. Try again later or check cookie validity."
                    )
                elif "connection" in error_msg and "playwright" in error_msg:
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Playwright connection issue on attempt {attempt + 1}, retrying..."
                        )
                        continue
                    else:
                        raise Exception(
                            "Playwright connection issue persisted after retries. Try running: playwright install && playwright install-deps"
                        )
                elif "timeout" in error_msg:
                    if attempt < max_retries - 1:
                        logger.warning(f"Timeout on attempt {attempt + 1}, retrying...")
                        continue
                    else:
                        raise Exception(
                            "LinkedIn page loading timeout persisted after retries. Network issues or LinkedIn is slow."
                        )
                else:
                    # Re-raise original error for non-retryable issues
                    logger.error(f"Non-retryable error in fast-linkedin-scraper: {e}")
                    raise

    def get_person_profile(self, linkedin_username: str) -> Dict[str, Any]:
        """Get a person's LinkedIn profile using fast-linkedin-scraper."""
        try:
            linkedin_url = f"https://www.linkedin.com/in/{linkedin_username}/"
            logger.info(f"Scraping profile: {linkedin_url}")

            def _get_profile(session, url):
                person = session.get_profile(url)
                return person.model_dump()

            return self._execute_with_session(_get_profile, linkedin_url)

        except ImportError:
            raise ImportError(
                "fast-linkedin-scraper is not installed. Please install it: pip install fast-linkedin-scraper"
            )
        except Exception as e:
            logger.error(f"Error scraping profile {linkedin_url}: {str(e)}")
            raise

    def get_company_profile(
        self, company_name: str, get_employees: bool = False
    ) -> Dict[str, Any]:
        """Get a company's LinkedIn profile using fast-linkedin-scraper."""
        try:
            linkedin_url = f"https://www.linkedin.com/company/{company_name}/"
            logger.info(f"Scraping company: {linkedin_url}")

            def _get_company(session, url, get_emps):
                company = session.get_company(url)
                result = company.model_dump()

                # Add employee data if requested
                if get_emps:
                    logger.info("Fetching employees may take a while...")
                    try:
                        employees = session.get_company_employees(url)
                        result["employees"] = [emp.model_dump() for emp in employees]
                    except Exception as e:
                        logger.warning(f"Could not fetch employees: {str(e)}")
                        result["employees"] = []

                return result

            return self._execute_with_session(_get_company, linkedin_url, get_employees)

        except ImportError:
            raise ImportError(
                "fast-linkedin-scraper is not installed. Please install it: pip install fast-linkedin-scraper"
            )
        except Exception as e:
            logger.error(f"Error scraping company {linkedin_url}: {str(e)}")
            raise

    def get_job_details(self, job_id: str) -> Dict[str, Any]:
        """Get job details using fast-linkedin-scraper."""
        try:
            job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
            logger.info(f"Scraping job: {job_url}")

            def _get_job(session, url):
                job = session.get_job(url)
                return job.model_dump()

            return self._execute_with_session(_get_job, job_url)

        except ImportError:
            raise ImportError(
                "fast-linkedin-scraper is not installed. Please install it: pip install fast-linkedin-scraper"
            )
        except Exception as e:
            logger.error(f"Error scraping job {job_url}: {str(e)}")
            raise

    def search_jobs(self, search_term: str) -> List[Dict[str, Any]]:
        """Search for jobs using fast-linkedin-scraper."""
        try:
            logger.info(f"Searching jobs: {search_term}")

            def _search_jobs(session, term):
                jobs = session.search_jobs(term)
                return [job.model_dump() for job in jobs]

            return self._execute_with_session(_search_jobs, search_term)

        except ImportError:
            raise ImportError(
                "fast-linkedin-scraper is not installed. Please install it: pip install fast-linkedin-scraper"
            )
        except Exception as e:
            logger.error(f"Error searching jobs: {str(e)}")
            raise

    def get_recommended_jobs(self) -> List[Dict[str, Any]]:
        """Get recommended jobs using fast-linkedin-scraper."""
        try:
            logger.info("Getting recommended jobs")

            def _get_recommended_jobs(session):
                jobs = session.get_recommended_jobs()
                return [job.model_dump() for job in jobs]

            return self._execute_with_session(_get_recommended_jobs)

        except ImportError:
            raise ImportError(
                "fast-linkedin-scraper is not installed. Please install it: pip install fast-linkedin-scraper"
            )
        except Exception as e:
            logger.error(f"Error getting recommended jobs: {str(e)}")
            raise


class LegacyLinkedInScraperAdapter:
    """Adapter for legacy linkedin-scraper library."""

    def get_person_profile(self, linkedin_username: str) -> Dict[str, Any]:
        """Get a person's LinkedIn profile using legacy linkedin-scraper."""
        from linkedin_scraper import Person
        from linkedin_mcp_server.drivers import get_driver_for_scraper_type

        linkedin_url = f"https://www.linkedin.com/in/{linkedin_username}/"
        driver = get_driver_for_scraper_type()

        logger.info(f"Scraping profile: {linkedin_url}")
        person = Person(linkedin_url, driver=driver, close_on_complete=False)

        # Convert experiences to structured dictionaries
        experiences: List[Dict[str, Any]] = [
            {
                "position_title": exp.position_title,
                "company": exp.institution_name,
                "from_date": exp.from_date,
                "to_date": exp.to_date,
                "duration": exp.duration,
                "location": exp.location,
                "description": exp.description,
            }
            for exp in person.experiences
        ]

        # Convert educations to structured dictionaries
        educations: List[Dict[str, Any]] = [
            {
                "institution": edu.institution_name,
                "degree": edu.degree,
                "from_date": edu.from_date,
                "to_date": edu.to_date,
                "description": edu.description,
            }
            for edu in person.educations
        ]

        # Convert interests to list of titles
        interests: List[str] = [interest.title for interest in person.interests]

        # Convert accomplishments to structured dictionaries
        accomplishments: List[Dict[str, str]] = [
            {"category": acc.category, "title": acc.title}
            for acc in person.accomplishments
        ]

        # Convert contacts to structured dictionaries
        contacts: List[Dict[str, str]] = [
            {
                "name": contact.name,
                "occupation": contact.occupation,
                "url": contact.url,
            }
            for contact in person.contacts
        ]

        return {
            "name": person.name,
            "about": person.about,
            "experiences": experiences,
            "educations": educations,
            "interests": interests,
            "accomplishments": accomplishments,
            "contacts": contacts,
            "company": person.company,
            "job_title": person.job_title,
            "open_to_work": getattr(person, "open_to_work", False),
        }

    def get_company_profile(
        self, company_name: str, get_employees: bool = False
    ) -> Dict[str, Any]:
        """Get a company's LinkedIn profile using legacy linkedin-scraper."""
        from linkedin_scraper import Company
        from linkedin_mcp_server.drivers import get_driver_for_scraper_type

        linkedin_url = f"https://www.linkedin.com/company/{company_name}/"
        driver = get_driver_for_scraper_type()

        logger.info(f"Scraping company: {linkedin_url}")
        if get_employees:
            logger.info("Fetching employees may take a while...")

        company = Company(
            linkedin_url,
            driver=driver,
            get_employees=get_employees,
            close_on_complete=False,
        )

        # Convert showcase pages to structured dictionaries
        showcase_pages: List[Dict[str, Any]] = [
            {
                "name": page.name,
                "linkedin_url": page.linkedin_url,
                "followers": page.followers,
            }
            for page in company.showcase_pages
        ]

        # Convert affiliated companies to structured dictionaries
        affiliated_companies: List[Dict[str, Any]] = [
            {
                "name": affiliated.name,
                "linkedin_url": affiliated.linkedin_url,
                "followers": affiliated.followers,
            }
            for affiliated in company.affiliated_companies
        ]

        # Build the result dictionary
        result: Dict[str, Any] = {
            "name": company.name,
            "about_us": company.about_us,
            "website": company.website,
            "phone": company.phone,
            "headquarters": company.headquarters,
            "founded": company.founded,
            "industry": company.industry,
            "company_type": company.company_type,
            "company_size": company.company_size,
            "specialties": company.specialties,
            "showcase_pages": showcase_pages,
            "affiliated_companies": affiliated_companies,
            "headcount": company.headcount,
        }

        # Add employees if requested and available
        if get_employees and company.employees:
            result["employees"] = company.employees

        return result

    def get_job_details(self, job_id: str) -> Dict[str, Any]:
        """Get job details using legacy linkedin-scraper."""
        from linkedin_scraper import Job
        from linkedin_mcp_server.drivers import get_driver_for_scraper_type

        job_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        driver = get_driver_for_scraper_type()

        logger.info(f"Scraping job: {job_url}")
        job = Job(job_url, driver=driver, close_on_complete=False)

        return job.to_dict()

    def search_jobs(self, search_term: str) -> List[Dict[str, Any]]:
        """Search for jobs using legacy linkedin-scraper."""
        from linkedin_scraper import JobSearch
        from linkedin_mcp_server.drivers import get_driver_for_scraper_type

        driver = get_driver_for_scraper_type()

        logger.info(f"Searching jobs: {search_term}")
        job_search = JobSearch(driver=driver, close_on_complete=False, scrape=False)
        jobs = job_search.search(search_term)

        return [job.to_dict() for job in jobs]

    def get_recommended_jobs(self) -> List[Dict[str, Any]]:
        """Get recommended jobs using legacy linkedin-scraper."""
        from linkedin_scraper import JobSearch
        from linkedin_mcp_server.drivers import get_driver_for_scraper_type

        driver = get_driver_for_scraper_type()

        logger.info("Getting recommended jobs")
        job_search = JobSearch(
            driver=driver,
            close_on_complete=False,
            scrape=True,  # Enable scraping to get recommended jobs
            scrape_recommended_jobs=True,
        )

        if hasattr(job_search, "recommended_jobs") and job_search.recommended_jobs:
            return [job.to_dict() for job in job_search.recommended_jobs]
        else:
            return []


def get_scraper_adapter() -> ScraperAdapter:
    """Get the appropriate scraper adapter based on configuration."""
    config = get_config()
    scraper_type = config.linkedin.scraper_type

    logger.info(f"Using scraper type: {scraper_type}")

    if scraper_type == "fast-linkedin-scraper":
        return FastLinkedInScraperAdapter()
    elif scraper_type == "linkedin-scraper":
        return LegacyLinkedInScraperAdapter()
    else:
        raise ValueError(f"Unknown scraper type: {scraper_type}")
