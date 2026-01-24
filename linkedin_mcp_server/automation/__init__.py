"""
Automation module for LinkedIn browser automation.

Provides Playwright-based automation classes for search, connection requests,
and company follows with human-like behavior patterns.
"""

from .base import (
    AutomationError,
    BaseAutomation,
    ElementNotFoundError,
    NavigationError,
)
from .connect import (
    AlreadyConnectedError,
    ConnectionRequestAutomation,
    ConnectionRequestError,
    PendingConnectionError,
)
from .follow import (
    AlreadyFollowingError,
    CompanyFollowAutomation,
    FollowError,
    PersonFollowAutomation,
)
from .search import (
    CompanySearchAutomation,
    PeopleSearchAutomation,
    SearchResult,
)
from .selectors import (
    CommonSelectors,
    CompanySelectors,
    JobSelectors,
    ProfileSelectors,
    SearchSelectors,
)

__all__ = [
    # Base classes
    "BaseAutomation",
    "AutomationError",
    "ElementNotFoundError",
    "NavigationError",
    # Search
    "PeopleSearchAutomation",
    "CompanySearchAutomation",
    "SearchResult",
    # Connection
    "ConnectionRequestAutomation",
    "ConnectionRequestError",
    "AlreadyConnectedError",
    "PendingConnectionError",
    # Follow
    "CompanyFollowAutomation",
    "PersonFollowAutomation",
    "FollowError",
    "AlreadyFollowingError",
    # Selectors
    "SearchSelectors",
    "ProfileSelectors",
    "CompanySelectors",
    "JobSelectors",
    "CommonSelectors",
]
