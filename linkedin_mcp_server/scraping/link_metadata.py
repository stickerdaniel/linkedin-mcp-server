"""Helpers for extracting compact, typed references from LinkedIn DOM links."""

from __future__ import annotations

import re
from typing import Literal, NotRequired, Required, TypedDict
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

ReferenceKind = Literal[
    "person",
    "company",
    "job",
    "feed_post",
    "article",
    "newsletter",
    "school",
    "external",
]


class Reference(TypedDict):
    """Compact reference payload returned to MCP clients."""

    kind: Required[ReferenceKind]
    url: Required[str]
    text: NotRequired[str]
    context: NotRequired[str]


class RawReference(TypedDict, total=False):
    """Raw anchor data collected from the browser DOM."""

    href: str
    text: str
    aria_label: str
    title: str
    heading: str
    in_article: bool
    in_nav: bool
    in_footer: bool


_GENERIC_LABELS = {
    "show all",
    "follow",
    "following",
    "connect",
    "send",
    "like",
    "comment",
    "repost",
    "post",
    "play",
    "pause",
    "fullscreen",
    "close",
    "manage notifications",
    "view my newsletter",
    "my newsletter",
}

_CONTEXT_LABELS = {
    "about",
    "experience",
    "education",
    "interests",
    "honors",
    "languages",
    "featured",
    "contact info",
}

_SECTION_CONTEXTS = {
    "experience": "experience",
    "education": "education",
    "interests": "interests",
    "honors": "honors",
    "languages": "languages",
    "contact_info": "contact info",
    "job_posting": "job result",
}

_DEFAULT_REFERENCE_CAP = 12
_REFERENCE_CAPS = {
    "main_profile": 12,
    "about": 12,
    "experience": 12,
    "education": 12,
    "interests": 12,
    "honors": 12,
    "languages": 12,
    "posts": 12,
    "jobs": 8,
    "search_results": 15,
    "job_posting": 8,
    "contact_info": 8,
}

_URL_LIKE_RE = re.compile(r"^(?:https?://|/)\S+$", re.IGNORECASE)
_DUPLICATE_HALVES_RE = re.compile(r"^(?P<value>.+?)\s+(?P=value)$")
_WHITESPACE_RE = re.compile(r"\s+")
_CONNECTIONS_FOLLOW_RE = re.compile(r"\bconnections follow this page\b", re.IGNORECASE)
_COMPANY_PATH_RE = re.compile(r"^/company/([^/?#]+)")
_PERSON_PATH_RE = re.compile(r"^/in/([^/?#]+)")
_SCHOOL_PATH_RE = re.compile(r"^/school/([^/?#]+)")
_JOB_PATH_RE = re.compile(r"^/jobs/view/(\d+)")
_NEWSLETTER_PATH_RE = re.compile(r"^/newsletters/([^/?#]+)")
_PULSE_PATH_RE = re.compile(r"^/pulse/([^/?#]+)")
_FEED_PATH_RE = re.compile(r"^/feed/update/([^/?#]+)")
_MAX_REDIRECT_UNWRAP_DEPTH = 5


def build_references(
    raw_references: list[RawReference],
    section_name: str,
) -> list[Reference]:
    """Filter and normalize raw DOM anchors into compact references."""
    cap = _REFERENCE_CAPS.get(section_name, _DEFAULT_REFERENCE_CAP)
    normalized_references: list[Reference] = []

    for raw in raw_references:
        normalized = normalize_reference(raw, section_name)
        if normalized is None:
            continue
        normalized_references.append(normalized)

    return dedupe_references(normalized_references, cap=cap)


def normalize_reference(
    raw: RawReference,
    section_name: str,
) -> Reference | None:
    """Normalize one raw DOM anchor into a compact reference."""
    if raw.get("in_nav") or raw.get("in_footer"):
        return None

    href = normalize_url(raw.get("href", ""))
    if href is None:
        return None

    kind_url = classify_link(href)
    if kind_url is None:
        return None
    kind, normalized_url = kind_url

    text = choose_reference_text(raw, kind)
    if text is None and kind not in {"feed_post", "external"}:
        return None

    context = derive_context(section_name, raw, kind)

    reference: Reference = {
        "kind": kind,
        "url": normalized_url,
    }
    if text:
        reference["text"] = text
    if context:
        reference["context"] = context
    return reference


def normalize_url(href: str, _depth: int = 0) -> str | None:
    """Normalize a raw href and unwrap LinkedIn redirect URLs."""
    if _depth > _MAX_REDIRECT_UNWRAP_DEPTH:
        return None

    href = href.strip()
    if not href or href.startswith("#"):
        return None

    parsed = urlparse(href)
    scheme = parsed.scheme.lower()
    if scheme in {"blob", "javascript", "mailto", "tel"}:
        return None
    if scheme and scheme not in {"http", "https"}:
        return None

    host = parsed.netloc.lower()
    if _is_linkedin_host(host) and parsed.path == "/redir/redirect/":
        target = unquote((parse_qs(parsed.query).get("url") or [""])[0]).strip()
        if not target:
            return None
        return normalize_url(target, _depth + 1)

    if not parsed.scheme:
        return None

    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def classify_link(href: str) -> tuple[ReferenceKind, str] | None:
    """Classify and canonicalize one normalized URL."""
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    path = parsed.path or "/"

    if not _is_linkedin_host(host):
        return "external", urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path or "/", "", "", "")
        )

    if _is_linkedin_chrome(path):
        return None

    if match := _PERSON_PATH_RE.match(path):
        person_suffix = path[match.end() :].lstrip("/")
        first_suffix_segment = person_suffix.split("/", 1)[0] if person_suffix else ""
        if first_suffix_segment in {"overlay", "details", "recent-activity"}:
            return None
        return "person", f"/in/{match.group(1)}/"

    if match := _COMPANY_PATH_RE.match(path):
        return "company", f"/company/{match.group(1)}/"

    if match := _SCHOOL_PATH_RE.match(path):
        return "school", f"/school/{match.group(1)}/"

    if match := _JOB_PATH_RE.match(path):
        return "job", f"/jobs/view/{match.group(1)}/"

    if match := _NEWSLETTER_PATH_RE.match(path):
        return "newsletter", f"/newsletters/{match.group(1)}/"

    if match := _PULSE_PATH_RE.match(path):
        return "article", f"/pulse/{match.group(1)}/"

    if match := _FEED_PATH_RE.match(path):
        return "feed_post", f"/feed/update/{match.group(1)}/"

    return None


