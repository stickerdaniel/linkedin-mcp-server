"""Feed permalink recognition across DOM anchors and SDUI payloads."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import re

from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    build_references,
    dedupe_references,
)

_FEED_RSC_MARKER = "sduiid=com.linkedin.sdui.pagers.feed.mainFeed"
# Matches a LinkedIn post permalink in either plain or JSON-escaped form
# (the initial /feed/ HTML embeds the RSC flight data with \u002f for slashes,
# while paginated responses use plain slashes). Captures the slug portion so
# we can rebuild a canonical URL regardless of the source encoding.
POST_SLUG_URL_RE = re.compile(
    r"linkedin\.com(?:\\u002[fF]|/)posts(?:\\u002[fF]|/)"
    r"(?P<slug>[A-Za-z0-9_-]+?-(?:ugcPost|activity|share)-\d+-[A-Za-z0-9_-]+)"
)
_FEED_DOCUMENT_URLS = {
    "https://www.linkedin.com/feed",
    "https://www.linkedin.com/feed/",
}
# Post entity URNs that build a canonical /feed/update/<urn>/ permalink.
# Only these three URN types resolve as post permalinks; comment, reaction
# and profile URNs are deliberately not matched. The id length floor of 10
# keeps short fixture-style numbers out of live captures while covering
# every real LinkedIn entity id (all are 12+ digits since the migration).
_POST_ENTITY_URN_RE = re.compile(
    r"urn:li:(?P<kind>ugcPost|share|activity):(?P<id>\d{10,})"
)
_PERMALINK_LINKEDIN_HOSTS = {"www.linkedin.com", "linkedin.com"}
# Same json family PR #894 measured on posts-listing pages, plus the document
# response (the initial HTML embeds the first results batch escaped with
# \u002f, which POST_SLUG_URL_RE already reads). Binary media never carries
# permalinks, so it is skipped by not being listed here.
_PERMALINK_MEDIA_TYPES = {
    "text/html",
    "application/json",
    "application/vnd.linkedin.normalized+json",
}
# Mirrors the 50-entry ceiling of build_feed_references: the number of post
# permalinks a caller can be asked for in one listing capture.
_PERMALINK_APPEND_CAP = 50


def is_permalink_payload_response(url: str, content_type: str) -> bool:
    """True for LinkedIn payload responses that can carry post permalinks."""
    parsed = urlparse(url)
    if parsed.netloc.lower() not in _PERMALINK_LINKEDIN_HOSTS:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    if media in _PERMALINK_MEDIA_TYPES:
        return True
    # Structured-suffix spellings of the normalized family, e.g. the
    # ``...+json+2.1`` measured on the voyager responses (PR #894).
    return media.startswith("application/vnd.linkedin.normalized+json")


def permalink_paths_from_payload(text: str) -> list[str]:
    """Extract relative post permalink paths from one payload body.

    Returns ``/posts/<slug>`` paths from slug-URL fields and
    ``/feed/update/<urn>/`` paths from post entity URNs, first-seen order,
    deduplicated per form. The two forms of one post do not collapse here
    (``build_feed_references`` documents the same polymorphic contract).
    """
    paths: list[str] = []
    seen: set[str] = set()
    for match in POST_SLUG_URL_RE.finditer(text):
        path = f"/posts/{match.group('slug')}"
        if path not in seen:
            seen.add(path)
            paths.append(path)
    for match in _POST_ENTITY_URN_RE.finditer(text):
        path = f"/feed/update/urn:li:{match.group('kind')}:{match.group('id')}/"
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def append_permalink_references(
    dom_references: list[Reference],
    captured_paths: list[str],
    *,
    context: str,
) -> list[Reference]:
    """Append captured permalinks after the DOM references of one section.

    DOM references keep their existing per-section cap and are never
    displaced — that is the #817 lesson (captured permalinks must not
    compete with what a caller already receives). Captured paths already
    present as DOM URLs are skipped on the exact string; the two URL
    shapes of one post do not collapse, as in ``build_feed_references``.
    """
    existing = {ref["url"] for ref in dom_references}
    appended: list[Reference] = []
    appended_urls: set[str] = set()
    for path in captured_paths:
        if len(appended) >= _PERMALINK_APPEND_CAP:
            break
        if path in existing or path in appended_urls:
            continue
        appended_urls.add(path)
        appended.append({"kind": "feed_post", "url": path, "context": context})
    return [*dom_references, *appended]


def is_feed_payload_response(url: str) -> bool:
    """True if the response URL is one that carries `postSlugUrl` fields."""
    if _FEED_RSC_MARKER in url:
        return True
    return url.split("?", 1)[0] in _FEED_DOCUMENT_URLS


def build_feed_references(
    raw_references: list[Any],
    captured_urls: list[str],
) -> list[Reference]:
    """Compose feed references from DOM anchors + SDUI captures.

    The feed page renders many anchors that are not post permalinks:
    sidebar widgets, profile cards, employer logos, etc. Mixing them
    into ``references["feed"]`` blurs the contract and competes with
    SDUI permalinks for the per-section cap. We keep only the
    ``feed_post`` slice from the DOM:

    - DOM anchors → ``feed_post`` entries with ``/feed/update/<urn>/``
      URLs (whatever ``classify_link`` recognises).
    - SDUI captures → ``feed_post`` entries with ``/posts/<slug>`` URLs
      for permalinks that the DOM does not surface as an anchor.

    Both are deduped on exact URL string. The two shapes pointing at
    the same underlying post will *not* collapse — ``dedupe_references``
    matches strings, not URNs. Both are valid LinkedIn permalinks, so
    consumers should treat ``feed_post`` as polymorphic on URL form;
    URN-based equivalence is left to the consumer.
    """
    refs = [
        ref
        for ref in build_references(raw_references, "feed")
        if ref["kind"] == "feed_post"
    ]
    existing = {r["url"] for r in refs}
    for sdui_url in captured_urls:
        # AGENTS.md mandates relative paths for LinkedIn references.
        # The SDUI capture carries fully-qualified URLs like
        # https://www.linkedin.com/posts/<slug>; strip the host so the
        # relative-path convention holds. ``classify_link`` does not
        # currently route ``/posts/<slug>`` paths to any kind, so we
        # bypass it for this fallback append.
        parsed = urlparse(sdui_url)
        if not parsed.path.startswith("/posts/"):
            continue
        relative = parsed.path
        if relative in existing:
            continue
        refs.append({"kind": "feed_post", "url": relative, "context": "feed"})
        existing.add(relative)
    # Cap kept in sync with _REFERENCE_CAPS["feed"] in link_metadata.py;
    # changing one without the other will drop or duplicate entries
    # silently. Matches get_feed's num_posts ceiling (Field(ge=1, le=50)).
    return dedupe_references(refs, cap=50)
