"""Repair the reference a caller passes in, before it becomes a URL.

Every tool argument that names a person, company, job or conversation ends up as
a path segment of a LinkedIn URL the browser then navigates to. Interpolating one
raw costs two things.

A pasted profile link never resolves. Handing a model a URL is the most natural
thing a user does, and ``https://www.linkedin.com/in/`` plus that URL is not a
page LinkedIn serves. The link arrives in more shapes than the canonical one: a
member's public profile URL carries the two-letter code of the country on their
profile (``de.``, ``ca.``, ``uk.``, ``fr.``, …) and those hosts answer directly
rather than redirecting to ``www.``, mobile adds ``m.``, ``touch.`` and the
``/mwlite/in/`` and ``/mwlite/profile/in/`` path wrappers, and a share sheet
appends ``?originalSubdomain=`` or ``?trk=``.

A value that resolves to a different path navigates somewhere else entirely.
Browsers apply RFC 3986 dot-segment removal before the request, so
``felix/../../feed`` reaches ``/feed/`` and the profile tool returns the feed as
if it were a profile. Percent-escaping the segment closes most of that, but not
all of it: ``quote`` leaves ``.`` untouched because a period is always URL-safe,
so a bare ``..`` still walks up a level. Both are refused here rather than
escaped, since neither can name anything.

Three rules earn their place by being easy to get wrong:

- **One decode, and no second layer.** A caller can hand over an already-escaped
  segment: ``get_my_profile`` reads the username straight out of ``page.url``
  after the ``/in/me/`` redirect, and a browser reports that path encoded.
  Passing it on unchanged would let the URL builder escape it again (``%D0``
  becomes ``%25D0``) and navigate to a path that is not the profile. So a value
  is decoded once. A result that still contains ``%`` is refused, because no
  identifier carries one and a second layer is how ``%252e%252e`` would survive
  into ``..`` on a later pass through this same function.
- **Malformed escapes are refused, never repaired.** ``unquote`` leaves ``%ZZ``
  untouched and turns ``%FF`` into a replacement character, so tolerating either
  silently changes the destination rather than rejecting the reference.
- **The reserved alias is refused in every form.** ``me`` is what LinkedIn
  resolves to the signed-in member, so accepting it from a caller that meant
  somebody else answers about the operator's own profile while reporting another
  person's.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlparse

from linkedin_mcp_server.core.exceptions import InvalidReferenceError

__all__ = [
    "company_page_url",
    "job_view_url",
    "messaging_thread_url",
    "normalize_company_identifier",
    "normalize_opaque_id",
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

# Path, query and whitespace syntax that no identifier carries.
_UNUSABLE = re.compile(r"[\s/\\?#]|[\x00-\x1f\x7f]")

# A `%` that begins no valid escape. LinkedIn references contain no literal one,
# so this is always a malformed escape rather than content.
_STRAY_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")

# The two segments a browser resolves away. `quote` cannot help here: a period is
# unreserved, so it survives escaping and `/in/../` still walks up a level.
_DOT_SEGMENTS = {".", ".."}

# `me` is what LinkedIn resolves to the signed-in member. get_my_profile is the
# tool for that; reaching it through a person lookup answers about the wrong one.
_RESERVED = {"me"}

# Routes that name a person and routes that name an organization. A school slug
# is accepted on the company side because /company/<school-slug> 301-redirects to
# /school/<slug>, measured at the root. The section routes the company scrape
# appends (/posts/, /people/) answer 302-to-login for a school slug rather than
# 404, so they are recognised, but what they render needs a signed-in session to
# establish and has not been. Accepting the slug is still better than refusing
# it: the caller gets LinkedIn's own answer instead of a refusal Cadenza cannot
# justify.
_PERSON_ROUTE = "in"
_ORGANIZATION_ROUTES = {"company", "school", "showcase"}


def _usable(value: str) -> str | None:
    """The reference in the form the URL builders expect, or ``None``.

    Shared by the URL branch and the bare branch on purpose. A segment lifted out
    of a link gets exactly the judgement a bare argument gets, so a second
    encoding layer inside a real profile URL cannot slip past on the way through.
    """
    if "%" in value:
        if _STRAY_PERCENT.search(value):
            return None
        try:
            value = unquote(value, errors="strict")
        except UnicodeDecodeError:
            return None
        # A decoded reference never contains a percent. One that still does
        # carries another encoding layer, which is how `%252e%252e` would reach
        # `..` on a later pass through this same function.
        if "%" in value:
            return None
    value = value.strip()
    if not value or value in _DOT_SEGMENTS:
        return None
    return None if _UNUSABLE.search(value) else value


def _parse_linkedin_url(value: str, *, want: str) -> tuple[str, str | None] | None:
    """``(route, reference)`` for a LinkedIn web URL, or ``None`` if it is not one.

    Lenient on purpose: the input is whatever someone copied out of a browser, so
    a missing scheme (``linkedin.com/in/x``), ``http``, any subdomain, tracking
    query, hash, trailing slash and profile sub-pages
    (``/in/x/recent-activity/all/``) all parse. The mobile-web-lite wrappers are
    unwrapped first. Case is preserved, because a share link from the app can
    carry a case-sensitive profile id where the public identifier normally sits.

    Args:
        want: what the caller is looking for, named in the short-link message so
            a company tool does not send the model after a personal profile.

    Raises:
        InvalidReferenceError: for an ``lnkd.in`` short link, which is a LinkedIn
            URL that only a redirect resolves.
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
        raise InvalidReferenceError(
            "That is a shortened LinkedIn link, and only a redirect resolves it. "
            f"Open it and pass the {want} the address it lands on contains."
        )
    if not _LINKEDIN_HOST.match(host):
        return None

    segments = [segment for segment in url.path.split("/") if segment]
    if segments and segments[0].lower() == "mwlite":
        segments.pop(0)
        if segments and segments[0].lower() == "profile":
            segments.pop(0)

    route = segments[0].lower() if segments else ""
    return route, _usable(segments[1]) if len(segments) > 1 else None


