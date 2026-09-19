"""Locale-guarded cleanup of LinkedIn innerText captures."""

from __future__ import annotations

from dataclasses import dataclass

import re


@dataclass(frozen=True)
class DetailCaptureTextTable:
    """Visible-text policy for hydrating and expanding profile details."""

    readiness_blocking_prefixes: tuple[str, ...]
    expansion_button_pattern: re.Pattern[str]

    def readiness_expression(self) -> str:
        """Build the historical readiness predicate without changing its bytes."""
        conditions = "\n                            && ".join(
            f"!text.startsWith({prefix!r})"
            for prefix in self.readiness_blocking_prefixes
        )
        return (
            "() => {\n"
            "                        const main = document.querySelector('main');\n"
            "                        if (!main) return false;\n"
            "                        const text = main.innerText.trimStart();\n"
            f"                        return {conditions};\n"
            "                    }"
        )


_DETAIL_CAPTURE_TEXT: dict[str, DetailCaptureTextTable] = {
    "en-US": DetailCaptureTextTable(
        readiness_blocking_prefixes=(
            "Load more",
            "More profiles for you",
            "Explore premium profiles",
        ),
        expansion_button_pattern=re.compile(
            r"^Show (more|all)\b",
            re.IGNORECASE,
        ),
    ),
}

# BrowserManager forces the browser context to en-US (core/browser.py), so the
# capture owner receives this exact entry. Unsupported locales are deliberately
# not inferred from language prefixes or detected from page text.
DETAIL_CAPTURE_EN_US = _DETAIL_CAPTURE_TEXT["en-US"]

# Patterns that mark the start of LinkedIn page chrome (sidebar/footer).
# Everything from the earliest match onwards is stripped.
_NOISE_MARKERS: list[re.Pattern[str]] = [
    # Footer nav links: "About" immediately followed by "Accessibility" or "Talent Solutions"
    re.compile(r"^About\n+(?:Accessibility|Talent Solutions)", re.MULTILINE),
    # Sidebar profile recommendations
    re.compile(r"^More profiles for you$", re.MULTILINE),
    # Sidebar premium upsell
    re.compile(r"^Explore premium profiles$", re.MULTILINE),
    # InMail upsell in contact info overlay
    re.compile(r"^Get up to .+ replies when you message with InMail$", re.MULTILINE),
    # Footer nav clusters in profile/posts pages
    re.compile(
        r"^(?:Careers|Privacy & Terms|Questions\?|Select language)\n+"
        r"(?:Privacy & Terms|Questions\?|Select language|Advertising|Ad Choices|"
        r"[A-Za-z]+ \([A-Za-z]+\))",
        re.MULTILINE,
    ),
]

_NOISE_LINES: list[re.Pattern[str]] = [
    re.compile(r"^(?:Play|Pause|Playback speed|Turn fullscreen on|Fullscreen)$"),
    re.compile(r"^(?:Show captions|Close modal window|Media player modal window)$"),
    re.compile(r"^(?:Loaded:.*|Remaining time.*|Stream Type.*)$"),
]


def strip_linkedin_noise(text: str) -> str:
    """Remove LinkedIn page chrome (footer, sidebar recommendations) from innerText.

    Finds the earliest occurrence of any known noise marker and truncates there.
    """
    cleaned = truncate_linkedin_noise(text)
    return filter_linkedin_noise_lines(cleaned)


def filter_linkedin_noise_lines(text: str) -> str:
    """Remove known media/control noise lines from already-truncated content."""
    filtered_lines = [
        line
        for line in text.splitlines()
        if not any(pattern.match(line.strip()) for pattern in _NOISE_LINES)
    ]
    return "\n".join(filtered_lines).strip()


def truncate_linkedin_noise(text: str) -> str:
    """Trim known LinkedIn chrome blocks before any per-line noise filtering."""
    earliest = len(text)
    for pattern in _NOISE_MARKERS:
        match = pattern.search(text)
        if match and match.start() < earliest:
            earliest = match.start()

    return text[:earliest].strip()


