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
    "normalize_job_id",
    "normalize_opaque_id",
    "normalize_person_identifier",
    "normalize_thread_id",
    "person_profile_url",
]

# linkedin.com and every host under it. There is no canonical host to normalize
# toward, because the locale subdomain serves the profile itself.
_LINKEDIN_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")

# LinkedIn's own shortener. Only a redirect resolves one, which nothing here can
# follow, so it gets its own message instead of a repair attempt.
_SHORTENER_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*lnkd\.in$")

_HAS_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# Path, query and whitespace syntax that no reference carries.
_UNUSABLE = re.compile(r"[\s/\\?#]|[\x00-\x1f\x7f]")

# Everything a public identifier or a page slug may consist of, as an allowlist
# rather than a list of forbidden characters. A blacklist keeps losing to inputs
# nobody enumerated: `foo@example.com` carries no path syntax, so it passed and
# spent a page load on a 404 that this module exists to avoid.
_IDENTIFIER = re.compile(r"^[\w-]+$")

# Characters that have to be judged in the argument itself, before it becomes a
# URL, because the parse below does not see them the way a browser does.
#
# Parsing strips tab, newline and carriage return out of a URL silently, so
# `/in/foo\nbar/` would arrive as `foobar` and be accepted as a different person
# than the caller named.
#
# A backslash is a path separator in the URL Standard for http(s), and
# `urlparse` is not: it leaves the backslash inside one segment, so splitting on
# `/` alone reads a path the browser never navigates. Measured,
# `/in/alice/x\..\..\..\in\bob` reads as `alice` here while a conforming
# parser resolves it to `/in/bob`, which is the retargeting the dot-segment rule
# below exists to stop. No supported identifier contains one, so it is refused
# outright rather than translated.
_RAW_REFUSED = re.compile(r"[\x00-\x1f\x7f\\]")

# A `%` that begins no valid escape. LinkedIn references contain no literal one,
# so this is always a malformed escape rather than content.
_STRAY_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")

# The port each scheme reaches without being told, so an explicitly written one
# can be told apart from one that redirects the request somewhere else.
_DEFAULT_PORTS = {"http": 80, "https": 443}

# The two segments a browser resolves away. `quote` cannot help here: a period is
# unreserved, so it survives escaping and `/in/../` still walks up a level.
_DOT_SEGMENTS = {".", ".."}

# `me` is what LinkedIn resolves to the signed-in member. get_my_profile is the
# tool for that; reaching it through a person lookup answers about the wrong one.
_RESERVED = {"me"}

# Routes that name a person and routes that name an organization.
#
# `/school/` and `/showcase/` were accepted here and are not any more. Nothing in
# this module can build an address under either one, so their slug was rebuilt
# under `/company/`, which 301-redirects to the organization root. That is right
# for the root and wrong for everything the company scrape appends: measured,
# `/company/<school-slug>/jobs/` redirects to the school root too, and the
# extractor does not check where it landed, so root content was recorded under
# the section the caller asked for. A bare slug still works, because that is the
# caller asserting which route it belongs to.
_PERSON_ROUTE = "in"
_ORGANIZATION_ROUTES = {"company"}

# The routes this repository emits for the ids these tools take. link_metadata
# renders every reference as a site-relative path (``/in/alice/``,
# ``/messaging/thread/2-abc/``) and the tool descriptions send callers back
# through them, so a reference handed straight back is a caller following the
# instructions. Refusing it would refuse this server's own output.
_JOB_ROUTE = ("jobs", "view")
_THREAD_ROUTE = ("messaging", "thread")

# A LinkedIn job id is the number in /jobs/view/<id>. Everything that produces
# one here extracts ``\d+``, and anything else navigates to a 404 that costs a
# page load to discover.
_NUMERIC_ID = re.compile(r"^[0-9]+$")


def _decoded(value: str) -> str | None:
    """The value with at most one layer of percent-encoding removed."""
    if "%" not in value:
        return value
    if _STRAY_PERCENT.search(value):
        return None
    try:
        value = unquote(value, errors="strict")
    except UnicodeDecodeError:
        return None
    # A decoded reference never contains a percent. One that still does carries
    # another encoding layer, which is how `%252e%252e` would reach `..` on a
    # later pass through this same function.
    return None if "%" in value else value


def _is_dot_segment(segment: str) -> bool:
    """Whether a path segment resolves away, in either spelling."""
    return segment in _DOT_SEGMENTS or _decoded(segment) in _DOT_SEGMENTS


