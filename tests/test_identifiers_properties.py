"""Properties of the reference repair in ``linkedin/identifiers.py``."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote, urlparse

import pytest
from hypothesis import given, strategies as st

from linkedin_mcp_server.core.destination import is_linkedin_landing
from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.identifiers import (
    company_page_url,
    job_id_from_search_query,
    job_view_url,
    messaging_thread_url,
    normalize_company_identifier,
    normalize_job_id,
    normalize_opaque_id,
    normalize_person_identifier,
    normalize_profile_urn,
    normalize_thread_id,
    person_profile_url,
    reply_thread_id,
)
from linkedin_mcp_server.linkedin.link_metadata import classify_link
from property_strategies import job_addresses, known_job_addresses, references

Normalizer = Callable[[str], str]
Builder = Callable[[str, str], str]


def _opaque(value: str) -> str:
    return normalize_opaque_id(value, field="job_id")


NORMALIZERS: dict[str, Normalizer] = {
    "person": normalize_person_identifier,
    "company": normalize_company_identifier,
    "job": normalize_job_id,
    "thread": normalize_thread_id,
    "reply_thread": reply_thread_id,
    "opaque": _opaque,
    "profile_urn": normalize_profile_urn,
}
IDEMPOTENT = (
    "person",
    "company",
    "job",
    "thread",
    "reply_thread",
    "opaque",
    "profile_urn",
)
BUILT: dict[str, tuple[Normalizer, Builder, str]] = {
    "person": (normalize_person_identifier, person_profile_url, "/in/"),
    "company": (normalize_company_identifier, company_page_url, "/company/"),
    "job": (normalize_job_id, job_view_url, "/jobs/view/"),
    "thread": (normalize_thread_id, messaging_thread_url, "/messaging/thread/"),
}
_SLUG = st.from_regex(r"[a-z0-9][a-z0-9-]{1,29}", fullmatch=True)
_JOB_ID = st.integers(1, 10**12).map(str)
_THREAD_ID = st.from_regex(r"2-[A-Za-z0-9_=-]{1,40}", fullmatch=True)
_URN_ID = st.from_regex(r"ACoA[A-Za-z0-9_-]{1,40}", fullmatch=True)
_HOST_SHAPED = st.builds(
    "{}{}".format,
    st.sampled_from(("", "x.", "de.", "www.", "acme-")),
    st.sampled_from(
        ("linkedin.com", "lnkd.in", "LinkedIn.com.", "acme.io", "acme", "linkedin")
    ),
)
_DOT_SEGMENTS = st.sampled_from((".", "..", "%2e", "%2E%2e"))
WELL_FORMED: dict[str, st.SearchStrategy[str]] = {
    "person": _SLUG,
    "company": st.one_of(
        _SLUG,
        _HOST_SHAPED,
        st.builds("/company/{}/".format, _HOST_SHAPED),
    ),
    "job": _JOB_ID,
    "thread": _THREAD_ID,
    "reply_thread": _THREAD_ID,
    "opaque": _JOB_ID,
    "profile_urn": st.one_of(
        _URN_ID, _URN_ID.map(lambda urn: f"urn:li:fsd_profile:{urn}")
    ),
}


def _reference(data: st.DataObject, name: str) -> str:
    return data.draw(st.one_of(references, WELL_FORMED[name], _DOT_SEGMENTS))


def _accepted(normalize: Normalizer, value: str) -> str | None:
    try:
        result = normalize(value)
    except InvalidReferenceError:
        return None
    assert isinstance(result, str)
    return result


@pytest.mark.parametrize("name", NORMALIZERS)
@given(value=references)
def test_a_reference_is_repaired_or_refused_as_invalid(name: str, value: str) -> None:
    _accepted(NORMALIZERS[name], value)


@pytest.mark.parametrize("name", IDEMPOTENT)
@given(data=st.data())
def test_a_repaired_reference_repairs_to_itself(name: str, data: st.DataObject) -> None:
    once = _accepted(NORMALIZERS[name], _reference(data, name))
    if once is None:
        return

    assert _accepted(NORMALIZERS[name], once) == once


SHORT_LINK_THREAD_IDS = (
    "lnkd%2ein",
    "lnkd%2Ein",
    "lnkd.in%2e",
    "%6cnkd.in",
    "lnkd.in%2elnkd.in",
    "/messaging/thread/lnkd.in/",
)


@pytest.mark.parametrize("name", IDEMPOTENT)
@pytest.mark.parametrize("value", SHORT_LINK_THREAD_IDS)
def test_a_host_shaped_reference_repairs_to_itself(name: str, value: str) -> None:
    once = _accepted(NORMALIZERS[name], value)
    if once is None:
        return

    assert _accepted(NORMALIZERS[name], once) == once


@pytest.mark.parametrize("value", SHORT_LINK_THREAD_IDS)
def test_a_host_shaped_thread_id_is_refused_as_a_short_link(value: str) -> None:
    with pytest.raises(InvalidReferenceError, match="shortened LinkedIn link"):
        normalize_thread_id(value)


@pytest.mark.parametrize("name", BUILT)
@given(data=st.data(), suffix=st.sampled_from(("", "/", "/details/experience/")))
def test_a_built_url_names_the_reference_it_was_built_from(
    name: str, data: st.DataObject, suffix: str
) -> None:
    normalize, build, route = BUILT[name]
    reference = _accepted(normalize, _reference(data, name))
    if reference is None:
        return

    url = build(reference, suffix)
    path = urlparse(url).path

    assert is_linkedin_landing(url)
    assert path.startswith(route) and path.endswith(suffix)
    assert "/" not in path[len(route) : len(path) - len(suffix)]
    assert urlparse(url).query == urlparse(url).fragment == ""
    assert normalize(url) == normalize(path) == reference


@given(address=job_addresses())
def test_a_job_link_names_the_same_job_as_input_and_as_reference(address: str) -> None:
    classified = classify_link(address)
    reference = classified[1] if classified and classified[0] == "job" else None
    job_id = _accepted(normalize_job_id, address)

    assert reference == (None if job_id is None else f"/jobs/view/{job_id}/")


@given(known=known_job_addresses())
def test_a_job_address_names_the_job_it_was_written_for(known: tuple[str, str]) -> None:
    address, job_id = known

    assert normalize_job_id(address) == job_id
    assert classify_link(address) == ("job", f"/jobs/view/{job_id}/")


@given(
    selections=st.lists(
        st.sampled_from(("", "7", "4252026496", "12a", "\u0664\u0662\u0665", " ")),
        max_size=3,
    ),
    key=st.sampled_from(("currentJobId", "current%4AobId")),
    lead=st.sampled_from(("", "keywords=python&", "currentJobID=9&")),
)
def test_a_search_query_selects_a_job_only_through_exactly_one_selection(
    selections: list[str], key: str, lead: str
) -> None:
    query = lead + "&".join(f"{key}={quote(value)}" for value in selections)
    named = [value for value in selections if value]
    expected = named[0] if len(named) == 1 and named[0] in ("7", "4252026496") else None

    assert job_id_from_search_query(query) == expected


@given(slug=_HOST_SHAPED, tail=st.sampled_from(("", "/", "/about/")))
def test_a_company_address_names_its_slug_whatever_the_slug_resembles(
    slug: str, tail: str
) -> None:
    address = f"https://www.linkedin.com/company/{slug}{tail}"

    assert normalize_company_identifier(address) == slug
    assert normalize_company_identifier(slug) == slug
