"""Tests for the people- and company-search card parsers.

The fixtures are the innerText LinkedIn served live for a people search
(``/search/results/people/``) and a company search
(``/search/results/companies/?keywords=fintech&page=2``), so each test is a
claim about LinkedIn's page shape, not only about the algorithm. The people
page is anonymised: every name, mutual connection, identifying employer and
distinctive headline or snippet is replaced, while line structure -- the
two-line and one-line degree markers, the snippet labels, the
mutual-connections lines, the lone followers line and the surrounding chrome
-- is kept exactly as captured. Company cards are public entities and are
kept as served, except for the first name on each "N other connections
follow this page" line, which is a connection of the account that captured
the page and is replaced.
"""

import logging

import pytest

from linkedin_mcp_server.scraping.company_parse import (
    parse_company_cards as reexported_parse_company_cards,
)
from linkedin_mcp_server.scraping.link_metadata import (
    RawReference,
    Reference,
    build_references,
)
from linkedin_mcp_server.scraping.search_parse import (
    parse_company_cards,
    parse_count,
    parse_people_cards,
    parse_result_count,
)

PEOPLE_PAGE = """Search with Sales Navigator

12+ additional advanced filters

Person One\x20
 • 2nd

Principal Software Engineer, Ads + ML at Example Corp

United States

Mutual Alpha is a mutual connection

Person Two\x20
 • 2nd

Software Engineer | Alum: Example University

San Francisco, California, United States

About: Software Engineer based in San Francisco primarily interested in computer systems: storage, networking, apis,...

Person Three • 2nd

Software Engineer

San Francisco, California, United States

Past: Lead Engineer at Example Health

Person Four\x20
 • 2nd

Staff Engineer | Head of ML Infrastructure | Data Platform & Tooling

Sunnyvale, California, United States

Current: * Head of ML Infrastructure (Data Platform & Tooling)

Person Five • 2nd

Principal AI Engineer

Greater Seattle Area

About: I am a principal-level AI engineer who does research almost as much as writing code

Mutual Beta is a mutual connection

Person Six\x20
 • 2nd

Engineering @ Example AI

New York, New York, United States

Current: Member of Engineering at example

Person Seven\x20
 • 2nd

Distinguished Engineer at Example Reviews

Melbourne, Florida, United States

Mutual Gamma is a mutual connection

Person Eight\x20
 • 2nd

Sr. Software Engineer

Menlo Park, California, United States

Past: ...shipping features as an individual contributor while leading a team of full-stack engineers in another region

Mutual D. is a mutual connection

 · 3K followers

Person-Nine Hyphen\x20
 • 2nd

Senior Frontend & UX Engineer

London Area, United Kingdom

Current: ...building the Design System and ensuring that our Designers work in perfect sync with the Engineers

Mutual Epsilon, MD PhD is a mutual connection

Person T.\x20
 • 2nd

🚀 Example MSc 🚀 Platform Engineer 🚀 Cloud native 🚀 Software Engineer SWE 🚀 Open to new connections! 🚀 Python Rust Kotlin Vue Scala C# 🚀 GCP 🚀 10+ YOE 🚀 Remote friendly 🚀

New York, New York, United States

Current: - Software engineer and working on Gen AI, LLMs, Data management and Search systems using Python, Golang, Ruby, Java,...

Mutual Zeta, Mutual Eta & 1 other mutual connection

Are these results helpful?

Your feedback helps us improve search results

1
2
3
4
5
6
7
8
9
10
Next"""