def _usable(value: str) -> str | None:
    """The reference in the form the URL builders expect, or ``None``.

    Shared by the URL branch and the bare branch on purpose. A segment lifted out
    of a link gets exactly the judgement a bare argument gets, so a second
    encoding layer inside a real profile URL cannot slip past on the way through.

    Nothing trims after decoding. `%20alice` has to reach the checks below as a
    leading space and be refused, not be tidied into the real `alice`.
    """
    decoded = _decoded(value)
    if decoded is None:
        return None
    value = decoded
    if not value or value in _DOT_SEGMENTS:
        return None
    # A lone surrogate survives JSON parsing and every check above, and then
    # raises inside `quote` while the URL is being built. The caller would see an
    # unexpected tool failure with issue-report diagnostics instead of the
    # correction this module exists to give.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return None if _UNUSABLE.search(value) else value


def _identifier(value: str) -> str | None:
    """A public identifier or page slug, which is narrower than a usable id."""
    usable = _usable(value)
    return usable if usable is not None and _IDENTIFIER.match(usable) else None


def _linkedin_segments(value: str, *, want: str) -> list[str] | None:
    """Path segments of a LinkedIn address, or ``None`` if it is not one.

    Lenient on purpose: the input is whatever someone copied out of a browser or
    read back out of this server's own output, so a missing scheme
    (``linkedin.com/in/x``), a site-relative path (``/in/x/``), ``http``, any
    subdomain, tracking query, hash, trailing slash and profile sub-pages
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
    # Checked before the parse, which reads these differently than a browser
    # does and would hand back a different reference than the one the caller
    # wrote. See _RAW_REFUSED.
    #
    # Only up to the first `?` or `#`: a backslash separates paths and nothing
    # else, so a tracking parameter like `?trk=foo\\bar` leaves the path alone
    # and refusing it would reject an address that works. Everything before that
    # cut is in scope, the authority included, because
    # `https://www.linkedin.com\\evil.example/in/alice` is a path on LinkedIn to
    # a browser and a host swap to anything that splits on `/`.
    if _RAW_REFUSED.search(re.split(r"[?#]", value, maxsplit=1)[0]):
        return None
    if _HAS_SCHEME.match(value):
        candidate = value
    elif value.startswith("//"):
        # A network-path reference. A browser takes the scheme from the page it
        # is on, which for a LinkedIn address is always https.
        candidate = f"https:{value}"
    elif value.startswith("/"):
        candidate = f"https://www.linkedin.com{value}"
    else:
        candidate = f"https://{value}"
    try:
        url = urlparse(candidate)
    except ValueError:
        return None
    if url.scheme not in {"http", "https"}:
        return None
    # A single trailing dot is the fully qualified spelling of the same host, and
    # LinkedIn answers on it, so it is dropped rather than refused. A second dot
    # is not a spelling of anything and falls through to the host match below.
    host = (url.hostname or "").lower().removesuffix(".")
    if _SHORTENER_HOST.match(host):
        raise InvalidReferenceError(
            "That is a shortened LinkedIn link, and only a redirect resolves it. "
            f"Open it and pass the {want} the address it lands on contains."
        )
    if not _LINKEDIN_HOST.match(host):
        return None
    # A port LinkedIn does not answer on is not a formatting quirk. Reading the
    # path and dropping the port turns an address a browser cannot load into a
    # working call the caller never asked for: `linkedin.com:444/in/x` times out
    # in a browser and would have been rebuilt here as the live profile.
    #
    # Judged against the scheme's own default, not against 443 alone.
    # `http://www.linkedin.com:443/in/x` is a browser request to port 443 spoken
    # in cleartext, which answers 400, and taking it for the default would have
    # rebuilt it as the live HTTPS profile.
    try:
        port = url.port
    except ValueError:
        return None
    if port is not None and port != _DEFAULT_PORTS[url.scheme]:
        return None

    raw_segments = url.path.split("/")
    # An interior empty segment is not a formatting quirk: LinkedIn answers 404
    # for `/company//microsoft/`, so dropping it would turn a path that names
    # nothing into one that names the real page.
    if any(segment == "" for segment in raw_segments[1:-1]):
        return None
    segments = [segment for segment in raw_segments if segment]
    # A dot segment anywhere retargets the whole path. A browser resolves
    # `/in/alice/../../in/bob` to `/in/bob`, while reading the route and the
    # segment after it answers `alice`, so the tool would act on the person the
    # reference does not name.
    if any(_is_dot_segment(segment) for segment in segments):
        return None
    if segments and segments[0].lower() == "mwlite":
        segments.pop(0)
        if segments and segments[0].lower() == "profile":
            segments.pop(0)
    return segments


def _parse_linkedin_url(value: str, *, want: str) -> tuple[str, str | None] | None:
    """``(route, reference)`` for a LinkedIn address, or ``None`` if it is not one."""
    segments = _linkedin_segments(value, want=want)
    if segments is None:
        return None
    route = segments[0].lower() if segments else ""
    return route, _identifier(segments[1]) if len(segments) > 1 else None


def _id_after_route(value: str, route: tuple[str, ...], *, want: str) -> str | None:
    """The id following ``route`` in a LinkedIn address, or ``None``."""
    segments = _linkedin_segments(value, want=want)
    if segments is None or len(segments) <= len(route):
        return None
    if [segment.lower() for segment in segments[: len(route)]] != list(route):
        return None
    return _usable(segments[len(route)])


def normalize_person_identifier(value: str, *, allow_self_alias: bool = False) -> str:
    """The public identifier for a person, from a link or from the identifier.

    Idempotent, so applying it twice along a call chain is harmless.

    Args:
        allow_self_alias: let ``me`` through. Only ``get_my_profile`` sets this,
            and only for its own fallback: it navigates to ``/in/me/`` and reads
            the identifier back out of the redirect, so an unresolved redirect
            leaves it holding the alias. Refusing there would answer the tool
            that owns the alias with an instruction to call itself.

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
        reference = _identifier(value)
        if reference is None:
            raise InvalidReferenceError(
                "That is not a LinkedIn public identifier. Pass the part after "
                '/in/ in a profile URL, for example "williamhgates".'
            )

    if not allow_self_alias and reference.lower() in _RESERVED:
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

    reference = _identifier(value)
    if reference is None:
        raise InvalidReferenceError(
            "That is not a LinkedIn company slug. Pass the part after /company/ "
            'in a company URL, for example "microsoft".'
        )
    return reference


