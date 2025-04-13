from typing import Optional, List, Dict, Any
import os
import asyncio
import getpass
import json
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import WebDriverException

# Import LinkedIn scraper components
from linkedin_scraper import Person, Company, Job, JobSearch, actions

# Initialize FastMCP server
mcp = FastMCP("linkedin_scraper")

# Global driver storage to reuse sessions
active_drivers: Dict[str, webdriver.Chrome] = {}
credentials_file = Path.home() / ".linkedin_mcp_credentials.json"

def setup_credentials() -> Dict[str, str]:
    """Ask for LinkedIn credentials during setup and store them securely."""
    if credentials_file.exists():
        try:
            with open(credentials_file, "r") as f:
                credentials = json.load(f)
                if "email" in credentials and "password" in credentials:
                    return credentials
        except Exception as e:
            print(f"Error reading credentials file: {e}")
    
    print("LinkedIn credentials are required for the scraper to work.")
    email = input("LinkedIn Email: ")
    password = getpass.getpass("LinkedIn Password: ")
    
    credentials = {"email": email, "password": password}
    
    # Store credentials securely
    try:
        with open(credentials_file, "w") as f:
            json.dump(credentials, f)
        
        # Set permissions to user-only read/write
        os.chmod(credentials_file, 0o600)
        print(f"Credentials stored securely at {credentials_file}")
    except Exception as e:
        print(f"Warning: Could not store credentials: {e}")
    
    return credentials