# The keyword-less variant of the same page: no snippets at all, so the
# followers line follows the mutual-connections line directly. (``\x20`` is
# the trailing space innerText leaves after a name whose marker wrapped to
# the next line, and after a location whose region is blank.)
PEOPLE_PAGE_NO_KEYWORDS = """Search with Sales Navigator

12+ additional advanced filters

Person Ay\x20
 • 2nd

Cloud Solutions Architect at Example Corp

Netherlands

Mutual One, Mutual Two & 12 other mutual connections

 · 2K followers

Person Bee • 2nd

Dr.-Ing. | Security Engineer

Germany

Mutual Three, Ph.D, Mutual Four & 35 other mutual connections

Person Cee\x20
 • 2nd

Product Lead & Group Product Manager | Product Strategy, Growth, Platforms & AI | Fintech, Marketplaces and Consumer Products

Amsterdam, North Holland, Netherlands

Mutual Five & Mutual Six are mutual connections

Are these results helpful?

Your feedback helps us improve search results

1
2
3
Next"""

COMPANY_PAGE = """About 5,200 results

Pharos Production - MiCA compliance software, FinTech AI agents, Web3 and Blockchain\x20

Software Development

Las Vegas, Nevada

Follow

Custom AI, FinTech, Web3 and enterprise software | 110+ apps since 2013 | 5/5 on Clutch

Alex & 14 other connections follow this page · 20K followers

Fintech Americas

Financial Services

Miami Beach, Florida

Follow

Fintech Americas es una empresa creadora de comunidades y de aprendizaje, enfocada en la transformación digital.

Blake & 13 other connections follow this page · 27K followers

Fintech Saudi | فنتك السعودية

Financial Services

Riyadh, Riyadh

Follow

...تتطلبها شركات الفنتك المالية ودعم رواد الأعمال في مجال التقنية المالية في كل مرحلة من مراحل تطورهم. Fintech Saudi Fintech Saudi was launched by the Saudi Central Bank in partnership with the Capital Market Authority in April 2018 to act as a catalyst for the development of the financial services technology (fintech...

Casey & 62 other connections follow this page · 69K followers

FinTech Futures

Technology, Information and Media

London, England

Follow

The #1 provider of global fintech news and intelligence

Dana & 57 other connections follow this page · 102K followers

Africa Fintech Summit

Venture Capital and Private Equity Principals

Washington DC,\x20

Follow

The global initiative dedicated to building an ecosystem of investors, pioneers, industry leaders in African Fintech.

Eli & 49 other connections follow this page · 61K followers

#AFTSLIVE | Fintechs Powering the Dangote IPO

Tomorrow, 3:00 PM (your local time) • Online event

Fintech Dünyası\x20

Financial Services

İstanbul, Istanbul

Follow

Fintech Ekosisteminin Buluşma Adresi | The News Portal of the Fintech Ecosystem

Finn & 5 other connections follow this page · 55K followers

Contact us

FinTech Connector\x20

Financial Services

New York, NY

Follow

Welcome to FinTech Connector on LinkedIn! Our mission is to foster collaboration and innovation in the financial services industry. We're dedicated to bringing together financial and business professionals, visionary fintech entrepreneurs, forward-thinking organizations, and capital providers. By uniting these diverse...

Gale & 15 other connections follow this page · 19K followers

Fintech Executive Search Consultants\x20

Staffing and Recruiting

Tampa, Florida

Follow

Executive Recruiting - Payments, Fintech, & SaaS

Harper & 15 other connections follow this page · 18K followers

FinTech Magazine

Broadcast Media Production and Distribution

Norwich, Norfolk

Follow

Connecting the World’s FinTech Leaders

Indy & 62 other connections follow this page · 112K followers

Stealth FinTech Startup

Financial Services

New York

Follow

Stealth FinTech Startup

Jules & 12 other connections follow this page · 44K followers

Previous
1
2
3
4
5
6
7
8
9
10
Next"""

# A facet combination with no hits: the page is only chrome and footer.
EMPTY_PAGE = """No results found

Try removing filters or rephrasing your search.

Edit search
Remove all filters

About

Accessibility

Help Center

Privacy & Terms

Ad Choices

Advertising

Business Services

Get the LinkedIn app

More

LinkedIn Corporation © 2026"""