def normalize_person_identifier(value: str) -> str:
    """The public identifier for a person, from a link or from the identifier.

    Idempotent, so applying it twice along a call chain is harmless.

    Raises:
        InvalidReferenceError: when the value cannot name a person, with the
            correction to make. Refusing costs nothing; letting it through spends
            a navigation and returns another page's text as a profile.
    """
    value = value.strip()
    if not value:
        raise InvalidReferenceError(
            "Missing linkedin_username (the /in/ public identifier of the person)."
        )

    parsed = _parse_linkedin_url(value, want="/in/ public identifier")
    if parsed is not None:
        route, reference = parsed
        if route != _PERSON_ROUTE or reference is None:
            raise InvalidReferenceError(
                "That is a LinkedIn link but not a personal profile. Pass the "
                '/in/ public identifier of a person, for example "williamhgates".'
            )
    else:
        reference = _usable(value)
        if reference is None:
            raise InvalidReferenceError(
                "That is not a LinkedIn public identifier. Pass the part after "
                '/in/ in a profile URL, for example "williamhgates".'
            )

    if reference.lower() in _RESERVED:
        raise InvalidReferenceError(
            f'"{reference}" is LinkedIn\'s alias for the signed-in member, not a '
            "person you can look up. Call get_my_profile for the authenticated "
            "user, or pass someone's own /in/ public identifier."
        )
    return reference


def normalize_company_identifier(value: str) -> str:
    """The page slug for an organization, from a link or from the slug itself.

    Idempotent, and raises the same way :func:`normalize_person_identifier` does.
    """
    value = value.strip()
    if not value:
        raise InvalidReferenceError(
            "Missing company_name (the /company/ slug of the organization)."
        )

    parsed = _parse_linkedin_url(value, want="/company/ slug")
    if parsed is not None:
        route, reference = parsed
        if route not in _ORGANIZATION_ROUTES or reference is None:
            raise InvalidReferenceError(
                "That is a LinkedIn link but not a company page. Pass the "
                '/company/ slug, for example "microsoft".'
            )
        return reference

    reference = _usable(value)
    if reference is None:
        raise InvalidReferenceError(
            "That is not a LinkedIn company slug. Pass the part after /company/ "
            'in a company URL, for example "microsoft".'
        )
    return reference


def normalize_opaque_id(value: str, *, field: str) -> str:
    """An id LinkedIn issued (a job id, a conversation thread id), checked as one.

    These are never links and never carry a reserved meaning, so only the syntax
    rules apply. They earn the same treatment as a username because they reach
    the same place: ``job_id="../../feed"`` builds ``/jobs/view/../../feed/``,
    which a browser resolves to ``/feed/`` before it asks for anything.
    """
    reference = _usable(value.strip())
    if reference is None:
        raise InvalidReferenceError(
            f"{field} is not a LinkedIn id. Pass the id exactly as a previous "
            "result returned it, with no URL, path or query around it."
        )
    return reference


def person_profile_url(identifier: str, suffix: str = "") -> str:
    """Profile URL for an already-normalized identifier, escaped as one segment.

    ``safe=""`` is the point: an identifier may not contribute path syntax, and a
    non-ASCII public identifier has to be percent-encoded to survive navigation.
    """
    return f"https://www.linkedin.com/in/{quote(identifier, safe='')}{suffix}"


def company_page_url(identifier: str, suffix: str = "") -> str:
    """Company page URL for an already-normalized slug, escaped as one segment."""
    return f"https://www.linkedin.com/company/{quote(identifier, safe='')}{suffix}"


def job_view_url(job_id: str, suffix: str = "") -> str:
    """Job posting URL for an already-normalized id, escaped as one segment."""
    return f"https://www.linkedin.com/jobs/view/{quote(job_id, safe='')}{suffix}"


def messaging_thread_url(thread_id: str, suffix: str = "") -> str:
    """Conversation URL for an already-normalized thread id, escaped as one segment."""
    return (
        f"https://www.linkedin.com/messaging/thread/{quote(thread_id, safe='')}{suffix}"
    )
