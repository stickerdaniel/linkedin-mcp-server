"""Tests for the paged search walk and row parsing shared by people and
company search."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import logging

import pytest

from linkedin_mcp_server.scraping import search_pages as search_pages_module
from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.search_pages import (
    SearchPages,
    paginate_search,
    search_rows,
)
from linkedin_mcp_server.scraping.search_parse import (
    parse_company_cards,
    parse_people_cards,
)
from linkedin_mcp_server.scraping.session import ScrapingSession

PEOPLE = "https://www.linkedin.com/search/results/people/?keywords=engineer"


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


def _person_card(name: str, headline: str, location: str) -> str:
    return f"{name} • 2nd\n\n{headline}\n\n{location}"


def _person_ref(slug: str, name: str) -> Reference:
    return {"kind": "person", "url": f"/in/{slug}/", "text": name}


def _company_card(name: str, industry: str, location: str, tagline: str) -> str:
    return (
        f"{name}\n\n{industry}\n\n{location}\n\nFollow\n\n{tagline}\n\n"
        f"Ann & 3 other connections follow this page · 20K followers"
    )


def _company_ref(slug: str, name: str) -> Reference:
    return {"kind": "company", "url": f"/company/{slug}/", "text": name}


class TestSearchRowsPeople:
    """``people`` rows come from ``search_parse`` per page, before the join.

    The pages here are synthetic containers in the shape the parser's
    docstring names -- a claim about the wiring, not about LinkedIn; the
    parser's own suite holds the live fixtures.
    """

    # Ada / Bob on page 1, Bob again / Cy / Dee on page 2. Bob is the row a
    # naive concatenation would double, and the last card of page 1, whose
    # location would swallow the ``---`` separator and page 2's header if the
    # pages were joined before parsing. Dee has no anchor.
    PAGE_1 = "About 1,234 results\n\n" + "\n\n".join(
        [
            _person_card("Ada", "Engineer at Example", "London, United Kingdom"),
            _person_card("Bob", "Founder", "Berlin, Germany"),
        ]
    )
    PAGE_2 = "About 999 results\n\n" + "\n\n".join(
        [
            _person_card("Bob", "Founder", "Berlin, Germany"),
            _person_card("Cy", "Designer", "Paris, France"),
            _person_card("Dee", "Writer", "Rome, Italy"),
        ]
    )

    @classmethod
    def _pages(cls) -> list[ExtractedSection]:
        return [
            extracted(
                cls.PAGE_1, [_person_ref("ada", "Ada"), _person_ref("bob", "Bob")]
            ),
            extracted(cls.PAGE_2, [_person_ref("bob", "Bob"), _person_ref("cy", "Cy")]),
        ]

    def test_rows_are_parsed_per_page_and_deduped_by_url(self):
        rows, count = search_rows(parse_people_cards, self._pages(), "person")

        assert [(row["name"], row["url"]) for row in rows] == [
            ("Ada", "/in/ada/"),
            ("Bob", "/in/bob/"),
            ("Cy", "/in/cy/"),
            ("Dee", None),
        ]
        assert rows[1] == {
            "name": "Bob",
            "degree": "2nd",
            "headline": "Founder",
            "location": "Berlin, Germany",
            "snippet": None,
            "url": "/in/bob/",
        }
        # The first page's header, not the last one's.
        assert count == 1234

    def test_pages_joined_before_parsing_would_lose_the_boundary_card(self):
        # The invariant the per-page contract protects: parsed as one text,
        # Bob's location on page 1 runs into the separator and page 2's
        # header, and the row count drops.
        joined = extracted(
            self.PAGE_1 + "\n---\n" + self.PAGE_2,
            [_person_ref("ada", "Ada"), _person_ref("bob", "Bob")],
        )
        per_page, _ = search_rows(parse_people_cards, self._pages(), "person")
        as_one, _ = search_rows(parse_people_cards, [joined], "person")

        assert len(per_page) == 4
        assert [row["location"] for row in as_one][1] != "Berlin, Germany"

    def test_a_parser_failure_yields_no_rows_but_keeps_the_count(self, caplog):
        def parser(text, refs):
            raise RuntimeError("parser bug")

        with caplog.at_level(logging.WARNING, logger=search_pages_module.__name__):
            rows, count = search_rows(parser, self._pages()[:1], "person")

        assert rows == []
        assert count == 1234
        assert "Could not parse result cards on page 1" in caplog.text

    def test_a_failing_page_does_not_take_the_others_down(self, caplog):
        calls = 0

        def parser(text, refs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("parser bug")
            return parse_people_cards(text, refs)

        with caplog.at_level(logging.WARNING, logger=search_pages_module.__name__):
            rows, _ = search_rows(parser, self._pages(), "person")

        assert [row["name"] for row in rows] == ["Bob", "Cy", "Dee"]
        assert "Could not parse result cards on page 1" in caplog.text

    def test_anchors_without_rows_warn_of_an_unrecognised_layout(self, caplog):
        """A page with profile anchors is a page of cards. Parsing none of
        them is a layout the text parser does not know (or a locale whose
        degree token differs), not an empty result, and must not pass as
        one in silence."""
        # The cards carry no ``• 2nd`` head, as a non-English page might.
        page = "About 12 results\n\nAda\nIngenieurin\nBerlin\n\nBob\nGruender\nWien"
        with caplog.at_level(logging.WARNING, logger=search_pages_module.__name__):
            rows, count = search_rows(
                parse_people_cards,
                [
                    extracted(
                        page, [_person_ref("ada", "Ada"), _person_ref("bob", "Bob")]
                    )
                ],
                "person",
            )

        assert rows == []
        assert count == 12
        assert [r.message for r in caplog.records if r.levelno == logging.WARNING] == [
            "Page 1: 2 references but no result rows parsed (unrecognised card "
            "layout or locale)"
        ]

    def test_only_anchors_of_the_row_kind_count(self, caplog):
        # A sidebar of company anchors on a people page is not evidence of
        # unparsed people cards.
        page = "No results found"
        with caplog.at_level(logging.WARNING, logger=search_pages_module.__name__):
            rows, _ = search_rows(
                parse_people_cards,
                [extracted(page, [_company_ref("acme", "Acme")])],
                "person",
            )

        assert rows == []
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_a_page_without_anchors_or_rows_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger=search_pages_module.__name__):
            rows, count = search_rows(
                parse_people_cards, [extracted("No results found")], "person"
            )

        assert rows == []
        assert count is None
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_no_page_means_no_rows_and_no_count(self):
        assert search_rows(parse_people_cards, [], "person") == ([], None)

    def test_rows_without_a_url_are_never_deduped_against_each_other(self):
        # Two anchorless cards with the same name are two rows: only a URL
        # is an identity.
        page = "\n\n".join(
            [
                _person_card("LinkedIn Member", "Engineer", "Oslo, Norway"),
                _person_card("LinkedIn Member", "Designer", "Bergen, Norway"),
            ]
        )
        rows, _ = search_rows(parse_people_cards, [extracted(page)], "person")

        assert [row["headline"] for row in rows] == ["Engineer", "Designer"]
        assert all(row["url"] is None for row in rows)


class TestSearchRowsCompanies:
    """``companies`` rows, same wiring as ``TestSearchRowsPeople``."""

    # Beta is the repeat. Delta closes page 1: a company card is read
    # backwards from its followers line, and joined before parsing that line
    # would run into the separator and page 2's header and stop being one,
    # so Delta would be lost rather than shifted.
    PAGE_1 = "About 5,200 results\n\n" + "\n\n".join(
        [
            _company_card("Acme", "Software Development", "Austin, Texas", "Tools"),
            _company_card("Beta Ltd", "Financial Services", "London, England", "Pay"),
            _company_card("Delta", "Insurance", "Oslo, Oslo", "Cover"),
        ]
    )
    PAGE_2 = "About 5,100 results\n\n" + "\n\n".join(
        [
            _company_card("Beta Ltd", "Financial Services", "London, England", "Pay"),
            _company_card("Gamma", "Banking", "Zurich, Zurich", "Vault"),
        ]
    )

    @classmethod
    def _pages(cls) -> list[ExtractedSection]:
        return [
            extracted(
                cls.PAGE_1,
                [
                    _company_ref("acme", "Acme"),
                    _company_ref("beta", "Beta Ltd"),
                    _company_ref("delta", "Delta"),
                ],
            ),
            extracted(
                cls.PAGE_2,
                [_company_ref("beta", "Beta Ltd"), _company_ref("gamma", "Gamma")],
            ),
        ]

    def test_rows_are_parsed_per_page_and_deduped_by_url(self):
        rows, count = search_rows(parse_company_cards, self._pages(), "company")

        assert [(row["name"], row["url"]) for row in rows] == [
            ("Acme", "/company/acme/"),
            ("Beta Ltd", "/company/beta/"),
            ("Delta", "/company/delta/"),
            ("Gamma", "/company/gamma/"),
        ]
        assert rows[2] == {
            "name": "Delta",
            "industry": "Insurance",
            "location": "Oslo, Oslo",
            "tagline": "Cover",
            "followers": 20000,
            "url": "/company/delta/",
        }
        assert count == 5200

    def test_anchors_without_rows_warn_of_an_unrecognised_layout(self, caplog):
        # Company cards are found by their followers line; a locale that
        # spells it differently yields no rows from a page full of anchors.
        page = "Acme\nSoftware\nOslo, Oslo\nFolgen\nTools\n1.200 Follower:innen"
        with caplog.at_level(logging.WARNING, logger=search_pages_module.__name__):
            rows, _ = search_rows(
                parse_company_cards,
                [extracted(page, [_company_ref("acme", "Acme")])],
                "company",
            )

        assert rows == []
        assert [r.message for r in caplog.records if r.levelno == logging.WARNING] == [
            "Page 1: 1 references but no result rows parsed (unrecognised card "
            "layout or locale)"
        ]


def _capture(page) -> SectionCapture:
    """Wire the capture owner the way the facade does."""
    session = ScrapingSession(page)
    return SectionCapture(session, PageNavigator(session), PageContentReader(session))


def _page(n: int) -> ExtractedSection:
    """One results page holding a single, page-unique person."""
    return extracted(
        f"Person {n}",
        [{"kind": "person", "url": f"/in/person{n}/", "text": f"Person {n}"}],
    )


class TestPaginateSearch:
    """``max_pages`` walks LinkedIn's ``&page=N`` facet (issue #526)."""

    @staticmethod
    def _walk(
        capture: SectionCapture,
        *,
        kind: str = "person",
        max_pages: int = 1,
        pace_first: bool = False,
    ):
        return paginate_search(
            capture,
            capture._session,
            PEOPLE,
            kind=kind,
            max_pages=max_pages,
            pace_first=pace_first,
        )

    async def test_search_pages_starts_empty(self):
        gathered = SearchPages()

        assert gathered == SearchPages([], [], [], {})
        # Fresh containers per instance, not one shared default.
        gathered.page_texts.append("x")
        assert SearchPages().page_texts == []

    async def test_one_page_by_default_with_the_uncapped_search_plan(self, mock_page):
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), _page(2)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep,
        ):
            gathered = await self._walk(capture)

        assert fetch.await_count == 1
        assert fetch.await_args_list[0].args == (
            PEOPLE,
            "search_results",
            CapturePlan(CaptureMode.SEARCH_RESULTS, apply_cap=False),
        )
        assert gathered.page_texts == ["Person 1"]
        assert gathered.pages == [_page(1)]
        assert gathered.section_errors == {}
        sleep.assert_not_awaited()

    async def test_pages_are_joined_in_order_and_paced_between(self, mock_page):
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), _page(2), _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep,
            patch(
                "linkedin_mcp_server.scraping.session.jitter",
                side_effect=lambda base, spread=0.5: base,
            ),
            patch.object(search_pages_module, "nav_delay", return_value=7.0),
        ):
            gathered = await self._walk(capture, max_pages=3)

        urls = [c.args[0] for c in fetch.await_args_list]
        assert urls == [PEOPLE, f"{PEOPLE}&page=2", f"{PEOPLE}&page=3"]
        # One pause per page after the first, before its navigation, at the
        # navigation delay read at call time.
        assert [c.args for c in sleep.await_args_list] == [(7.0,), (7.0,)]
        assert gathered.page_texts == ["Person 1", "Person 2", "Person 3"]
        assert [r["url"] for r in gathered.page_references] == [
            "/in/person1/",
            "/in/person2/",
            "/in/person3/",
        ]

    async def test_pace_first_spaces_the_first_page_too(self, mock_page):
        """A facet resolution may have just navigated; the first results
        page then gets the same spacing as every later one."""
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), _page(2)],
            ),
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep,
        ):
            await self._walk(capture, max_pages=2, pace_first=True)

        assert sleep.await_count == 2

    async def test_stops_when_a_page_repeats_people(self, mock_page):
        """Running past the last page re-serves it; stop instead of looping."""
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), _page(1), _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            gathered = await self._walk(capture, max_pages=10)

        assert fetch.await_count == 2
        # The re-served page is dropped: joining it would hand the caller the
        # same people twice and make the parsed rows disagree with the text.
        assert gathered.page_texts == ["Person 1"]
        assert [r["url"] for r in gathered.page_references] == ["/in/person1/"]
        assert gathered.section_errors == {}

    async def test_a_first_page_without_people_is_still_returned(self, mock_page):
        """Only a re-served *later* page is dropped; an empty first page is
        the whole answer and its raw text is what the caller gets to read."""
        capture = _capture(mock_page)
        with patch.object(
            capture,
            "capture",
            new_callable=AsyncMock,
            side_effect=[extracted("No results")],
        ):
            gathered = await self._walk(capture, max_pages=3)

        assert gathered.page_texts == ["No results"]

    async def test_only_references_of_the_walked_kind_count_as_new(self, mock_page):
        """A page of nothing but company/job anchors is the end of the
        people results."""
        capture = _capture(mock_page)
        filler = extracted(
            "Sidebar",
            [{"kind": "company", "url": "/company/acme/", "text": "Acme"}],
        )
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), filler, _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            gathered = await self._walk(capture, max_pages=10)

        assert fetch.await_count == 2
        assert gathered.page_texts == ["Person 1"]

    async def test_kind_selects_which_anchors_keep_the_walk_going(self, mock_page):
        capture = _capture(mock_page)
        company = extracted(
            "Acme", [{"kind": "company", "url": "/company/acme/", "text": "Acme"}]
        )
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[company, _page(2), _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await self._walk(capture, kind="company", max_pages=3)

        # Page 2 carries people only, which is not a new company.
        assert fetch.await_count == 2

    async def test_rate_limit_midway_keeps_earlier_pages(self, mock_page):
        capture = _capture(mock_page)
        with (
            patch.object(
                capture,
                "capture",
                new_callable=AsyncMock,
                side_effect=[_page(1), extracted(RATE_LIMITED_SECTION_TEXT), _page(3)],
            ) as fetch,
            patch(
                "linkedin_mcp_server.scraping.session.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            gathered = await self._walk(capture, max_pages=5)

        assert fetch.await_count == 2
        assert gathered.page_texts == ["Person 1"]
        assert gathered.pages == [_page(1)]
        assert gathered.section_errors["search_results"]["error_type"] == "rate_limit"

    async def test_a_throttled_page_reports_the_rate_limit_over_its_error(
        self, mock_page
    ):
        # The more specific diagnosis wins when a page carries both.
        capture = _capture(mock_page)
        throttled = extracted(
            RATE_LIMITED_SECTION_TEXT, error={"error_type": "NetworkError"}
        )
        with patch.object(
            capture, "capture", new_callable=AsyncMock, return_value=throttled
        ):
            gathered = await self._walk(capture)

        assert gathered.section_errors["search_results"]["error_type"] == "rate_limit"

    async def test_an_errored_page_surfaces_its_diagnostics(self, mock_page):
        capture = _capture(mock_page)
        failed = extracted("", error={"error_type": "NetworkError"})
        with patch.object(
            capture, "capture", new_callable=AsyncMock, return_value=failed
        ):
            gathered = await self._walk(capture)

        assert gathered.page_texts == []
        assert gathered.section_errors == {
            "search_results": {"error_type": "NetworkError"}
        }

    async def test_an_empty_page_without_an_error_is_not_reported(self, mock_page):
        capture = _capture(mock_page)
        with patch.object(
            capture, "capture", new_callable=AsyncMock, return_value=extracted("")
        ):
            gathered = await self._walk(capture)

        assert gathered == SearchPages()

    async def test_references_are_capped_per_page_but_pages_are_not(self, mock_page):
        # Six cards naming two mutual connections each is 18 ``/in/``
        # anchors, three past the section cap. The page reaches ``pages``
        # uncapped so card six still finds its own anchor; the cap lands on
        # ``page_references`` instead.
        refs: list[Reference] = []
        for i in range(1, 7):
            refs.append(_person_ref(f"person-{i}", f"Person {i}"))
            refs.append(_person_ref(f"mutual-{i}a", f"Mutual {i}A"))
            refs.append(_person_ref(f"mutual-{i}b", f"Mutual {i}B"))
        capture = _capture(mock_page)
        with patch.object(
            capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("cards", refs),
        ):
            gathered = await self._walk(capture)

        assert len(gathered.pages[0].references) == 18
        assert len(gathered.page_references) == 15

    async def test_duplicate_anchors_within_a_page_are_collapsed(self, mock_page):
        # A card links its own profile from the photo and the name.
        twice = extracted(
            "Person 1", [_person_ref("person1", ""), _person_ref("person1", "Person 1")]
        )
        capture = _capture(mock_page)
        with patch.object(
            capture, "capture", new_callable=AsyncMock, return_value=twice
        ):
            gathered = await self._walk(capture)

        assert gathered.page_references == [_person_ref("person1", "Person 1")]


@pytest.mark.parametrize("max_pages", [0, -1])
async def test_no_pages_requested_means_no_navigation(mock_page, max_pages):
    capture = _capture(mock_page)
    with patch.object(capture, "capture", new_callable=AsyncMock) as fetch:
        gathered = await paginate_search(
            capture,
            capture._session,
            PEOPLE,
            kind="person",
            max_pages=max_pages,
            pace_first=True,
        )

    fetch.assert_not_awaited()
    assert gathered == SearchPages()
