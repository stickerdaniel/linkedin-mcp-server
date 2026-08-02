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
# A company page renders its own logo large and every other company small,
# exactly as a profile does with member photos.
COMPANY_SUBJECT_LOGO = (
    "https://media.licdn.com/dms/image/v2/D4D0BAQGZ3dq_qonY0w"
    "/company-logo_200_200/B4DZpFeaQeGgAQ-/0/1762102192036/nimbus_logo"
)
ARTICLE_IMAGE = (
    "https://media.licdn.com/dms/image/v2/D4E10AQFvKaHallTUrw"
    "/articleshare-shrink_800/B4EZy0cQDzJcAQ-/0/1772553831073"
)
STATIC_ICON = "https://static.licdn.com/aero-v1/sc/h/icon.svg"


def cdn(kind: str, size: int, n: int = 0) -> str:
    """A CDN URL for one kind at one size variant."""
    return f"https://media.licdn.com/dms/image/v2/X{n}/{kind}_{size}_{size}/y/0"


PHOTO = "profile-displayphoto-shrink"
LOGO = "company-logo"


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


@pytest.mark.parametrize(
    "src",
    [OTHER_MEMBER, COMPANY_LOGO, COVER, POST_IMAGE, STATIC_ICON],
    ids=["other-member", "company-logo", "cover", "post-image", "static-icon"],
)
def test_rejects_everything_that_is_not_the_subject(src: str) -> None:
    assert build_image_references([RawImage(src=src, alt="x")], "main_profile") == []


def test_returns_the_company_logo_on_a_company_page() -> None:
    [ref] = build_image_references(
        [RawImage(src=COMPANY_SUBJECT_LOGO, alt="Nimbus Structure GmbH logo")],
        "main_company",
    )
    assert ref["url"] == COMPANY_SUBJECT_LOGO
    # context says which kind it is, so a caller need not parse the URL back.
    assert ref["context"] == "company logo"
    assert ref["text"] == "Nimbus Structure GmbH logo"


def test_picks_the_subject_out_of_a_whole_company_page() -> None:
    """Live shape: the page's own logo large, everyone else's small."""
    page: list[RawImage] = [
        {"src": OTHER_MEMBER, "alt": "Alexander Sanchez de la Cerda"},
        {"src": COMPANY_SUBJECT_LOGO, "alt": "Nimbus Structure GmbH logo"},
        {"src": COMPANY_LOGO, "alt": "ChatGPT page logo"},
        {"src": ARTICLE_IMAGE, "alt": ""},
    ]
    assert [r["url"] for r in build_image_references(page, "main_company")] == [
        COMPANY_SUBJECT_LOGO
    ]


def test_a_person_photo_is_still_labelled_a_profile_photo() -> None:
    [ref] = build_image_references([RawImage(src=SUBJECT)], "main_profile")
    assert ref["context"] == "profile photo"


@pytest.mark.parametrize("src", [ARTICLE_IMAGE], ids=["article-image"])
def test_rejects_article_images(src: str) -> None:
    assert build_image_references([RawImage(src=src, alt="x")], "main_company") == []


@pytest.mark.parametrize(
    "src",
    [
        "",
        "   ",
        "/relative.png",
        "data:image/gif;base64,R0lGOD",
        "https://example.com/a.jpg",
    ],
)
def test_rejects_anything_off_the_media_cdn(src: str) -> None:
    assert build_image_references([RawImage(src=src)], "main_profile") == []


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
    assert build_image_references([{"src": cdn(LOGO, 100)}], "main_profile") == []
    assert build_image_references([{"src": cdn(PHOTO, 100)}], "main_company") == []
    # But a lone image above thumbnail size is the subject of a sparse page.
    assert build_image_references([{"src": cdn(LOGO, 200)}], "main_company")


def test_each_kind_is_judged_separately() -> None:
    """A member page carries employer logos; a company page carries avatars.

    Comparing a photo against a logo would let one suppress the other.
    """
    member_page: list[RawImage] = [
        {"src": cdn(PHOTO, 800, 1)},
        {"src": cdn(PHOTO, 100, 2)},
        {"src": cdn(LOGO, 100, 3)},
    ]
    assert [
        r["context"] for r in build_image_references(member_page, "main_profile")
    ] == ["profile photo"]

    company_page: list[RawImage] = [
        {"src": cdn(PHOTO, 100, 1)},
        {"src": cdn(LOGO, 200, 2)},
        {"src": cdn(LOGO, 100, 3)},
    ]
    assert [
        r["context"] for r in build_image_references(company_page, "main_company")
    ] == ["company logo"]


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

    [logo] = build_image_references([RawImage(src=COMPANY_SUBJECT_LOGO)], "posts")
    assert logo["context"] == "company logo"


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
