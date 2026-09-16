"""Browser-DOM tests for the post-detail body program.

The unit suite mocks ``page.evaluate``, so ``_POST_DETAIL_JS`` never runs
there: a scripted payload asserts the Python around it and nothing about the
selection it performs. That selection is the whole point of the program — the
detail page's innerText is author header, follow and translate controls,
reaction bar and the full comment thread, and the body is a fraction of it.

The markup below reproduces the attribute skeleton measured live on
2026-09-16 (``[role="article"][data-urn="urn:li:activity:…"]`` around the
post, ``[data-urn="urn:li:comment:(…)"]`` around each comment, ``dir`` on
user-authored text, author and link-preview text inside anchors). It is a
claim about LinkedIn, and it is the claim the program depends on.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.posts import _POST_DETAIL_JS

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

POST_URN = "urn:li:activity:7490677621786214400"

DETAIL_HTML = f"""
<main>
  <div aria-label="Feld aktualisieren">
    <section>
      <div data-view-name="feed-full-update">
        <div role="article" data-urn="{POST_URN}">
          <a href="/in/hagen-huebel/" aria-label="Ansehen: Hagen Hübel">
            <span dir="ltr">Hagen Hübel</span>
            <span dir="ltr">Agentic Workflows &amp; Coding</span>
          </a>
          <a href="https://www.nestfainder.ai/get-started">Zur Website</a>
          <button aria-label="Hagen Hübel folgen">Folgen</button>
          <div dir="ltr"><span dir="ltr">Don't tell me software engineering is dead.<br><br>That's Uncle Bob, yesterday.<br><a href="/feed/hashtag/agenticwork"><span class="visually-hidden" style="position:absolute;clip:rect(0 0 0 0);width:1px;height:1px;overflow:hidden">Hashtag</span>#agenticwork</a></span></div>
          <button>Übersetzung anzeigen</button>
          <a href="https://addyo.substack.com/p/brownfield">
            <span dir="ltr">Brownfield Agentic Engineering</span>
            <span dir="ltr">addyo.substack.com</span>
          </a>
          <img src="https://media.licdn.com/dms/image/v2/feedshare-shrink_800/post" />
          <section>Reaktionen +56</section>
          <button aria-label="Mit „Gefällt mir“ reagieren">Gefällt mir</button>
          <div>
            <article data-id="urn:li:comment:({POST_URN},7490989716519927808)">
              <a href="/in/tom-le/"><span dir="ltr">Tom Le</span></a>
              <div dir="ltr"><span dir="ltr">The issue isn't trust but fragility.</span></div>
              <img src="https://media.licdn.com/dms/image/v2/comment-image-shrink_8192_480/c" />
              <a href="https://sites.google.com/a/scrumplop.org/patlets">patlets</a>
            </article>
          </div>
        </div>
      </div>
    </section>
  </div>
</main>
"""

# A reshare, with the quoted post wrapped in an anchor to its own permalink
# and the quoted author card as a separate profile anchor inside it — the
# shape measured on 2026-09-16.
RESHARE_HTML = """
<main>
  <div role="article" data-urn="urn:li:activity:111">
    <a href="/in/markus/"><span dir="ltr">Markus Andrezak</span></a>
    <div dir="ltr">When Cees writes about AI augmented SW engineering, I read.</div>
    <a href="/in/cees/"><span dir="ltr">Cees de Groot</span></a>
    <a href="/feed/update/urn:li:activity:222/">
      <div dir="ltr">After some intensive Claude Code work, time to jot down my thoughts.</div>
    </a>
    <a href="/pulse/ai-is-a-great-amplifier/">
      <span dir="ltr">AI is a great amplifier - of everything</span>
    </a>
  </div>
</main>
"""


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


class TestPostDetailProgram:
    async def test_the_body_is_read_without_header_controls_or_comments(self, dom_page):
        """Everything the page says that the post did not say is dropped.

        The premise is asserted rather than assumed: the same document's
        page text carries the author, the follow and translate buttons, the
        reaction bar and the comment, which is exactly why reading
        ``<main>`` was not good enough.
        """
        await dom_page.set_content(DETAIL_HTML)

        payload = await dom_page.evaluate(_POST_DETAIL_JS, {"urn": POST_URN})

        assert payload["scoped"] is True
        assert payload["text"] == (
            "Don't tell me software engineering is dead.\n\n"
            "That's Uncle Bob, yesterday.\n\nHashtag\n#agenticwork"
        )
        # The hashtag's screen-reader prefix is reported rather than removed
        # here: the caller drops those lines, and the word is localized.
        assert payload["hidden_labels"] == ["Hashtag"]
        for chrome in (
            "Hagen Hübel",
            "Folgen",
            "Übersetzung anzeigen",
            "Gefällt mir",
            "addyo.substack.com",
            "The issue isn't trust but fragility.",
        ):
            assert chrome in payload["page_text"]
            assert chrome not in payload["text"]

    async def test_media_and_anchors_come_from_the_post_not_its_surroundings(
        self, dom_page
    ):
        """A commenter's attachment, and the author's own website, are not it.

        Scoping the text but not the media would still hand a digest the
        wrong image and a stranger's link. The website button is the subtler
        half: it is external, it sits inside the post element, and only its
        position above the body says the post is not pointing at it.
        """
        await dom_page.set_content(DETAIL_HTML)

        payload = await dom_page.evaluate(_POST_DETAIL_JS, {"urn": POST_URN})

        # The program hands back raw URLs; the media and external filters
        # live in Python. What it decides here is whose URLs these are.
        assert payload["images"] == [
            "https://media.licdn.com/dms/image/v2/feedshare-shrink_800/post"
        ]
        assert "https://addyo.substack.com/p/brownfield" in payload["links"]
        assert not any("scrumplop" in link for link in payload["links"])
        assert not any("nestfainder" in link for link in payload["links"])

    async def test_a_reshare_keeps_the_quoted_body(self, dom_page):
        """The quoted post is content, and it lives under the same URN.

        Taking only the first block would drop the piece the resharer is
        pointing at, which is usually the part worth reading.
        """
        await dom_page.set_content(RESHARE_HTML)

        payload = await dom_page.evaluate(
            _POST_DETAIL_JS, {"urn": "urn:li:activity:111"}
        )

        assert payload["text"] == (
            "When Cees writes about AI augmented SW engineering, I read.\n\n"
            "After some intensive Claude Code work, time to jot down my thoughts."
        )
        # The quoted author's card and an article card are anchors too, and
        # neither is body text — only the reshare wrapper's /feed/update/
        # target earns the exception.
        assert "Cees de Groot" not in payload["text"]
        assert "AI is a great amplifier" not in payload["text"]

    async def test_a_page_without_the_requested_urn_reports_no_scope(self, dom_page):
        """An article page has no activity element, and must say so.

        Answering with a body pulled from whatever else carried ``dir``
        would look like a successful read of the wrong text; reporting no
        scope is what sends the reader to its page-text fallback.
        """
        await dom_page.set_content(DETAIL_HTML)

        payload = await dom_page.evaluate(
            _POST_DETAIL_JS, {"urn": "urn:li:activity:999"}
        )

        assert payload["scoped"] is False
        assert payload["text"] == ""
        assert "Uncle Bob" in payload["page_text"]
