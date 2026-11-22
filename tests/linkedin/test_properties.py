"""Properties of the pure parsers and builders under ``linkedin/``."""

from __future__ import annotations

import json
import sys
import unicodedata
from urllib.parse import parse_qs, urlparse

import pytest
from hypothesis import given, strategies as st

from linkedin_mcp_server.linkedin.contracts import (
    FilterValidationError,
    normalize_message_text,
)
from linkedin_mcp_server.linkedin.feed_payload import (
    append_permalink_references,
    build_feed_references,
)
from linkedin_mcp_server.linkedin.identifiers import job_id_from_path
from linkedin_mcp_server.linkedin.job_policy import (
    parse_posting_title,
    reconcile_search_references,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.search_urls import (
    build_company_search_url,
    build_content_search_url,
    build_job_search_url,
    build_people_search_url,
)
from linkedin_mcp_server.linkedin.text import JOB_SEARCH_EN_US

printable = st.text(st.characters(exclude_categories=("Cs",)), max_size=20)
optional = st.none() | printable


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query, keep_blank_values=True, strict_parsing=True)


def _filter(known: dict[str, str]) -> st.SearchStrategy[tuple[str | None, str | None]]:
    unknown = st.from_regex(r"x-[a-z0-9+&=%]{0,8}", fullmatch=True)
    return (
        st.just((None, None))
        | st.just(("", None))
        | st.sampled_from(sorted(known.items()))
        | unknown.map(lambda value: (value, value))
    )


@given(
    keywords=printable,
    location=optional,
    date_posted=_filter({"past_week": "r604800", " past_24_hours ": "r86400"}),
    job_type=_filter({"full_time": "F", "contract, x-new ,part_time": "C,x-new,P"}),
    experience_level=_filter({"entry": "2", "director,executive": "5,6"}),
    work_type=_filter({"remote": "2", "on_site,hybrid": "1,3"}),
    easy_apply=st.booleans(),
    sort_by=_filter({"date": "DD", " relevance": "R"}),
)
def test_a_job_search_url_carries_each_filter_as_the_code_linkedin_reads(
    keywords: str,
    location: str | None,
    date_posted: tuple[str | None, str | None],
    job_type: tuple[str | None, str | None],
    experience_level: tuple[str | None, str | None],
    work_type: tuple[str | None, str | None],
    easy_apply: bool,
    sort_by: tuple[str | None, str | None],
) -> None:
    url = build_job_search_url(
        keywords,
        location,
        date_posted[0],
        job_type[0],
        experience_level[0],
        work_type[0],
        easy_apply,
        sort_by[0],
    )
    expected = {
        "keywords": keywords,
        "location": location or None,
        "f_TPR": date_posted[1],
        "f_JT": job_type[1],
        "f_E": experience_level[1],
        "f_WT": work_type[1],
        "f_AL": "true" if easy_apply else None,
        "sortBy": sort_by[1],
    }

    assert url.startswith("https://www.linkedin.com/jobs/search/?")
    assert urlparse(url).fragment == ""
    assert _query(url) == {
        key: [value] for key, value in expected.items() if value is not None
    }


@given(keywords=printable)
def test_search_keywords_survive_every_search_url(keywords: str) -> None:
    for url in (
        build_company_search_url(keywords),
        build_content_search_url(keywords),
        build_people_search_url(keywords),
    ):
        query = _query(url)

        assert query["keywords"] == [keywords]
        assert set(query) <= {"keywords", "origin"}


CONTENT_DATES = {
    None: None,
    "": None,
    "  ": None,
    "past-24h": "past-24h",
    "past_24_hours": "past-24h",
    " past_week ": "past-week",
    "past-month": "past-month",
}
CONTENT_TYPES = {
    None: None,
    "": None,
    "videos": "videos",
    "images": "photos",
    " job_posts": "jobs",
    "live_videos": "liveVideos",
    "documents": "documents",
}
UNKNOWN_CONTENT_FILTERS = ("past_hour", "past-year", "articles", "photos", "Videos")


