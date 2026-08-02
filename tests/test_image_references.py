# tests/test_image_references.py
"""Tests for ``image`` references — the profile subject's own photo.

``innerText`` extraction cannot see an ``<img>``: an image contributes no text,
so the member photo was the one thing on the top card that never reached the
caller.

Every fixture URL below is copied from a live profile dump (2026-08-02), which
is where the rule comes from: LinkedIn's media CDN names both the kind of image
and its size variant in the path, and that is the only stable discriminator.
Rendered size is not usable — extraction runs before the image is laid out, so
the subject's photo reports 0x0 as often as not — and the class names are
hashed and change between deploys.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.link_metadata import (
    RawImage,
    build_image_references,
)

# The subject, from a large variant.
SUBJECT = (
    "https://media.licdn.com/dms/image/v2/C4E03AQHaoqb8h-ev4w"
    "/profile-displayphoto-shrink_800_800/profile-displayphoto-shrink_800_800/0"
)
# Everyone else on the page — post authors, mutual connections, suggestions.
OTHER_MEMBER = (
    "https://media.licdn.com/dms/image/v2/D5603AQExSlVQUDCavQ"
    "/profile-displayphoto-scale_100_100/B56Zy_XQAyHQAc-/0/1772737086600"
)
COMPANY_LOGO = (
    "https://media.licdn.com/dms/image/v2/D4D0BAQEE_my7WpYL7g"
    "/company-logo_100_100/B4DZpFeaQeGgAQ-/0/1762102192036/geofoundationai_logo"
)
COVER = (
    "https://media.licdn.com/dms/image/v2/D4D16AQE71FRdhqtCAA"
    "/profile-displaybackgroundimage-shrink_200_800/B4DZpPiJ_JIMAU-/0/176227094"
)
POST_IMAGE = (
    "https://media.licdn.com/dms/image/v2/D5622AQH1rhjR5nc6nw"
    "/feedshare-shrink_480/B56Z7XNJhOJoAg-/0/1781727009179"
)
STATIC_ICON = "https://static.licdn.com/aero-v1/sc/h/icon.svg"


def cdn(kind: str, size: int, n: int = 0) -> str:
    """A CDN URL for one kind at one size variant."""
    return f"https://media.licdn.com/dms/image/v2/X{n}/{kind}_{size}_{size}/y/0"


PHOTO = "profile-displayphoto-shrink"


def test_returns_the_subject_photo() -> None:
    [ref] = build_image_references([RawImage(src=SUBJECT, alt="")], "main_profile")
    assert ref == {"kind": "image", "url": SUBJECT, "context": "profile photo"}


def test_picks_the_subject_out_of_a_whole_page() -> None:
    """A real profile carries ~42 images; exactly one is the subject."""
    page: list[RawImage] = [
        {"src": COVER, "alt": "Cover photo"},
        {"src": OTHER_MEMBER, "alt": "View Austin Cruz’s profile"},
        {"src": COMPANY_LOGO, "alt": ""},
        {"src": SUBJECT, "alt": ""},
        {"src": POST_IMAGE, "alt": "View image"},
        {"src": STATIC_ICON, "alt": ""},
    ]
    assert [r["url"] for r in build_image_references(page, "main_profile")] == [SUBJECT]


def test_the_subject_is_whoever_is_largest_not_whoever_clears_a_number() -> None:
    """The rule compares the page against itself, so no size is hard-coded.

    A fixed threshold breaks in both directions: it misses a subject rendered
    smaller than expected, and it promotes a stranger the moment LinkedIn
    renders one card larger. Both are covered here.
    """
    # Subject well below any plausible constant, still found.
    small: list[RawImage] = [{"src": cdn(PHOTO, 100, 1)}, {"src": cdn(PHOTO, 128, 2)}]
    assert [r["url"] for r in build_image_references(small, "main_profile")] == [
        cdn(PHOTO, 128, 2)
    ]

    # A second card at 200 does not become the subject when a larger one exists.
    big: list[RawImage] = [{"src": cdn(PHOTO, 200, 1)}, {"src": cdn(PHOTO, 800, 2)}]
    assert [r["url"] for r in build_image_references(big, "main_profile")] == [
        cdn(PHOTO, 800, 2)
    ]


def test_returns_nothing_when_no_candidate_stands_out() -> None:
    """A search-results page is all thumbnails; none of them is a subject."""
    page: list[RawImage] = [{"src": cdn(PHOTO, 100, i)} for i in range(8)]
    assert build_image_references(page, "search_results") == []


def test_a_lone_thumbnail_is_not_a_subject() -> None:
    """With nothing to compare against, size is the only evidence left.

    One employer logo on a member profile, or one visitor avatar on a company
    page, is a thumbnail — not the thing the page is about.
    """
    assert build_image_references([{"src": cdn(PHOTO, 100)}], "main_profile") == []
    # But a lone image above thumbnail size is the subject of a sparse page.
    assert build_image_references([{"src": cdn(PHOTO, 400)}], "main_profile")


def test_does_not_depend_on_rendered_size() -> None:
    """The whole reason the rule reads the path: a not-yet-laid-out image is 0x0."""
    assert build_image_references(
        [{"src": SUBJECT, "width": 0, "height": 0}], "main_profile"
    )


def test_keeps_alt_when_present_and_omits_it_when_blank() -> None:
    [with_alt] = build_image_references(
        [{"src": SUBJECT, "alt": "Konstantin Gerner"}], "main_profile"
    )
    assert with_alt["text"] == "Konstantin Gerner"
    [blank] = build_image_references([RawImage(src=SUBJECT, alt="   ")], "main_profile")
    assert "text" not in blank


def test_the_same_image_rendered_twice_is_one_reference() -> None:
    """LinkedIn repeats the top-card photo in the sticky header.

    Deduping has to happen before the subject is chosen — counting the copies
    as separate same-sized candidates would make the subject look ambiguous.
    """
    page: list[RawImage] = [
        {"src": cdn(PHOTO, 800)},
        {"src": cdn(PHOTO, 800)},
        {"src": cdn(PHOTO, 100, 2)},
    ]
    assert [r["url"] for r in build_image_references(page, "main_profile")] == [
        cdn(PHOTO, 800)
    ]


def test_caps_the_number_returned() -> None:
    page: list[RawImage] = [{"src": cdn(PHOTO, 800, i)} for i in range(10)]
    page.append({"src": cdn(PHOTO, 100, 99)})
    assert len(build_image_references(page, "main_profile", cap=4)) == 4


def test_context_names_the_kind_not_the_section() -> None:
    """``context`` says what the image is, not where it was found.

    The section is already the key this reference is filed under, so repeating
    it carries nothing; which *kind* of image it is cannot be recovered without
    parsing the CDN path back out of the URL.
    """
    [photo] = build_image_references([RawImage(src=SUBJECT)], "experience")
    assert photo["context"] == "profile photo"


class TestAgainstARealDom:
    """The collection JS, run against synthetic HTML in headless chromium.

    Mirrors the live top card: the subject's photo, a suggested member, a
    company logo and the cover — all from the same CDN, so nothing but the path
    tells them apart.
    """

    pytestmark = pytest.mark.browser_dom

    PAGE = f"""
    <main>
      <section class="topcard">
        <h1>Konstantin Gerner</h1>
        <img src="{COVER}" alt="Cover photo">
        <img src="{SUBJECT}" alt="">
        <img src="{COMPANY_LOGO}" alt="">
      </section>
      <section class="activity">
        <img src="{OTHER_MEMBER}" alt="View Austin Cruz’s profile">
        <img src="{POST_IMAGE}" alt="View image">
      </section>
    </main>
    """

    @pytest.mark.asyncio
    async def test_collects_the_subject_photo_from_a_rendered_page(self) -> None:
        from patchright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            await page.set_content(self.PAGE)
            images = await page.evaluate(
                """() => {
                    const normalize = v => (v || '').replace(/\\s+/g, ' ').trim();
                    return Array.from(document.querySelectorAll('img[src]')).map(img => ({
                        src: (img.currentSrc || img.src || '').trim(),
                        alt: normalize(img.getAttribute('alt')),
                    })).filter(i => i.src);
                }"""
            )
            await browser.close()

        assert [r["url"] for r in build_image_references(images, "main_profile")] == [
            SUBJECT
        ]
