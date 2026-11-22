"""Strategies shared by the property test files.

Every literal below is a form already present in the repository: the hosts and
path wrappers in ``tests/test_identifiers.py``, the routes in
``linkedin/identifiers.py`` and ``linkedin/link_metadata.py``, the escapes in
``TestOneJobGrammar``.
"""

from __future__ import annotations

from hypothesis import strategies as st

SCHEMES = ("", "https://", "http://", "//", "HTTPS://", "ftp://")
HOSTS = (
    "www.linkedin.com",
    "linkedin.com",
    "de.linkedin.com",
    "m.linkedin.com",
    "LINKEDIN.COM",
    "www.linkedin.com.",
    "www.linkedin.com:443",
    "www.linkedin.com:444",
    "user@www.linkedin.com",
    "lnkd.in",
    "evil-linkedin.com",
    "example.com",
    "grnh.se",
    "127.0.0.1",
    "[::1]",
    "[",
    "a]",
)
ROUTES = (
    "in",
    "company",
    "school",
    "jobs/view",
    "jobs/search",
    "jobs/search-results",
    "messaging/thread",
    "mwlite/in",
    "mwlite/profile/in",
    "feed/update",
    "safety/go",
    "redir/redirect",
    "IN",
)
TAILS = (
    "",
    "/",
    "/details/experience/",
    "?trk=public_profile",
    "#experience",
    "/?currentJobId=4252026496",
    "?currentJobId=1&currentJobId=2",
    "?url=https%3A%2F%2Fgrnh.se%2Fabc",
    "?url=http%3A%2F%2F%5B",
)
PIECES = (
    *"abzAZ019_-.&'%/\\?#:@= \t\n",
    "é",
    "а",
    "中",
    "٠",
    "\ud800",
    "%2e",
    "%2F",
    "%25",
    "%2541",
    "%252e%252e",
    "%20",
    "%C3%A9",
    "%FF",
    "..",
    "me",
    "new",
    "linkedin.com",
    "lnkd.in",
    "://",
    "currentJobId=",
    "4252026496",
    "-at-acme-",
    "urn:li:fsd_profile:",
    "ACoA",
)

segments = st.lists(st.sampled_from(PIECES), max_size=8).map("".join)


@st.composite
def addresses(draw: st.DrawFn) -> str:
    scheme = draw(st.sampled_from(SCHEMES))
    host = draw(st.sampled_from(HOSTS)) if scheme or draw(st.booleans()) else ""
    route = draw(st.sampled_from(ROUTES))
    pad = draw(st.sampled_from(("", " ", "\n")))
    return f"{pad}{scheme}{host}/{route}/{draw(segments)}{draw(st.sampled_from(TAILS))}{pad}"


references = st.one_of(st.text(max_size=30), segments, addresses())


JOB_TITLES = (
    "",
    "engineer",
    "senior-.net-developer-at-acme",
    "c%2B%2B-engineer-at-acme",
    "100%25-remote-developer-at-acme",
    "2026-software-engineer-at-acme",
    "ui%2Fux-designer-at-acme",
    "what%3F-at-acme",
    "caf%FF-at-acme",
    "engineer-(ai)",
)
JOB_IDS = ("4252026496", "1", "2026", "\u0664\u0662\u0665", "", "12a")
JOB_QUERIES = (
    "",
    "?currentJobId=4252026496",
    "?currentJobId=1&currentJobId=2",
    "?currentJobId=&currentJobId=1",
    "?current%4AobId=7",
    "?currentJobId=%D9%A4%D9%A2%D9%A5",
    "?trk=x#currentJobId=3",
)


@st.composite
def job_addresses(draw: st.DrawFn) -> str:
    route = draw(st.sampled_from(("jobs/view", "jobs/search", "jobs/search-results")))
    title = draw(st.sampled_from(JOB_TITLES))
    number = draw(st.sampled_from(JOB_IDS))
    segment = f"{title}-{number}" if title else number
    path = f"/{route}/{segment}/" if route == "jobs/view" else f"/{route}/"
    return f"https://www.linkedin.com{path}{draw(st.sampled_from(JOB_QUERIES))}"


KNOWN_JOB_TITLES = (
    "",
    "engineer-",
    "senior-.net-developer-at-acme-",
    "c%2B%2B-engineer-at-acme-",
    "100%25-remote-developer-at-acme-",
    "2026-software-engineer-at-acme-",
)


@st.composite
def known_job_addresses(draw: st.DrawFn) -> tuple[str, str]:
    job_id = str(draw(st.integers(1, 10**12)))
    host = draw(st.sampled_from(("www.linkedin.com", "de.linkedin.com")))
    path = draw(
        st.sampled_from(
            (
                "/jobs/view/{title}{job_id}/",
                "/jobs/view/{title}{job_id}/?trk=public_jobs",
                "/jobs/search/?keywords=python&currentJobId={job_id}",
                "/jobs/search-results/?currentJobId={job_id}&start=25",
            )
        )
    )
    title = draw(st.sampled_from(KNOWN_JOB_TITLES))
    return f"https://{host}{path.format(title=title, job_id=job_id)}", job_id
