"""Tests for the classifier deciding whether a page is one LinkedIn served."""

import pytest

from linkedin_mcp_server.core.destination import (
    describe_landing,
    is_another_site,
    is_linkedin_landing,
    raise_if_off_linkedin,
)
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    NetworkError,
    OffLinkedInLandingError,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/testuser/",
        "https://linkedin.com/feed/",
        # Locale subdomains serve a profile themselves.
        "https://de.linkedin.com/in/testuser/",
        "https://WWW.LinkedIn.COM/feed/",
        "https://www.linkedin.com./feed/",
        "https://www.linkedin.com:443/feed/",
        "https://www.linkedin.com/jobs/search/?keywords=python#top",
    ],
)
def test_linkedin_documents_are_linkedin_landings(url: str):
    assert is_linkedin_landing(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # A suffix that is not on a label boundary.
        "https://evil-linkedin.com/in/testuser/",
        "https://notlinkedin.com/feed/",
        # LinkedIn's name as a label of someone else's host.
        "https://linkedin.com.evil.test/feed/",
        "https://www.linkedin.com.evil.test/in/testuser/",
        "https://portal.invalid/login",
        # LinkedIn's host, but not a page LinkedIn serves that way.
        "http://www.linkedin.com/feed/",
        "https://www.linkedin.com:8443/feed/",
        "https://user:pass@www.linkedin.com/feed/",
        "https://www.linkedin.com../feed/",
        "https://[::1/feed/",
    ],
)
def test_foreign_and_deceptive_hosts_are_not(url: str):
    assert is_linkedin_landing(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "about:blank",
        "data:text/html,<main>LinkedIn</main>",
        "blob:https://www.linkedin.com/1b2c3d",
        "file:///www.linkedin.com/feed/",
        "chrome-error://chromewebdata/",
        "",
        None,
    ],
)
def test_hostless_documents_are_not(url: object):
    """An interrupted navigation or a cleared page is not a signed-in LinkedIn."""
    assert is_linkedin_landing(url) is False


@pytest.mark.parametrize(
    ("url", "another_site"),
    [
        ("https://portal.invalid/interstitial", True),
        ("http://192.0.2.1/login", True),
        ("https://evil-linkedin.com/feed/", True),
        ("https://www.linkedin.com/in/testuser/", False),
        # What a failed request leaves: its own error says more.
        ("chrome-error://chromewebdata/", False),
        ("about:blank", False),
        ("data:text/html,<p>x</p>", False),
        (None, False),
    ],
)
def test_another_site_is_a_web_page_from_a_foreign_host(
    url: object, another_site: bool
):
    assert is_another_site(url) is another_site


@pytest.mark.parametrize(
    ("url", "description"),
    [
        ("https://portal.invalid/interstitial?token=s3cret", "https://portal.invalid"),
        ("http://192.0.2.1:8080/login#x", "http://192.0.2.1:8080"),
        ("https://user:pw@filter.example/block", "https://filter.example"),
        ("about:blank", "about:blank"),
        ("data:text/html,<p>secret</p>", "a data: document"),
        (None, "an unknown page"),
    ],
)
def test_the_description_names_the_origin_and_nothing_after_it(
    url: object, description: str
):
    assert describe_landing(url) == description


def test_an_off_linkedin_landing_is_a_network_failure_naming_where_it_landed():
    with pytest.raises(OffLinkedInLandingError) as excinfo:
        raise_if_off_linkedin("https://portal.invalid/interstitial?token=s3cret")

    error = excinfo.value
    # A network error, so nothing that recovers from an expired session runs.
    assert isinstance(error, NetworkError)
    assert not isinstance(error, AuthenticationError)
    assert "https://portal.invalid" in str(error)
    assert "s3cret" not in str(error)


def test_a_linkedin_landing_passes():
    raise_if_off_linkedin("https://www.linkedin.com/in/testuser/")
