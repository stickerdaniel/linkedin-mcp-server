"""Ranking introduction paths and reading the messages export."""

from __future__ import annotations

from datetime import date

import pytest

from linkedin_mcp_server.network.messages_import import parse_messages_csv
from linkedin_mcp_server.network.ranking import (
    organisations,
    rank_paths,
    shared_organisations,
    tie_strength,
)

TODAY = date(2026, 11, 3)

MESSAGES = """CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER
1,,Me,https://www.linkedin.com/in/member-x,Ada,https://www.linkedin.com/in/ada,2026-10-01 09:00:00 UTC,,hello,INBOX
1,,Ada,https://www.linkedin.com/in/ada,Me,https://www.linkedin.com/in/member-x,2026-10-02 09:00:00 UTC,,hi,INBOX
2,,Alan,https://www.linkedin.com/in/alan,Me,https://www.linkedin.com/in/member-x,2019-05-01 09:00:00 UTC,,old,INBOX
3,,Me,https://www.linkedin.com/in/member-x,Ada Grace,"https://www.linkedin.com/in/ada,https://www.linkedin.com/in/grace",2024-01-01 09:00:00 UTC,,group,INBOX
"""


def test_messages_are_counted_per_counterpart_and_the_member_is_inferred():
    stats = parse_messages_csv(MESSAGES)
    assert stats == {
        "ada": (3, "2026-10-02"),
        "alan": (1, "2019-05-01"),
        "grace": (1, "2024-01-01"),
    }


def test_a_file_that_is_not_the_messages_export_is_refused():
    with pytest.raises(ValueError, match="messages.csv"):
        parse_messages_csv("a,b\n1,2\n")


def test_a_closeness_mark_outweighs_message_history():
    marked, why = tie_strength({"level": 3}, TODAY)
    chatty, _ = tie_strength(
        {"messages": 10, "last_message": "2020-01-01", "connected_on": "2016-01-01"},
        TODAY,
    )
    assert marked > chatty
    assert why == ["you marked closeness 3/3"]


def test_recent_messages_beat_old_ones_and_dont_know_suppresses_proxies():
    recent, _ = tie_strength({"messages": 5, "last_message": "2026-09-01"}, TODAY)
    old, _ = tie_strength({"messages": 5, "last_message": "2019-09-01"}, TODAY)
    assert recent > old
    unknown, _ = tie_strength(
        {"level": 0, "messages": 30, "last_message": "2026-09-01"}, TODAY
    )
    assert unknown < old


def test_organisations_come_from_company_headline_and_experience():
    person = {
        "company": "KU Leuven",
        "headline": "Data & AI Strategist @ Datashift | Speaker",
    }
    assert organisations(person, "Experience\n\nMindfully\n\n2026 – Present") == [
        "KU Leuven",
        "Datashift",
        "Experience",
        "Mindfully",
    ]


def test_shared_organisation_is_found_on_the_target_card():
    intro = {"headline": "Professor at KU Leuven"}
    card = ["Koen", "Researcher", "Current: Postdoc at KU Leuven"]
    assert shared_organisations(intro, card) == ["KU Leuven"]
    assert shared_organisations({"headline": "Engineer at Acme"}, card) == []
    # A substring of a longer word is not a match.
    assert shared_organisations({"company": "Leuv"}, card) == []


def test_paths_are_ranked_by_the_warmest_introducer_with_reasons():
    results = [
        {
            "slug": "t1",
            "card": ["T1", "Current: Lead at Acme"],
            "introducers": [{"slug": "weak", "name": "Weak"}],
        },
        {
            "slug": "t2",
            "card": ["T2", "Current: Lead at Globex"],
            "introducers": [
                {"slug": "close", "name": "Close"},
                {"slug": "weak", "name": "Weak"},
            ],
        },
    ]
    ties = {"close": {"slug": "close", "level": 3}, "weak": {"slug": "weak"}}
    ranked = rank_paths(results, ties, {}, TODAY)
    assert [r["slug"] for r in ranked] == ["t2", "t1"]
    top = ranked[0]
    assert top["best_via"] == "close"
    assert top["reasons"][:2] == ["best path via Close", "you marked closeness 3/3"]
    assert "2 mutual connections you know" in top["reasons"]
    assert [i["slug"] for i in top["introducers"]] == ["close", "weak"]


def test_a_shared_organisation_lifts_an_otherwise_equal_path():
    results = [
        {
            "slug": "t1",
            "card": ["T1", "Current: Lead at Acme"],
            "introducers": [{"slug": "a", "name": "A", "company": "Globex"}],
        },
        {
            "slug": "t2",
            "card": ["T2", "Current: Lead at Globex"],
            "introducers": [{"slug": "b", "name": "B", "company": "Globex"}],
        },
    ]
    ranked = rank_paths(results, {}, {}, TODAY)
    assert ranked[0]["slug"] == "t2"
    assert "also at Globex" in ranked[0]["reasons"]