def choose_reference_text(
    raw: RawReference,
    kind: ReferenceKind,
) -> str | None:
    """Choose the best compact human-readable label for a reference."""
    candidates: list[tuple[int, str]] = []
    for priority, candidate in enumerate(
        (
            raw.get("text", ""),
            raw.get("aria_label", ""),
            raw.get("title", ""),
        )
    ):
        cleaned = clean_label(candidate, kind)
        if cleaned:
            candidates.append((priority, cleaned))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (_label_sort_key(item[1]), item[0]))
    return candidates[0][1]


def clean_label(value: str, kind: ReferenceKind) -> str | None:
    """Normalize and compact a candidate label."""
    value = _WHITESPACE_RE.sub(" ", value).strip()
    if not value:
        return None

    value = re.sub(
        r"^(?:View:\s*|View\b\s+|Open article:\s*)",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"[’']s\s+graphic link$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+graphic link$", "", value, flags=re.IGNORECASE)
    value = value.strip(" :-")

    if " by " in value and kind in {"article", "external"}:
        value = value.split(" by ", 1)[0].strip()

    for separator in (" • ", " · ", " | "):
        if separator in value:
            value = value.split(separator, 1)[0].strip()

    duplicate_match = _DUPLICATE_HALVES_RE.match(value)
    if duplicate_match:
        value = duplicate_match.group("value").strip()

    if _URL_LIKE_RE.match(value):
        return None
    if _CONNECTIONS_FOLLOW_RE.search(value):
        return None
    if value.lower() in _GENERIC_LABELS:
        return None
    if len(value) < 2:
        return None
    if len(value) > 80:
        return None
    if not re.search(r"[A-Za-z0-9]", value):
        return None

    return value


def derive_context(
    section_name: str,
    raw: RawReference,
    kind: ReferenceKind,
) -> str | None:
    """Build a compact context hint for one retained reference."""
    if section_name in _SECTION_CONTEXTS:
        return _SECTION_CONTEXTS[section_name]

    heading = clean_heading(raw.get("heading", ""))

    if section_name == "search_results":
        return "job result" if kind == "job" else "search result"

    if section_name == "posts":
        if kind == "person":
            return "post author"
        if kind == "feed_post":
            return "company post"
        return "post attachment"

    if section_name in {"main_profile", "about"}:
        if heading in _CONTEXT_LABELS:
            return heading
        if raw.get("in_article"):
            return "featured"
        return "top card"

    return heading if heading in _CONTEXT_LABELS else None


def clean_heading(value: str) -> str | None:
    """Normalize a raw heading into a short supported context label."""
    value = _WHITESPACE_RE.sub(" ", value).strip().lower()
    if not value:
        return None
    return value if value in _CONTEXT_LABELS else None


def _choose_better_reference(existing: Reference, new: Reference) -> Reference:
    """Keep the cleaner, richer of two duplicate-url references."""
    existing_score = _reference_score(existing)
    new_score = _reference_score(new)
    return new if new_score > existing_score else existing


def dedupe_references(
    references: list[Reference],
    cap: int | None = None,
) -> list[Reference]:
    """Dedupe references by URL while keeping the cleaner duplicate in order."""
    deduped: dict[str, Reference] = {}
    ordered_urls: list[str] = []

    for reference in references:
        url = reference["url"]
        existing = deduped.get(url)
        if existing is None:
            deduped[url] = reference
            ordered_urls.append(url)
            continue
        deduped[url] = _choose_better_reference(existing, reference)

    ordered = [deduped[url] for url in ordered_urls]
    return ordered[:cap] if cap is not None else ordered


def _reference_score(reference: Reference) -> tuple[int, int, int | float]:
    text = reference.get("text")
    context = reference.get("context")
    return (
        1 if text else 0,
        1 if context else 0,
        _text_score(text),
    )


def _label_sort_key(label: str) -> tuple[int, int]:
    """Prefer concise labels, but deprioritize short 2-character strings."""
    return (1 if len(label) < 3 else 0, len(label))


def _text_score(text: str | None) -> int | float:
    """Prefer richer labels while scoring missing text as strictly worst."""
    return len(text) if text else float("-inf")


def _is_linkedin_chrome(path: str) -> bool:
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = f"/{path}"

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return False

    first = segments[0]
    second = segments[1] if len(segments) > 1 else ""

    if first in {
        "help",
        "legal",
        "about",
        "accessibility",
        "mypreferences",
        "preferences",
    }:
        return True
    if first == "search" and second == "results":
        return True
    if first == "overlay" and second in {
        "background-photo",
        "browsemap-recommendations",
    }:
        return True
    return first == "preload" and second == "custom-invite"


def _is_linkedin_host(host: str) -> bool:
    return host == "linkedin.com" or host.endswith(".linkedin.com")