# The company anchors in the order the page carries them, as ``company_sample``
# recorded live: one per card, plus the summit's event link, which sits inside
# its card's event block.
COMPANY_SLUGS = [
    "pharos-production-web3-software-development",
    "fintech-americas",
    "fintech-saudi",
    "fintechfutures",
    "the-africa-fintech-summit",
    "fintech-dunyasi",
    "fintech-connector",
    "fintech-executive-search-consultants",
    "fintech-magazine-bizclik",
    "stealth-startup-tlv",
]


def _anchor(path: str, text: str) -> RawReference:
    return {"href": f"https://www.linkedin.com{path}", "text": text}


def _company_block(name: str, industry: str, location: str, tagline: str) -> str:
    return (
        f"{name}\n\n{industry}\n\n{location}\n\nFollow\n\n{tagline}\n\n"
        "Alex & 2 other connections follow this page · 2K followers"
    )


def _refs_for(*names: str) -> list[Reference]:
    return [
        {
            "kind": "company",
            "url": f"/company/{name.lower().replace(' ', '-')}/",
            "text": name,
        }
        for name in names
    ]


# Synthetic: a card short of its tagline, with a promoted event above it.
TAGLINE_LESS_AFTER_EVENT = "\n\n".join(
    [
        "About 3 results",
        "Big Summit | Fintechs Powering Growth",
        "Tomorrow, 3:00 PM (your local time) • Online event",
        "Only Name",
        "Software",
        "Las Vegas, Nevada",
        "Follow",
        "Alex & 2 other connections follow this page · 2K followers",
        _company_block("Whole Co", "Banking", "Bern", "Vaults"),
    ]
)

# Synthetic, as reported: a tagline-less first card whose one-word location
# lands in the button slot, so the five-line read starts at the header.
TAGLINE_LESS_UNDER_HEADER = "\n\n".join(
    [
        "About 1 result",
        "Acme",
        "Software Development",
        "Netherlands",
        "Follow",
        " · 3K followers",
    ]
)


def _people_refs(apply_cap: bool = True) -> list[Reference]:
    """The people page's anchors run through the real reference builder: a
    card's own anchor, then each mutual connection's, as the DOM has them."""
    raw = [
        _anchor("/in/person-one-8ab00514/", "Person One"),
        _anchor("/in/mutual-alpha/", "Mutual Alpha"),
        _anchor("/in/person-two-b3ba4a4/", "Person Two"),
        _anchor("/in/personthree/", "Person Three"),
        _anchor("/in/person-four/", "Person Four"),
        _anchor("/in/pfive/", "Person Five"),
        _anchor("/in/mutual-beta/", "Mutual Beta"),
        _anchor("/in/psix/", "Person Six"),
        _anchor("/in/pseven/", "Person Seven"),
        _anchor("/in/mutual-gamma/", "Mutual Gamma"),
        _anchor("/in/peight/", "Person Eight"),
        _anchor("/in/mutual-d-9b4737230/", "Mutual D."),
        _anchor("/in/person-nine-hyphen/", "Person-Nine Hyphen"),
        _anchor("/in/mutual-epsilon/", "Mutual Epsilon, MD PhD"),
        _anchor("/in/person-t/", "Person T."),
        _anchor("/in/mutual-zeta/", "Mutual Zeta"),
        _anchor("/in/mutual-eta/", "Mutual Eta"),
    ]
    return build_references(raw, "search_results", apply_cap=apply_cap)


def _company_refs() -> list[Reference]:
    names = [row.strip() for row in COMPANY_PAGE.split("\n\n") if row.strip()]
    card_names = [
        names[i]
        for i in range(1, len(names))
        if i + 4 < len(names) and names[i + 3] == "Follow"
    ]
    assert len(card_names) == len(COMPANY_SLUGS)
    raw: list[RawReference] = []
    for slug, name in zip(COMPANY_SLUGS, card_names):
        raw.append(_anchor(f"/company/{slug}/", name))
        # The relationship blurb links the page too; clean_label drops it.
        raw.append(
            _anchor(f"/company/{slug}/", "Alex & 14 other connections follow this page")
        )
        if slug == "the-africa-fintech-summit":
            raw.append(
                _anchor(f"/company/{slug}/events/", "#AFTSLIVE | Fintechs Powering")
            )
    return build_references(raw, "search_results")