def get_chromedriver_path() -> Optional[str]:
    """Get the ChromeDriver path from environment variable or default locations."""
    chromedriver_path = os.getenv("CHROMEDRIVER")
    if chromedriver_path and os.path.exists(chromedriver_path):
        return chromedriver_path
    
    # Check common locations
    possible_paths = [
        os.path.join(os.path.dirname(__file__), 'drivers/chromedriver'),
        os.path.join(os.path.expanduser("~"), 'chromedriver'),
        '/usr/local/bin/chromedriver',
        '/usr/bin/chromedriver',
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    return None

async def initialize_driver() -> None:
    """Initialize the driver and log in at server start."""
    credentials = setup_credentials()
    
    # Validate chromedriver can be found
    chromedriver_path = get_chromedriver_path()
    if not chromedriver_path:
        print("WARNING: ChromeDriver not found in PATH or common locations.")
        print("Please set the CHROMEDRIVER environment variable to your ChromeDriver path.")
        print("Continuing with automatic detection (may fail)...")
    else:
        print(f"Using ChromeDriver at: {chromedriver_path}")
    
    try:
        driver = await get_or_create_driver(headless=True)
        
        # Login to LinkedIn
        try:
            actions.login(driver, credentials["email"], credentials["password"])
            print("✅ Successfully logged in to LinkedIn")
        except Exception as e:
            print(f"❌ Failed to login: {str(e)}")
            print("Please check your credentials and try again.")
    except WebDriverException as e:
        print(f"❌ Failed to initialize web driver: {str(e)}")
        print("Please ensure ChromeDriver is properly installed and in your PATH.")

async def get_or_create_driver(headless: bool = True) -> webdriver.Chrome:
    """Get existing driver or create a new one."""
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
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36")
    
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

@mcp.tool()
async def get_person_profile(linkedin_url: str) -> Dict[str, Any]:
    """Scrape a person's LinkedIn profile.
    
    Args:
        linkedin_url (str): The LinkedIn URL of the person's profile
    """
    driver = await get_or_create_driver()
    
    try:
        print(f"Scraping profile: {linkedin_url}")
        person = Person(linkedin_url, driver=driver, close_on_complete=False)
        
        # Convert person object to a dictionary
        return {
            "name": person.name,
            "about": person.about,
            "experiences": [
                {
                    "position_title": exp.position_title,
                    "company": exp.institution_name,
                    "from_date": exp.from_date,
                    "to_date": exp.to_date,
                    "duration": exp.duration,
                    "location": exp.location,
                    "description": exp.description
                } for exp in person.experiences
            ],
            "educations": [
                {
                    "institution": edu.institution_name,
                    "degree": edu.degree,
                    "from_date": edu.from_date,
                    "to_date": edu.to_date,
                    "description": edu.description
                } for edu in person.educations
            ],
            "interests": [interest.title for interest in person.interests],
            "accomplishments": [
                {
                    "category": acc.category,
                    "title": acc.title
                } for acc in person.accomplishments
            ],
            "contacts": [
                {
                    "name": contact.name,
                    "occupation": contact.occupation,
                    "url": contact.url
                } for contact in person.contacts
            ],
            "company": person.company,
            "job_title": person.job_title,
            "open_to_work": getattr(person, "open_to_work", False)
        }
    except Exception as e:
        print(f"Error scraping profile: {e}")
        return {"error": f"Failed to scrape profile: {str(e)}"}


@mcp.tool()
async def get_company_profile(linkedin_url: str, get_employees: bool = False) -> Dict[str, Any]:
    """Scrape a company's LinkedIn profile.
    
    Args:
        linkedin_url (str): The LinkedIn URL of the company's profile
        get_employees (bool): Whether to scrape the company's employees (slower)
    """
    driver = await get_or_create_driver()
    
    try:
        print(f"Scraping company: {linkedin_url}")
        company = Company(linkedin_url, driver=driver, get_employees=get_employees, close_on_complete=False)
        
        # Convert company object to a dictionary
        result = {
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
            "showcase_pages": [
                {
                    "name": page.name,
                    "linkedin_url": page.linkedin_url,
                    "followers": page.followers
                } for page in company.showcase_pages
            ],
            "affiliated_companies": [
                {
                    "name": affiliated.name,
                    "linkedin_url": affiliated.linkedin_url,
                    "followers": affiliated.followers
                } for affiliated in company.affiliated_companies
            ],
            "headcount": company.headcount
        }
        
        if get_employees and company.employees:
            result["employees"] = company.employees
            
        return result
    except Exception as e:
        print(f"Error scraping company: {e}")
        return {"error": f"Failed to scrape company profile: {str(e)}"}


@mcp.tool()
async def get_job_details(job_url: str) -> Dict[str, Any]:
    """Scrape job details from a LinkedIn job posting.
    
    Args:
        job_url (str): The LinkedIn URL of the job posting
    """
    driver = await get_or_create_driver()
    
    try:
        print(f"Scraping job: {job_url}")
        job = Job(job_url, driver=driver, close_on_complete=False)
        
        # Convert job object to a dictionary
        return job.to_dict()
    except Exception as e:
        print(f"Error scraping job: {e}")
        return {"error": f"Failed to scrape job posting: {str(e)}"}


@mcp.tool()
async def search_jobs(search_term: str) -> List[Dict[str, Any]]:
    """Search for jobs on LinkedIn with the given search term.
    
    Args:
        search_term (str): The job search query
    """
    driver = await get_or_create_driver()
    
    try:
        print(f"Searching jobs: {search_term}")
        job_search = JobSearch(driver=driver, close_on_complete=False, scrape=False)
        jobs = job_search.search(search_term)
        
        # Convert job objects to dictionaries
        return [job.to_dict() for job in jobs]
    except Exception as e:
        print(f"Error searching jobs: {e}")
        return [{"error": f"Failed to search jobs: {str(e)}"}]


@mcp.tool()
async def get_recommended_jobs() -> List[Dict[str, Any]]:
    """Get recommended jobs from your LinkedIn homepage."""
    driver = await get_or_create_driver()
    
    try:
        print("Getting recommended jobs")
        job_search = JobSearch(driver=driver, close_on_complete=False, scrape=True, scrape_recommended_jobs=True)
        
        # Get recommended jobs and convert to dictionaries
        if hasattr(job_search, "recommended_jobs"):
            return [job.to_dict() for job in job_search.recommended_jobs]
        else:
            return []
    except Exception as e:
        print(f"Error getting recommended jobs: {e}")
        return [{"error": f"Failed to get recommended jobs: {str(e)}"}]


@mcp.tool()
async def close_session() -> str:
    """Close the current browser session and clean up resources."""
    session_id = "default"  # Using the same default session as in get_or_create_driver
    
    if session_id in active_drivers:
        try:
            active_drivers[session_id].quit()
            del active_drivers[session_id]
            return "Successfully closed the browser session"
        except Exception as e:
            print(f"Error closing browser session: {e}")
            return f"Error closing browser session: {str(e)}"
    else:
        return "No active browser session to close"


async def shutdown_handler():
    """Clean up resources on shutdown."""
    for session_id, driver in list(active_drivers.items()):
        try:
            driver.quit()
            del active_drivers[session_id]
        except Exception as e:
            print(f"Error closing driver during shutdown: {e}")


if __name__ == "__main__":
    try:
        # Run the initialization before starting the MCP server
        asyncio.run(initialize_driver())
        
        # Run the MCP server with stdio transport
        print("Starting LinkedIn MCP server...")
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        print("\nShutting down LinkedIn MCP server...")
        asyncio.run(shutdown_handler())
    except Exception as e:
        print(f"Error running MCP server: {e}")
        asyncio.run(shutdown_handler())