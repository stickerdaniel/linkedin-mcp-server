"""The local network store and the Connections.csv reader."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.network import NetworkStore, parse_connections_csv
from linkedin_mcp_server.linkedin.my_connections import PageConnection
from linkedin_mcp_server.network.store import default_store_path

EXPORT = """\ufeffNotes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,ada@example.org,Analytical Engines,Head of Policy,05 Mar 2024
Alan,Turing,https://www.linkedin.com/in/alan-turing,,University of Manchester,Reader in AI Regulation,12 Jan 2023
Hidden,Person,,,Acme,Program Officer,01 Feb 2021
"""


@pytest.fixture
def store(tmp_path):
    s = NetworkStore(tmp_path / "network.db")
    yield s
    s.close()


def test_csv_header_is_found_after_the_notes_preamble():
    rows = parse_connections_csv(EXPORT)
    assert [r.slug for r in rows] == [
        "ada-lovelace",
        "alan-turing",
        "csv:hidden-person",
    ]
    assert rows[0].email == "ada@example.org"
    assert rows[0].connected_on == "2024-03-05"
    assert rows[1].email is None


def test_csv_without_a_header_is_refused():
    with pytest.raises(ValueError, match="Connections.csv"):
        parse_connections_csv("name,email\nx,y\n")


def test_csv_is_read_from_a_path(tmp_path):
    f = tmp_path / "Connections.csv"
    f.write_text(EXPORT, encoding="utf-8")
    assert len(parse_connections_csv(f)) == 3


def test_page_and_export_rows_merge_without_losing_either(store):
    store.upsert_page_connections(
        [
            PageConnection(
                slug="ada-lovelace",
                name="Ada Lovelace",
                headline="Policy lead, AI governance",
                connected_text="Connected on March 5, 2024",
                connected_on="2024-03-05",
            )
        ]
    )
    counts = store.upsert_export_connections(parse_connections_csv(EXPORT))
    assert (counts.seen, counts.added) == (3, 2)

    ada = store.search("Ada")["results"][0]
    assert ada["headline"] == "Policy lead, AI governance"
    assert ada["company"] == "Analytical Engines"
    assert ada["email"] == "ada@example.org"
    assert ada["sources"] == ["page", "export"]

    # A later page read with an empty headline does not erase the stored one.
    store.upsert_page_connections(
        [PageConnection("ada-lovelace", "Ada Lovelace", None, None, None)]
    )
    assert store.search("Ada")["results"][0]["headline"] == "Policy lead, AI governance"


def test_search_ranks_role_words_and_matches_prefixes(store):
    store.upsert_export_connections(parse_connections_csv(EXPORT))
    found = store.search("regul")
    assert [r["slug"] for r in found["results"]] == ["alan-turing"]
    assert store.search("policy OR -drop:")["total"] == 0  # operators are words


def test_search_filters_and_newest_first_without_a_query(store):
    store.upsert_export_connections(parse_connections_csv(EXPORT))
    assert [r["slug"] for r in store.search()["results"]] == [
        "ada-lovelace",
        "alan-turing",
        "csv:hidden-person",
    ]
    assert store.search(company="manchester")["total"] == 1
    assert store.search(connected_after="2023")["total"] == 2
    assert store.search(connected_before="2023")["total"] == 2
    assert store.search(connected_before="2022-12")["total"] == 1
    page = store.search(limit=1, offset=1)
    assert page["total"] == 3 and len(page["results"]) == 1


def test_overview_counts_sources_and_runs(store):
    run = store.start_run("export_csv")
    counts = store.upsert_export_connections(parse_connections_csv(EXPORT))
    store.finish_run(run, seen=counts.seen, added=counts.added, complete=True)
    o = store.overview()
    assert o["total"] == 3
    assert o["from_export"] == 3 and o["from_page"] == 0
    assert o["with_email"] == 1
    assert o["last_runs"]["export_csv"]["complete"] == 1
    assert {"year": "2024", "count": 1} in o["connections_per_year"]


def test_known_slugs_are_only_those_read_from_the_page(store):
    store.upsert_export_connections(parse_connections_csv(EXPORT))
    store.upsert_page_connections([PageConnection("p", "P", None, None, None)])
    assert store.known_slugs() == {"p"}


def test_store_path_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_NETWORK_DB", str(tmp_path / "x.db"))
    assert default_store_path() == tmp_path / "x.db"