class TestPeopleCards:
    def test_one_row_per_card_in_page_order(self):
        rows = parse_people_cards(PEOPLE_PAGE, [])
        assert [r["name"] for r in rows] == [
            "Person One",
            "Person Two",
            "Person Three",
            "Person Four",
            "Person Five",
            "Person Six",
            "Person Seven",
            "Person Eight",
            "Person-Nine Hyphen",
            "Person T.",
        ]
        assert {r["degree"] for r in rows} == {"2nd"}

    def test_two_line_and_one_line_markers_read_the_same(self):
        rows = parse_people_cards(PEOPLE_PAGE, [])
        two_line, one_line = rows[0], rows[2]
        assert two_line == {
            "name": "Person One",
            "degree": "2nd",
            "headline": "Principal Software Engineer, Ads + ML at Example Corp",
            "location": "United States",
            "snippet": None,
            "url": None,
        }
        assert one_line == {
            "name": "Person Three",
            "degree": "2nd",
            "headline": "Software Engineer",
            "location": "San Francisco, California, United States",
            "snippet": "Past: Lead Engineer at Example Health",
            "url": None,
        }

    @pytest.mark.parametrize(
        ("index", "label"),
        [(1, "About:"), (3, "Current:"), (7, "Past:")],
    )
    def test_snippet_keeps_its_label(self, index, label):
        rows = parse_people_cards(PEOPLE_PAGE, [])
        assert rows[index]["snippet"].startswith(label)

    def test_mutual_connections_line_is_not_a_field(self):
        rows = parse_people_cards(PEOPLE_PAGE, [])
        for row in rows:
            for value in row.values():
                assert "mutual connection" not in str(value)

    def test_followers_line_is_parsed_only_where_present(self):
        rows = parse_people_cards(PEOPLE_PAGE, [])
        assert rows[7]["followers"] == 3000
        assert [i for i, r in enumerate(rows) if "followers" in r] == [7]

    def test_missing_optional_lines_do_not_shift_the_next_card(self):
        # Card 7 has neither snippet nor followers; card 8 has all of them.
        rows = parse_people_cards(PEOPLE_PAGE, [])
        assert rows[6]["snippet"] is None
        assert rows[6]["location"] == "Melbourne, Florida, United States"
        assert rows[7]["headline"] == "Sr. Software Engineer"
        assert rows[7]["location"] == "Menlo Park, California, United States"

    def test_trailing_chrome_does_not_leak_into_the_last_card(self):
        rows = parse_people_cards(PEOPLE_PAGE, [])
        last = rows[-1]
        assert last["location"] == "New York, New York, United States"
        assert last["snippet"].startswith("Current: - Software engineer")
        assert "followers" not in last
        assert not any("results helpful" in str(v) for v in last.values())

    def test_no_keyword_page_followers_after_a_mutual_line(self):
        rows = parse_people_cards(PEOPLE_PAGE_NO_KEYWORDS, [])
        assert [r["name"] for r in rows] == ["Person Ay", "Person Bee", "Person Cee"]
        assert rows[0]["followers"] == 2000
        assert rows[0]["snippet"] is None
        assert rows[1] == {
            "name": "Person Bee",
            "degree": "2nd",
            "headline": "Dr.-Ing. | Security Engineer",
            "location": "Germany",
            "snippet": None,
            "url": None,
        }
        assert rows[2]["location"] == "Amsterdam, North Holland, Netherlands"

    def test_urls_pair_by_name_and_skip_mutual_connection_anchors(self):
        rows = parse_people_cards(PEOPLE_PAGE, _people_refs(apply_cap=False))
        assert [r["url"] for r in rows] == [
            "/in/person-one-8ab00514/",
            "/in/person-two-b3ba4a4/",
            "/in/personthree/",
            "/in/person-four/",
            "/in/pfive/",
            "/in/psix/",
            "/in/pseven/",
            "/in/peight/",
            "/in/person-nine-hyphen/",
            "/in/person-t/",
        ]

    def test_a_card_without_an_anchor_gets_none_not_the_next_anchor(self):
        refs = [r for r in _people_refs(apply_cap=False) if r["url"] != "/in/psix/"]
        rows = parse_people_cards(PEOPLE_PAGE, refs)
        assert rows[5]["url"] is None
        assert rows[6]["url"] == "/in/pseven/"

    def test_rows_must_pair_against_the_uncapped_anchors(self):
        # Synthetic: eight cards, each naming two mutual connections, is 24
        # ``/in/`` anchors. The section cap of 15 keeps cards 1-5 whole and
        # cuts the rest, so a parser fed the capped list strands cards 6-8
        # without a URL. That is why the extractor pairs before capping.
        cards, raw = [], []
        for i in range(1, 9):
            cards.append(
                f"Person {i} • 2nd\n\nEngineer\n\nOslo, Norway\n\n"
                f"Mutual {i}A & Mutual {i}B are mutual connections"
            )
            raw.append(_anchor(f"/in/person-{i}/", f"Person {i}"))
            raw.append(_anchor(f"/in/mutual-{i}a/", f"Mutual {i}A"))
            raw.append(_anchor(f"/in/mutual-{i}b/", f"Mutual {i}B"))
        page = "\n\n".join(cards)
        capped = build_references(raw, "search_results")
        assert len(capped) == 15
        stranded = parse_people_cards(page, capped)
        assert [r["url"] for r in stranded][5:] == [None, None, None]
        uncapped = build_references(raw, "search_results", apply_cap=False)
        rows = parse_people_cards(page, uncapped)
        assert [r["url"] for r in rows] == [f"/in/person-{i}/" for i in range(1, 9)]

    def test_refs_without_text_pair_by_order(self):
        refs = [{"kind": "person", "url": f"/in/p{i}/"} for i in range(3)]
        rows = parse_people_cards(PEOPLE_PAGE_NO_KEYWORDS, refs)
        assert [r["url"] for r in rows] == ["/in/p0/", "/in/p1/", "/in/p2/"]

    def test_a_bullet_inside_a_headline_does_not_open_a_card(self):
        # Synthetic: the marker is a bullet plus a digit-led token, so a
        # headline's own bullet ("Founder • CEO") stays a headline.
        page = PEOPLE_PAGE_NO_KEYWORDS.replace(
            "Cloud Solutions Architect at Example Corp", "Founder • CEO"
        )
        rows = parse_people_cards(page, [])
        assert [r["name"] for r in rows] == ["Person Ay", "Person Bee", "Person Cee"]
        assert rows[0]["headline"] == "Founder • CEO"
        assert rows[0]["location"] == "Netherlands"

    def test_a_numeric_headline_is_not_a_followers_line(self):
        # Synthetic: "100 Employees" is a count and one word, which is the
        # followers shape minus the ``·``; without the separator it stays a
        # headline.
        page = PEOPLE_PAGE_NO_KEYWORDS.replace(
            "Cloud Solutions Architect at Example Corp", "100 Employees"
        )
        rows = parse_people_cards(page, [])
        assert rows[0]["headline"] == "100 Employees"
        assert rows[0]["location"] == "Netherlands"
        assert rows[0]["followers"] == 2000

    def test_a_year_after_a_bullet_does_not_open_a_card(self):
        # Synthetic: ``• 2024`` starts with a digit 1-3 like a degree token
        # but runs on into more digits, which no degree marker does.
        page = PEOPLE_PAGE_NO_KEYWORDS.replace(
            "Cloud Solutions Architect at Example Corp", "Founder @ X • 2024"
        )
        rows = parse_people_cards(page, [])
        assert [r["name"] for r in rows] == ["Person Ay", "Person Bee", "Person Cee"]
        assert rows[0]["headline"] == "Founder @ X • 2024"
        assert rows[0]["location"] == "Netherlands"

    def test_the_self_card_does_not_overwrite_the_previous_followers(self):
        # Synthetic: the ``• You`` card has no digit-led marker, so it is
        # absorbed into the card above; its followers line must not replace
        # that card's own count.
        page = PEOPLE_PAGE_NO_KEYWORDS.replace(
            "Person Bee • 2nd",
            "Me Myself • You\n\nOwner\n\nHere\n\n · 9K followers\n\nPerson Bee • 2nd",
        )
        rows = parse_people_cards(page, [])
        assert [r["name"] for r in rows] == ["Person Ay", "Person Bee", "Person Cee"]
        assert rows[0]["followers"] == 2000

    def test_other_kinds_are_ignored(self):
        refs = [{"kind": "company", "url": "/company/x/", "text": "Person Ay"}]
        rows = parse_people_cards(PEOPLE_PAGE_NO_KEYWORDS, refs)
        assert rows[0]["url"] is None

    def test_empty_page(self):
        assert parse_people_cards(EMPTY_PAGE, []) == []
        assert parse_people_cards("", []) == []