# Messaging-page chrome around an opened conversation thread. innerText on
# /messaging/thread/ pages carries no URL or attribute signal separating the
# inbox sidebar from the thread, so the boundaries are matched on visible
# strings — guarded by an explicit per-locale table (CLAUDE.md → Scraping
# Rules). BrowserManager forces the context locale to en-US (core/browser.py),
# so the "en" entry is the operative one; a locale without a table entry
# passes through unstripped.
@dataclass(frozen=True)
class _MessagingChromeTable:
    # Sidebar pagination control; the last line of the inbox sidebar. Pins
    # the thread header so quoted UI text inside messages can't move the
    # start boundary.
    sidebar_end: str
    # Screen-reader label on the options dropdown; appears once per sidebar
    # entry and once in the opened thread's header. The thread's own line is
    # the first occurrence after ``sidebar_end``.
    thread_header_prefix: str
    # First control of the trailing message-composer block.
    composer_start: str
    # Standalone controls of the composer block, matched exactly. At least
    # one must follow a ``composer_start`` candidate to confirm it is the
    # real composer rather than a message quoting the label. Controls whose
    # text embeds the participant name (the Attach lines) are deliberately
    # excluded: they would need prefix matching, and any prefix match lets
    # quoted control text with a suffix confirm a false boundary.
    composer_companions: tuple[str, ...]


# How far below a composer-label candidate a companion control may sit and
# still count as the same block. The observed block spans 6 lines; the slack
# covers extra controls LinkedIn injects (e.g. "Press Enter to Send").
_COMPOSER_COMPANION_WINDOW = 8

_MESSAGING_CHROME_STRINGS: dict[str, _MessagingChromeTable] = {
    "en": _MessagingChromeTable(
        sidebar_end="Load more conversations",
        thread_header_prefix="Open the options list in your conversation with",
        composer_start="Maximize compose field",
        composer_companions=(
            "Open GIF Keyboard",
            "Open Emoji Keyboard",
            "Open send options",
        ),
    ),
}


def strip_conversation_chrome(text: str, locale: str = "en") -> str:
    """Trim messaging chrome around an opened conversation thread.

    A conversation page's innerText embeds the thread between three chrome
    blocks: the messaging header, the inbox sidebar (which previews *other*
    conversations), and the trailing message composer. Drops everything
    through the thread-header line and everything from the composer onward.
    Each boundary independently falls back to keeping the text when its
    marker is absent (unknown locale, layout change), so a failed match
    leaks chrome rather than dropping messages.
    """
    table = _MESSAGING_CHROME_STRINGS.get(locale)
    if table is None:
        return text

    lines = text.splitlines()

    # End boundary: the last composer-label line, accepted only when an
    # exact companion control follows within the next few lines. The real
    # composer block is contiguous (label + controls observed within 6
    # lines), so a nearby companion confirms chrome, while a message that
    # quotes the label — or control text with any suffix — falls through to
    # the missing-marker fallback. A verbatim multi-line reproduction of the
    # block inside a message remains indistinguishable from the block itself;
    # that ambiguity is inherent to text-only stripping.
    end = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() != table.composer_start:
            continue
        if any(
            lines[j].strip() in table.composer_companions
            for j in range(i + 1, min(i + 1 + _COMPOSER_COMPANION_WINDOW, len(lines)))
        ):
            end = i
        break

    # Start boundary: the sidebar's pagination line, when present, pins the
    # real thread header as the first options line after it; quoted UI text
    # inside messages can no longer pull the boundary into the thread. The
    # sidebar omits the pagination control when there are few conversations —
    # then fall back to the last options line before the composer.
    start = 0
    sidebar_end = next(
        (i for i in range(end) if lines[i].strip() == table.sidebar_end), None
    )
    if sidebar_end is not None:
        header = next(
            (
                i
                for i in range(sidebar_end + 1, end)
                if lines[i].strip().startswith(table.thread_header_prefix)
            ),
            None,
        )
        start = (header + 1) if header is not None else sidebar_end + 1
    else:
        for i in range(end - 1, -1, -1):
            if lines[i].strip().startswith(table.thread_header_prefix):
                start = i + 1
                break

    return "\n".join(lines[start:end]).strip()


