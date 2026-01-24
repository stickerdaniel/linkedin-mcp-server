"""
Centralized CSS selectors for LinkedIn automation.

Keeps all selectors in one place for easier maintenance when
LinkedIn updates their UI.
"""


class SearchSelectors:
    """Selectors for LinkedIn search pages."""

    # Search input
    SEARCH_INPUT = "input.search-global-typeahead__input"
    SEARCH_BUTTON = "button.search-global-typeahead__collapsed-search-button"

    # Search filters
    PEOPLE_FILTER = "button[aria-label='People']"
    COMPANIES_FILTER = "button[aria-label='Companies']"
    JOBS_FILTER = "button[aria-label='Jobs']"

    # Filter dropdowns
    ALL_FILTERS_BUTTON = "button:has-text('All filters')"
    CONNECTIONS_FILTER = "button[aria-label='Connections filter']"
    LOCATIONS_FILTER = "button[aria-label='Locations filter']"
    CURRENT_COMPANY_FILTER = "button[aria-label='Current company filter']"
    INDUSTRY_FILTER = "button[aria-label='Industry filter']"

    # Search results - People
    PEOPLE_RESULTS = "ul.reusable-search__entity-result-list"
    PERSON_CARD = "li.reusable-search__result-container"
    PERSON_NAME = "span.entity-result__title-text a span[aria-hidden='true']"
    PERSON_HEADLINE = "div.entity-result__primary-subtitle"
    PERSON_LOCATION = "div.entity-result__secondary-subtitle"
    PERSON_PROFILE_LINK = "a.app-aware-link[href*='/in/']"

    # Search results - Companies
    COMPANY_RESULTS = "ul.reusable-search__entity-result-list"
    COMPANY_CARD = "li.reusable-search__result-container"
    COMPANY_NAME = "span.entity-result__title-text a span[aria-hidden='true']"
    COMPANY_INDUSTRY = "div.entity-result__primary-subtitle"
    COMPANY_LOCATION = "div.entity-result__secondary-subtitle"
    COMPANY_PROFILE_LINK = "a.app-aware-link[href*='/company/']"

    # Pagination
    NEXT_PAGE = "button[aria-label='Next']"
    PAGINATION = "ul.artdeco-pagination__pages"
    CURRENT_PAGE = "button.artdeco-pagination__indicator--number.active"

    # No results
    NO_RESULTS = "div.search-reusable-search-no-results"


class ProfileSelectors:
    """Selectors for LinkedIn profile pages."""

    # Profile header
    PROFILE_NAME = "h1.text-heading-xlarge"
    PROFILE_HEADLINE = "div.text-body-medium"
    PROFILE_LOCATION = "span.text-body-small"
    PROFILE_ABOUT = "section.artdeco-card div.display-flex span[aria-hidden='true']"

    # Connection button states
    CONNECT_BUTTON = "button:has-text('Connect')"
    CONNECT_BUTTON_ALT = "div.pvs-profile-actions button:has-text('Connect')"
    MORE_BUTTON = "button[aria-label='More actions']"
    CONNECT_IN_DROPDOWN = "div[aria-label='Connect'] span"
    PENDING_BUTTON = "button:has-text('Pending')"
    MESSAGE_BUTTON = "button:has-text('Message')"
    FOLLOW_BUTTON = "button:has-text('Follow')"
    FOLLOWING_BUTTON = "button:has-text('Following')"

    # Connection modal
    ADD_NOTE_BUTTON = "button[aria-label='Add a note']"
    NOTE_TEXTAREA = "textarea[name='message']"
    SEND_BUTTON = "button[aria-label='Send invitation']"
    SEND_BUTTON_ALT = "button[aria-label='Send now']"
    CLOSE_MODAL = "button[aria-label='Dismiss']"

    # Profile sections
    EXPERIENCE_SECTION = "section#experience"
    EDUCATION_SECTION = "section#education"
    SKILLS_SECTION = "section.artdeco-card:has(div#skills)"


class CompanySelectors:
    """Selectors for LinkedIn company pages."""

    # Company header
    COMPANY_NAME = "h1.org-top-card-summary__title"
    COMPANY_INDUSTRY = "div.org-top-card-summary__tagline"
    COMPANY_SIZE = "dd.org-about-company-module__company-size-definition-text"
    COMPANY_LOCATION = "div.org-top-card-summary__headquarter"

    # Follow button states
    FOLLOW_BUTTON = "button.follow"
    FOLLOW_BUTTON_ALT = "button:has-text('Follow')"
    FOLLOWING_BUTTON = "button:has-text('Following')"
    FOLLOWING_BUTTON_ALT = "button.follow--following"

    # Company tabs
    ABOUT_TAB = "a[href*='/about/']"
    POSTS_TAB = "a[href*='/posts/']"
    JOBS_TAB = "a[href*='/jobs/']"
    PEOPLE_TAB = "a[href*='/people/']"


class JobSelectors:
    """Selectors for LinkedIn job pages."""

    # Job search
    JOB_SEARCH_INPUT = "input[aria-label='Search by title, skill, or company']"
    JOB_LOCATION_INPUT = "input[aria-label='City, state, or zip code']"
    JOB_SEARCH_BUTTON = "button.jobs-search-box__submit-button"

    # Job results
    JOB_RESULTS = "ul.scaffold-layout__list-container"
    JOB_CARD = "li.jobs-search-results__list-item"
    JOB_TITLE = "a.job-card-list__title"
    JOB_COMPANY = "a.job-card-container__company-name"
    JOB_LOCATION = "li.job-card-container__metadata-item"

    # Job details
    JOB_DETAIL_TITLE = "h1.job-details-jobs-unified-top-card__job-title"
    JOB_DETAIL_COMPANY = "a.job-details-jobs-unified-top-card__company-name"
    JOB_DESCRIPTION = "div.jobs-description"
    APPLY_BUTTON = "button.jobs-apply-button"
    SAVE_BUTTON = "button[aria-label='Save job']"


class CommonSelectors:
    """Common selectors used across pages."""

    # Loading indicators
    LOADING_SPINNER = "div.artdeco-spinner"
    LOADING_OVERLAY = "div.artdeco-loader__bars"

    # Toast notifications
    TOAST_SUCCESS = "div.artdeco-toast-item--success"
    TOAST_ERROR = "div.artdeco-toast-item--error"
    TOAST_MESSAGE = "p.artdeco-toast-item__message"

    # Modal dialogs
    MODAL_CONTAINER = "div.artdeco-modal"
    MODAL_CLOSE = "button.artdeco-modal__dismiss"
    MODAL_CONFIRM = "button.artdeco-modal__confirm"

    # Rate limit / captcha indicators
    RATE_LIMIT_PAGE = "div.challenge-container"
    CAPTCHA = "div.captcha-internal-container"
    AUTH_WALL = "div.authwall-container"

    # Global elements
    GLOBAL_NAV = "header.global-nav"
    FEED_CONTAINER = "div.scaffold-layout__content"
