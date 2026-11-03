"""Browser-DOM tests for the 2nd-degree search-card program.

The program decides which links are results and which are the member's
mutual connections, and where one card ends, entirely in JavaScript, so it
runs here in headless chromium against a synthetic results page. The fixture
uses no LinkedIn class names: the program matches none.

Skipped automatically when chromium is not installed.
"""

from __future__ import annotations

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.linkedin.introductions import _SECOND_DEGREE_CARDS_JS
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

URL = "https://www.linkedin.com/search/results/people/?keywords=ai"


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


def result(slug: str, name: str, headline: str, mutual: list[tuple[str, str]]) -> str:
    links = " &amp; ".join(f'<a href="/in/{s}/">{n}</a>' for s, n in mutual)
    return f"""
    <li><div>
      <a href="/in/{slug}/"><img alt=""></a>
      <div>
        <a href="https://www.linkedin.com/in/{slug}/"><span>{name}</span><span> • 2nd</span></a>
        <div>{headline}</div>
        <div>Current: Lead at Acme</div>
        <button>Connect</button>
        <p>{links} {"is a" if len(mutual) == 1 else "are"} mutual connection{"" if len(mutual) == 1 else "s"}</p>
      </div>
    </div></li>"""


async def load(page: Page, items: list[str]) -> None:
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"></head>
<body><nav><a href="/in/member/">Me</a></nav><main><ul>{"".join(items)}</ul></main></body></html>"""
    await page.route(
        "https://www.linkedin.com/**",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    await page.goto(URL)


async def test_each_result_gets_its_own_mutual_connections(dom_page):
    await load(
        dom_page,
        [
            result(
                "koen",
                "Koen van Kan",
                "AI Governance Consultant",
                [("nicky", "Nicky Dries")],
            ),
            result(
                "cristina",
                "Cristina Muresan",
                "AI Researcher",
                # A mutual shared with the first result is credited to both.
                [("nicky", "Nicky Dries"), ("abdallah", "Abdallah Abdul-Salam")],
            ),
            result("salvi", "Salvi Jansen", "Technology Assurance", []),
        ],
    )
    cards = await PageSession(dom_page).run_on_linkedin(
        _SECOND_DEGREE_CARDS_JS, ["nicky", "abdallah", "someone-else"]
    )

    assert [(c["slug"], c["mutual"]) for c in cards] == [
        ("koen", ["nicky"]),
        ("cristina", ["nicky", "abdallah"]),
        ("salvi", []),
    ]
    first = cards[0]
    assert first["name"] == "Koen van Kan"  # the degree badge is cut off
    assert "AI Governance Consultant" in first["lines"]
    assert "Connect" not in first["lines"]
    # A mutual connection's own link never becomes a result.
    assert "nicky" not in [c["slug"] for c in cards]