@given(
    keywords=printable,
    date_posted=st.sampled_from(sorted(CONTENT_DATES, key=str)),
    content_type=st.sampled_from(sorted(CONTENT_TYPES, key=str)),
    unknown=st.none() | st.sampled_from(UNKNOWN_CONTENT_FILTERS),
    unknown_is_the_date=st.booleans(),
)
def test_a_content_search_facet_is_one_known_token_or_a_refusal(
    keywords: str,
    date_posted: str | None,
    content_type: str | None,
    unknown: str | None,
    unknown_is_the_date: bool,
) -> None:
    if unknown is not None:
        arguments = (
            (unknown, content_type) if unknown_is_the_date else (date_posted, unknown)
        )
        with pytest.raises(FilterValidationError):
            build_content_search_url(keywords, *arguments)
        return

    query = _query(build_content_search_url(keywords, date_posted, content_type))
    facets = {
        "datePosted": CONTENT_DATES[date_posted],
        "contentType": CONTENT_TYPES[content_type],
    }

    assert query.pop("keywords") == [keywords]
    assert query.pop("origin") == ["FACETED_SEARCH"]
    assert {key: json.loads(value[0]) for key, value in query.items()} == {
        key: [token] for key, token in facets.items() if token
    }


numeric = st.from_regex(r"[0-9]{1,12}", fullmatch=True)
profile_id = st.from_regex(
    r"(urn:li:fsd_profile:)?ACoA[A-Za-z0-9_-]{1,12}", fullmatch=True
)


@given(
    location=st.none() | numeric,
    network=st.none() | st.lists(st.sampled_from(("F", "S", "O")), max_size=4),
    current_company=st.none() | numeric,
    connection_of=st.none() | profile_id,
    ignored=st.sampled_from(
        (None, "location", "network", "current_company", "connection_of")
    ),
)
def test_a_people_search_facet_holds_the_given_value_or_the_search_is_refused(
    location: str | None,
    network: list[str] | None,
    current_company: str | None,
    connection_of: str | None,
    ignored: str | None,
) -> None:
    if ignored is not None:
        with pytest.raises(FilterValidationError):
            build_people_search_url(
                "engineer",
                "Berlin" if ignored == "location" else location,
                ["F", "X"] if ignored == "network" else network,
                "SAP" if ignored == "current_company" else current_company,
                "ada-lovelace" if ignored == "connection_of" else connection_of,
            )
        return

    url = build_people_search_url(
        "engineer", location, network, current_company, connection_of
    )
    facets = {
        key: json.loads(value[0])
        for key, value in _query(url).items()
        if key != "keywords"
    }
    expected = {
        "geoUrn": [location] if location else None,
        "network": network or None,
        "currentCompany": [current_company] if current_company else None,
        "connectionOf": [connection_of.removeprefix("urn:li:fsd_profile:")]
        if connection_of
        else None,
    }

    assert facets == {key: value for key, value in expected.items() if value}


JS_TRIMMED = frozenset(
    {"\n", "\r", "\ufeff"}
    | {
        chr(code)
        for code in range(sys.maxunicode + 1)
        if unicodedata.category(chr(code)) == "Zs"
    }
)
admissible = st.characters(
    exclude_categories=("Cs", "Cc"), exclude_characters="\u2028\u2029"
) | st.sampled_from(sorted(JS_TRIMMED | {"a", "."}))
padding = st.text(st.sampled_from(sorted(JS_TRIMMED)), max_size=6)


line_structure = st.sampled_from(
    ("a", ".", " ", "\r", "\n", "\r\n", "\ufeff", "\u3000")
)


@given(
    message=st.text(admissible, max_size=40)
    | st.lists(line_structure, max_size=12).map("".join)
)
def test_normalized_message_text_is_trimmed_and_stable(message: str) -> None:
    text = normalize_message_text(message)

    assert "\r" not in text
    assert text == "" or (text[0] not in JS_TRIMMED and text[-1] not in JS_TRIMMED)
    assert normalize_message_text(text) == text


@given(
    before=padding,
    after=padding,
    lines=st.lists(
        st.text(admissible, min_size=1, max_size=10).filter(
            lambda line: (
                not set(line) & {"\n", "\r"}
                and line[0] not in JS_TRIMMED
                and line[-1] not in JS_TRIMMED
            )
        ),
        min_size=1,
        max_size=3,
    ),
    line_end=st.sampled_from(("\n", "\r\n", "\r")),
)
def test_trimming_a_message_removes_its_padding_and_nothing_else(
    before: str, after: str, lines: list[str], line_end: str
) -> None:
    message = before + line_end.join(lines) + after

    assert normalize_message_text(message) == "\n".join(lines)


word = st.text(
    st.characters(exclude_categories=("Cs", "Cc", "Zs", "Zl", "Zp")),
    min_size=1,
    max_size=12,
)