class TestCompanyCards:
    def test_one_row_per_card_in_page_order(self):
        rows = parse_company_cards(COMPANY_PAGE, [])
        assert [r["name"] for r in rows] == [
            "Pharos Production - MiCA compliance software, FinTech AI agents, Web3 and Blockchain",
            "Fintech Americas",
            "Fintech Saudi | فنتك السعودية",
            "FinTech Futures",
            "Africa Fintech Summit",
            "Fintech Dünyası",
            "FinTech Connector",
            "Fintech Executive Search Consultants",
            "FinTech Magazine",
            "Stealth FinTech Startup",
        ]

    def test_fields(self):
        rows = parse_company_cards(COMPANY_PAGE, [])
        assert rows[1] == {
            "name": "Fintech Americas",
            "industry": "Financial Services",
            "location": "Miami Beach, Florida",
            "tagline": "Fintech Americas es una empresa creadora de comunidades y de aprendizaje, enfocada en la transformación digital.",
            "followers": 27000,
            "url": None,
        }
        assert rows[3]["industry"] == "Technology, Information and Media"
        assert rows[3]["followers"] == 102000

    def test_followers_per_card(self):
        rows = parse_company_cards(COMPANY_PAGE, [])
        assert [r["followers"] for r in rows] == [
            20000,
            27000,
            69000,
            102000,
            61000,
            55000,
            19000,
            18000,
            112000,
            44000,
        ]

    def test_event_and_contact_blocks_between_cards_do_not_shift_them(self):
        rows = parse_company_cards(COMPANY_PAGE, [])
        assert rows[4]["tagline"].startswith("The global initiative")
        assert rows[5]["name"] == "Fintech Dünyası"
        assert rows[5]["industry"] == "Financial Services"
        assert rows[6]["name"] == "FinTech Connector"
        assert rows[6]["location"] == "New York, NY"
        for row in rows:
            assert "AFTSLIVE" not in str(row.values())
            assert "Contact us" not in str(row.values())

    def test_dangling_comma_in_location_is_trimmed(self):
        rows = parse_company_cards(COMPANY_PAGE, [])
        assert rows[4]["location"] == "Washington DC"

    def test_follow_button_is_never_a_field(self):
        rows = parse_company_cards(COMPANY_PAGE, [])
        assert all(
            "Follow" not in (r["industry"], r["location"], r["tagline"]) for r in rows
        )

    def test_urls_pair_through_the_real_reference_builder(self):
        rows = parse_company_cards(COMPANY_PAGE, _company_refs())
        urls = [r["url"] for r in rows]
        # The first card's name runs past clean_label's 80-character limit,
        # so the builder drops that anchor and the card keeps no URL rather
        # than the next company's.
        assert urls[0] is None
        # The builder cuts "Fintech Saudi | ..." at the bar; the card name is
        # cut the same way before the two are compared.
        assert urls[2] == "/company/fintech-saudi/"
        assert urls[1:] == [f"/company/{slug}/" for slug in COMPANY_SLUGS[1:]]

    def test_a_promoted_page_by_label_still_pairs(self):
        refs = [
            {
                "kind": "company",
                "url": "/company/fa/",
                "text": "Page by Fintech Americas",
            }
        ]
        rows = parse_company_cards(COMPANY_PAGE, refs)
        assert rows[1]["url"] == "/company/fa/"
        assert rows[0]["url"] is None

    def test_empty_page(self):
        assert parse_company_cards(EMPTY_PAGE, []) == []
        assert parse_company_cards("", []) == []

    def test_a_card_missing_a_line_is_skipped_not_guessed(self):
        page = "About 3 results\n\nOnly Name\n\nSoftware\n\nFollow\n\n · 2K followers"
        assert parse_company_cards(page, []) == []

    def test_a_short_card_after_an_event_block_is_skipped(self, caplog):
        # Synthetic: with a block before it, a card short of its tagline
        # still has five blocks above the followers line, and the window
        # slides up into the event. The button slot then holds the card's
        # "City, Region" location rather than a one-word button, which is
        # the tell.
        with caplog.at_level(logging.DEBUG):
            rows = parse_company_cards(TAGLINE_LESS_AFTER_EVENT, [])
        assert [r["name"] for r in rows] == ["Whole Co"]
        assert "short of a line" in caplog.text

    def test_a_tagline_less_card_after_an_event_block_is_read_with_refs(self):
        # The same page with the card's anchor: the ref names the fourth
        # slot above the followers line, so the card is read as four lines
        # and the event block above it is left alone.
        rows = parse_company_cards(TAGLINE_LESS_AFTER_EVENT, _refs_for("Only Name"))
        assert rows[0] == {
            "name": "Only Name",
            "industry": "Software",
            "location": "Las Vegas, Nevada",
            "tagline": None,
            "followers": 2000,
            "url": "/company/only-name/",
        }
        assert [r["name"] for r in rows] == ["Only Name", "Whole Co"]

    def test_a_tagline_less_card_under_the_header_is_read_with_refs(self):
        # Reported: a one-word location passes the button-slot guard, so a
        # five-line read of a tagline-less first card fabricated a row out
        # of the header. The anchor says which slot is the name.
        rows = parse_company_cards(TAGLINE_LESS_UNDER_HEADER, _refs_for("Acme"))
        assert rows == [
            {
                "name": "Acme",
                "industry": "Software Development",
                "location": "Netherlands",
                "tagline": None,
                "followers": 3000,
                "url": "/company/acme/",
            }
        ]

    def test_a_tagline_less_card_under_the_header_is_skipped_without_refs(self, caplog):
        # Without an anchor the five-line read is all there is, and its name
        # slot is the results header: skipped, not fabricated.
        with caplog.at_level(logging.DEBUG):
            assert parse_company_cards(TAGLINE_LESS_UNDER_HEADER, []) == []
        assert "under the header" in caplog.text

    def test_a_tagline_less_card_after_a_full_card_is_read_with_refs(self):
        page = "\n\n".join(
            [
                "About 3 results",
                _company_block("Whole Co", "Banking", "Bern", "Vaults"),
                "Acme",
                "Software Development",
                "Netherlands",
                "Follow",
                " · 3K followers",
            ]
        )
        refs = _refs_for("Whole Co", "Acme")
        rows = parse_company_cards(page, refs)
        assert [(r["name"], r["tagline"], r["url"]) for r in rows] == [
            ("Whole Co", "Vaults", "/company/whole-co/"),
            ("Acme", None, "/company/acme/"),
        ]
        # Without the anchor the four-line card is one block short of a
        # five-line read and is skipped, as before.
        assert [r["name"] for r in parse_company_cards(page, [])] == ["Whole Co"]

    def test_a_full_card_whose_industry_is_another_company_name_stays_five(self):
        # Synthetic: "Banking" is both Whole Co's industry and a company on
        # the page. The five-line read is tried first, so Whole Co's own
        # anchor wins over the industry slot and the card keeps its tagline.
        page = "\n\n".join(
            [
                "About 3 results",
                _company_block("Whole Co", "Banking", "Bern", "Vaults"),
                _company_block("Banking", "Financial Services", "Zug", "Loans"),
            ]
        )
        rows = parse_company_cards(page, _refs_for("Whole Co", "Banking"))
        assert [(r["name"], r["industry"], r["tagline"]) for r in rows] == [
            ("Whole Co", "Banking", "Vaults"),
            ("Banking", "Financial Services", "Loans"),
        ]

    def test_the_live_page_reads_the_same_with_and_without_refs(self):
        # The ref-driven shape choice must not touch the captured page: every
        # card there carries a tagline, including the first, whose anchor the
        # builder drops for length and which is therefore read by fallback.
        with_refs = parse_company_cards(COMPANY_PAGE, _company_refs())
        without = parse_company_cards(COMPANY_PAGE, [])
        assert [{**r, "url": None} for r in with_refs] == without
        assert len(with_refs) == 10
        assert all(r["tagline"] for r in with_refs)

    @pytest.mark.parametrize(
        ("name", "tagline"),
        [("500 Startups", "Seed fund"), ("Acme", "1 Team")],
    )
    def test_a_count_and_word_without_the_separator_is_not_a_followers_line(
        self, name, tagline
    ):
        # Synthetic: a name or tagline that is a count and one word would
        # read as a followers line and take the neighbouring card with it.
        page = "\n\n".join(
            [
                "About 3 results",
                _company_block(name, "Venture Capital", "Palo Alto", tagline),
                _company_block("Next Co", "Banking", "Bern", "Vaults"),
            ]
        )
        rows = parse_company_cards(page, [])
        assert [r["name"] for r in rows] == [name, "Next Co"]
        assert rows[0]["tagline"] == tagline
        assert rows[0]["followers"] == 2000

    def test_reexported_from_company_parse(self):
        assert reexported_parse_company_cards is parse_company_cards