# Sidebar recommendation headings on a person page, and the control that opens
# the full list behind one. Neither carries a URL, an attribute or a structural
# count separating it from any other heading or anchor in the same container,
# so both are matched on visible strings — guarded by an explicit per-locale
# table (CLAUDE.md → Scraping Rules) exactly like the messaging chrome above.
# This is the only place the strings are written down; `person.py` builds its
# extraction program from this table rather than repeating them.
@dataclass(frozen=True)
class SidebarChromeTable:
    # Headings of the recommendation sections worth collecting, matched whole
    # against a normalized `h1`/`h2`/`h3`. A heading outside the table is left
    # alone rather than guessed at.
    section_headings: tuple[str, ...]
    # Prefixes of the anchor that expands a section to its full list, matched
    # against lowercased anchor text. LinkedIn labels that control either way
    # depending on the surface, so both spellings are listed.
    show_all_prefixes: tuple[str, ...]


_SIDEBAR_CHROME_STRINGS: dict[str, SidebarChromeTable] = {
    "en": SidebarChromeTable(
        section_headings=(
            "More profiles for you",
            "Explore premium profiles",
            "People you may know",
        ),
        show_all_prefixes=("show all", "see all"),
    ),
}

# BrowserManager forces the context locale to en-US (core/browser.py), so this
# is the entry a running server reads, and the dictionary above is what makes
# that dependency visible instead of implicit. A locale with no entry would
# collect nothing here, which is why the sidebar is the one workflow whose
# coverage has to be stated per locale rather than assumed.
SIDEBAR_CHROME_EN = _SIDEBAR_CHROME_STRINGS["en"]


@dataclass(frozen=True)
class JobSearchTextTable:
    """Visible-text policy for reading a job search results page."""

    # Headings LinkedIn puts over unrelated postings when a search matches
    # nothing. That page keeps the search's route and query and its cards are
    # ordinary job links, so the heading is the only thing separating it from
    # a result page.
    no_match_headings: tuple[str, ...]
    # The result count, matched as a whole line with named groups `count` and
    # `plus`. It sits in a bare element with no attribute to find it by.
    result_count_pattern: re.Pattern[str]
    # A sponsored card's own line. The detail pane says "Promoted by hirer",
    # which a whole-line match leaves alone.
    promoted_label: str

    def shows_no_match(self, text: str) -> bool:
        """Whether a search page's text opens with a no-match heading.

        The first line only: a real result page opens with "<keywords> in
        <location>", and a posting further down could carry the same words.
        """
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return first in self.no_match_headings

    def result_count(self, text: str) -> tuple[int, bool] | None:
        """The advertised number of results, and whether it is exact.

        Only the first lines are searched, where both layouts print it: under
        the heading on the classic page, first on the redesigned one. "500+"
        is a lower bound, not a count.
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines[:_RESULT_COUNT_LINES]:
            match = self.result_count_pattern.fullmatch(line)
            if match:
                count = int(match.group("count").replace(",", ""))
                return count, match.group("plus") is None
        return None


_RESULT_COUNT_LINES = 3

_JOB_SEARCH_TEXT: dict[str, JobSearchTextTable] = {
    "en-US": JobSearchTextTable(
        no_match_headings=("Jobs you may be interested in",),
        result_count_pattern=re.compile(
            r"(?P<count>[0-9]{1,3}(?:,[0-9]{3})*)(?P<plus>\+)? results?"
        ),
        promoted_label="Promoted",
    ),
}

# Same locale contract as `DETAIL_CAPTURE_EN_US`. A heading the table does not
# know reads as a result page, which is how every search was read before.
JOB_SEARCH_EN_US = _JOB_SEARCH_TEXT["en-US"]