@given(
    card_title=st.lists(word, min_size=1, max_size=4).map(" | ".join),
    company=word,
    unread=st.sampled_from(("", "(3) ", "(99+) ")),
)
def test_a_posting_title_splits_into_the_card_title_and_the_company(
    card_title: str, company: str, unread: str
) -> None:
    title = f"{unread}{card_title} | {company} | LinkedIn"

    assert parse_posting_title(title) == (card_title, company)


@given(count=st.integers(0, 10**12), lower_bound=st.booleans(), plural=st.booleans())
def test_an_advertised_result_count_reads_back_as_the_number(
    count: int, lower_bound: bool, plural: bool
) -> None:
    line = f"{count:,}{'+' if lower_bound else ''} result{'s' if plural else ''}"
    text = f"Python in Berlin\n{line}\nSenior Engineer\n"

    assert JOB_SEARCH_EN_US.result_count(text) == (count, not lower_bound)


job_reference = st.integers(1, 40).map(
    lambda number: Reference(kind="job", url=f"/jobs/view/{number}/", text="Engineer")
)
other_reference = st.integers(1, 40).map(
    lambda number: Reference(
        kind="company", url=f"/company/acme-{number}/", text="Acme"
    )
)


@given(
    references=st.lists(job_reference | other_reference, max_size=40),
    rail=st.lists(st.integers(1, 30).map(str), max_size=30),
)
def test_the_results_rail_decides_which_jobs_a_search_page_references(
    references: list[Reference], rail: list[str]
) -> None:
    reconciled = reconcile_search_references(references, rail)
    jobs = [job_id_from_path(ref["url"]) for ref in reconciled if ref["kind"] == "job"]
    others = [ref for ref in reconciled if ref["kind"] != "job"]

    assert sorted(job or "" for job in jobs) == sorted(set(rail))
    assert len(others) <= max(0, 15 - len(set(rail)))


post_path = st.integers(0, 80).map(
    lambda n: f"/posts/ada_post-activity-{7_000_000_000_000 + n}-abcd"
)
dom_reference = st.integers(0, 20).map(
    lambda number: Reference(kind="person", url=f"/in/person-{number}/", text="Ada")
) | post_path.map(lambda path: Reference(kind="feed_post", url=path))


@given(
    dom=st.lists(dom_reference, max_size=20),
    captured=st.lists(post_path, max_size=120)
    | st.permutations(range(81)).map(
        lambda order: [
            f"/posts/ada_post-activity-{7_000_000_000_000 + n}-abcd" for n in order
        ]
    ),
)
def test_captured_permalinks_never_displace_dom_references(
    dom: list[Reference], captured: list[str]
) -> None:
    combined = append_permalink_references(dom, captured, context="search_results")
    appended = [ref["url"] for ref in combined[len(dom) :]]
    present = {ref["url"] for ref in dom}

    assert combined[: len(dom)] == dom
    assert len(appended) <= 50
    assert (
        appended
        == [path for path in dict.fromkeys(captured) if path not in present][:50]
    )


POST_ANCHOR = "/feed/update/urn:li:activity:7000000000001/"


@given(
    urns=st.lists(
        st.integers(0, 80).map(lambda n: f"urn:li:activity:{7_000_000_000_000 + n}")
        | st.none(),
        max_size=70,
    ),
    anchors=st.lists(
        st.sampled_from(("/in/ada/", "/company/acme/", POST_ANCHOR, "/jobs/view/1/")),
        max_size=10,
    ),
    captured=st.lists(post_path, max_size=70),
)
def test_feed_references_are_the_rendered_posts_then_anchors_then_captures(
    urns: list[str | None], anchors: list[str], captured: list[str]
) -> None:
    references = build_feed_references(
        [
            {"href": f"https://www.linkedin.com{path}", "text": "Ada Lovelace"}
            for path in anchors
        ],
        [f"https://www.linkedin.com{path}" for path in captured],
        post_boundaries=[{"urn": urn, "text": "x" * 500} for urn in urns],
    )
    rendered = list(dict.fromkeys(f"/feed/update/{urn}/" for urn in urns if urn))
    expected = list(
        dict.fromkeys(
            [*rendered, *(path for path in anchors if path == POST_ANCHOR), *captured]
        )
    )[:50]

    assert [ref["url"] for ref in references] == expected
    assert all(ref["kind"] == "feed_post" for ref in references)
    assert [ref.get("value") for ref in references[: len(rendered)]] == [
        url.removeprefix("/feed/update/").rstrip("/") for url in rendered[:50]
    ]
    assert all(ref.get("text") == "x" * 240 for ref in references[: len(rendered)])