class TestResultCount:
    def test_reads_the_header(self):
        assert parse_result_count(COMPANY_PAGE) == 5200

    def test_none_without_a_header(self):
        assert parse_result_count(PEOPLE_PAGE) is None
        assert parse_result_count(EMPTY_PAGE) is None
        assert parse_result_count("") is None

    @pytest.mark.parametrize(
        ("header", "count"),
        [
            ("About 5.200 results", 5200),
            ("Environ 5 200 résultats", 5200),
            ("Cerca de 5.200 resultados", 5200),
            ("2,000+ results", 2000),
            ("12+ additional advanced filters", None),
            ("Search with Sales Navigator\n\n12+ additional advanced filters", None),
        ],
    )
    def test_separators_and_upsell(self, header, count):
        assert parse_result_count(header) == count


class TestParseCount:
    @pytest.mark.parametrize(
        ("value", "count"),
        [
            ("3K", 3000),
            ("2.5K", 2500),
            ("1,5K", 1500),
            ("102K", 102000),
            ("1M", 1_000_000),
            ("1.2M", 1_200_000),
            ("5,200", 5200),
            ("5.200", 5200),
            ("1 234", 1234),
            ("1.5", None),
            ("followers", None),
        ],
    )
    def test_values(self, value, count):
        assert parse_count(value) == count