def _numeric_tail(segment: str) -> str:
    """The id at the end of a slugged path segment, or the segment unchanged.

    LinkedIn serves a job under both `/jobs/view/1967281839/` and
    `/jobs/view/<title>-at-<company>-1967281839/`, and the slugged form is the
    one a browser address bar holds. Measured, both 301 to the same destination,
    so the trailing run of digits is the id and the words in front of it are
    decoration.

    Only reached for a segment taken out of a URL under the id's own route. A
    bare argument stays strictly numeric, because there the words are not a slug
    LinkedIn wrote, they are a wrong value.
    """
    match = re.fullmatch(r"[\w-]*?-(\d+)", segment)
    return match.group(1) if match else segment


def normalize_opaque_id(
    value: str,
    *,
    field: str,
    route: tuple[str, ...] = (),
    numeric: bool = False,
) -> str:
    """An id LinkedIn issued (a job id, a conversation thread id), checked as one.

    Ids carry no reserved meaning, so mostly the syntax rules apply. They earn
    the same treatment as a username because they reach the same place:
    ``job_id="../../feed"`` builds ``/jobs/view/../../feed/``, which a browser
    resolves to ``/feed/`` before it asks for anything.

    Args:
        route: the path this kind of id sits under, so the reference this server
            prints (``/messaging/thread/2-abc/``) can be handed straight back.
        numeric: reject anything but digits, for the ids LinkedIn issues as a
            number. A word there is a 404 that costs a page load to learn.
    """
    value = value.strip()
    reference = _id_after_route(value, route, want=field) if route else None
    if reference is not None and numeric:
        reference = _numeric_tail(reference)
    if reference is None:
        reference = _usable(value)
    if reference is None or (numeric and not _NUMERIC_ID.match(reference)):
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


def normalize_job_id(value: str) -> str:
    """The numeric id for a job posting, from the id or from a reference to it."""
    return normalize_opaque_id(value, field="job_id", route=_JOB_ROUTE, numeric=True)


def normalize_thread_id(value: str) -> str:
    """The id for a conversation, from the id or from a reference to it."""
    return normalize_opaque_id(value, field="thread_id", route=_THREAD_ROUTE)
