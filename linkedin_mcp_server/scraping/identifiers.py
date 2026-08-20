"""Repair the profile reference a caller passes in, before it becomes a URL.

Every tool argument named ``linkedin_username`` or ``company_name`` ends up as a
path segment of a LinkedIn URL the browser then navigates to. Interpolating it
raw costs two things.

A pasted profile link never resolves. Handing a model a URL is the most natural
thing a user does, and ``https://www.linkedin.com/in/`` plus that URL is not a
page LinkedIn serves. The link arrives in more shapes than the canonical one:
a member's public profile URL carries the two-letter code of the country on
their profile (``de.``, ``ca.``, ``uk.``, ``fr.``, …) and those hosts answer
directly rather than redirecting to ``www.``, mobile adds ``m.``, ``touch.`` and
the ``/mwlite/in/`` and ``/mwlite/profile/in/`` path wrappers, and a share sheet
appends ``?originalSubdomain=`` or ``?trk=``.

A value containing ``../`` navigates somewhere else entirely. Browsers apply
RFC 3986 dot-segment removal before the request, so ``felix/../../feed``
resolves to ``/feed/`` and the profile tool returns the feed as if it were a
profile. Measured against live LinkedIn: that URL redirects to
``session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2F``. Query parameters
in this package already go through ``quote_plus``; the path segment is the one
place that did not.

Both are fixed here rather than at each call site, so a new URL builder cannot
miss one. :func:`normalize_person_identifier` is idempotent, which is what makes
it safe to apply at the extractor boundary even though tools and other extractor
methods both call in.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlparse

from linkedin_mcp_server.core.exceptions import LinkedInScraperException

__all__ = [
    "company_page_url",
    "normalize_company_identifier",
    "normalize_person_identifier",
    "person_profile_url",
]

# linkedin.com and every host under it. There is no canonical host to normalize
# toward, because the locale subdomain serves the profile itself.
_LINKEDIN_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")

# LinkedIn's own shortener. Only a redirect resolves one, which nothing here can
# follow, so it gets its own message instead of a repair attempt.
_SHORTENER_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*lnkd\.in$")

_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# Path, query and whitespace syntax that no identifier carries. The dot segments
# are what a browser would resolve away, taking the navigation with them.
_UNUSABLE = re.compile(r"[\s/\\?#]|[\x00-\x1f\x7f]")

# `me` is LinkedIn's alias for the signed-in member. A link that spells it must
# never collapse into that alias: reading the operator's own profile while
# reporting someone else's is a silently wrong answer, not a visible failure.
_RESERVED = {"me"}

# Routes that name a person and routes that name an organization. A school slug
# is accepted on the company side because /company/<school-slug> 301-redirects to
# /school/<slug>, so reusing it on the company route resolves (measured).
_PERSON_ROUTE = "in"
_ORGANIZATION_ROUTES = {"company", "school", "showcase"}

_ECHO_LIMIT = 96


def _echo(value: str) -> str:
    """The caller's own argument, shortened, for an error a model has to act on."""
    inline = re.sub(r"[\s\x00-\x1f\x7f]+", " ", value).strip()
    if len(inline) > _ECHO_LIMIT:
        inline = inline[: _ECHO_LIMIT - 1] + "…"
    return f'"{inline}"'


def _parse_linkedin_url(value: str) -> tuple[str, str] | None:
    """``(route, slug)`` for a LinkedIn web URL, or ``None`` when it is not one.

    Lenient on purpose: the input is whatever someone copied out of a browser, so
    a missing scheme, ``http``, any subdomain, tracking query, hash, trailing
    slash and profile sub-pages (``/in/x/recent-activity/all/``) all parse. The
    mobile-web-lite wrappers are unwrapped first. Case is preserved, because a
    share link from the app can carry a case-sensitive profile id where the
    public identifier normally sits.

    Raises:
        LinkedInScraperException: for an ``lnkd.in`` short link, which is a
            LinkedIn URL that only a redirect resolves.
    """
    candidate = value if _HAS_SCHEME.match(value) else f"https://{value}"
    try:
        url = urlparse(candidate)
    except ValueError:
        return None
    if url.scheme not in {"http", "https"}:
        return None
    host = (url.hostname or "").lower()
    if _SHORTENER_HOST.match(host):
        raise LinkedInScraperException(
            f"{_echo(value)} is a shortened LinkedIn link, and only a redirect "
            "resolves it. Open it and pass the linkedin.com/in/ address it lands on."
        )
    if not _LINKEDIN_HOST.match(host):
        return None

    segments = [segment for segment in url.path.split("/") if segment]
    if segments and segments[0].lower() == "mwlite":
        segments.pop(0)
        if segments and segments[0].lower() == "profile":
            segments.pop(0)

    route = segments[0].lower() if segments else ""
    slug = unquote(segments[1]).strip() if len(segments) > 1 else ""
    return route, slug


def _usable(slug: str) -> bool:
    return bool(slug) and not _UNUSABLE.search(slug)


def normalize_person_identifier(value: str) -> str:
    """The public identifier for a person, from a link or from the identifier.

    Idempotent: a bare identifier is returned unchanged, so applying this twice
    along a call chain is harmless.

    Raises:
        LinkedInScraperException: when the value cannot name a person, with the
            correction to make. Refusing here costs nothing; letting it through
            spends a navigation and returns another page's text as a profile.
    """
    value = value.strip()
    if not value:
        raise LinkedInScraperException(
            "Missing linkedin_username (the /in/ public identifier of the person)."
        )

    parsed = _parse_linkedin_url(value)
    if parsed is not None:
        route, slug = parsed
        if route != _PERSON_ROUTE or not _usable(slug) or slug.lower() in _RESERVED:
            raise LinkedInScraperException(
                f"{_echo(value)} is a LinkedIn link but not a personal profile. "
                "Pass the /in/ public identifier of a person, for example "
                '"williamhgates".'
            )
        return slug

    if not _usable(value):
        raise LinkedInScraperException(
            f"{_echo(value)} is not a LinkedIn public identifier. Pass the part "
            'after /in/ in a profile URL, for example "williamhgates".'
        )
    return value


def normalize_company_identifier(value: str) -> str:
    """The page slug for an organization, from a link or from the slug itself.

    Idempotent, and raises the same way :func:`normalize_person_identifier` does.
    """
    value = value.strip()
    if not value:
        raise LinkedInScraperException(
            "Missing company_name (the /company/ slug of the organization)."
        )

    parsed = _parse_linkedin_url(value)
    if parsed is not None:
        route, slug = parsed
        if route not in _ORGANIZATION_ROUTES or not _usable(slug):
            raise LinkedInScraperException(
                f"{_echo(value)} is a LinkedIn link but not a company page. "
                'Pass the /company/ slug, for example "microsoft".'
            )
        return slug

    if not _usable(value):
        raise LinkedInScraperException(
            f"{_echo(value)} is not a LinkedIn company slug. Pass the part after "
            '/company/ in a company URL, for example "microsoft".'
        )
    return value


def person_profile_url(identifier: str, suffix: str = "") -> str:
    """Profile URL for an already-normalized identifier, escaped as one segment.

    ``safe=""`` is the point: an identifier may not contribute path syntax, and a
    non-ASCII public identifier has to be percent-encoded to survive navigation.
    """
    return f"https://www.linkedin.com/in/{quote(identifier, safe='')}{suffix}"


def company_page_url(identifier: str, suffix: str = "") -> str:
    """Company page URL for an already-normalized slug, escaped as one segment."""
    return f"https://www.linkedin.com/company/{quote(identifier, safe='')}{suffix}"
