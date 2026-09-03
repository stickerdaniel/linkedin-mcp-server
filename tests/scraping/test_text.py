"""Tests for LinkedIn innerText cleanup."""

from linkedin_mcp_server.scraping.text import (
    strip_conversation_chrome,
    strip_linkedin_noise,
    truncate_linkedin_noise,
)


class TestStripLinkedInNoise:
    def test_strips_footer(self):
        text = "Bill Gates\nChair, Gates Foundation\n\nAbout\nAccessibility\nTalent Solutions\nCareers"
        assert strip_linkedin_noise(text) == "Bill Gates\nChair, Gates Foundation"

    def test_strips_footer_with_talent_solutions_variant(self):
        text = "Profile content here\n\nAbout\nTalent Solutions\nMore footer"
        assert strip_linkedin_noise(text) == "Profile content here"

    def test_strips_sidebar_recommendations(self):
        text = "Experience\nCo-chair\nGates Foundation\n\nMore profiles for you\nSundar Pichai\nCEO at Google"
        assert strip_linkedin_noise(text) == "Experience\nCo-chair\nGates Foundation"

    def test_strips_premium_upsell(self):
        text = "Education\nHarvard University\n\nExplore premium profiles\nRandom Person\nSoftware Engineer"
        assert strip_linkedin_noise(text) == "Education\nHarvard University"

    def test_picks_earliest_marker(self):
        text = "Content\n\nExplore premium profiles\nStuff\n\nMore profiles for you\nMore stuff\n\nAbout\nAccessibility"
        assert strip_linkedin_noise(text) == "Content"

    def test_no_noise_returns_unchanged(self):
        text = "Clean content with no LinkedIn chrome"
        assert strip_linkedin_noise(text) == "Clean content with no LinkedIn chrome"

    def test_empty_string(self):
        assert strip_linkedin_noise("") == ""

    def test_truncate_noise_preserves_media_controls_for_rate_limit_detection(self):
        text = "Play\nLoaded: 100.00%\nRemaining time 0:07\nShow captions"
        assert truncate_linkedin_noise(text) == text
        assert strip_linkedin_noise(text) == ""

    def test_about_in_profile_content_not_stripped(self):
        """'About' followed by actual content (not 'Accessibility') should be preserved."""
        text = "About\nChair of the Gates Foundation.\n\nFeatured\nPost"
        assert (
            strip_linkedin_noise(text)
            == "About\nChair of the Gates Foundation.\n\nFeatured\nPost"
        )

    def test_real_footer_with_languages(self):
        text = (
            "Company info\n\n"
            "About\nAccessibility\nTalent Solutions\nCareers\n"
            "Select language\nEnglish (English)\nDeutsch (German)"
        )
        assert strip_linkedin_noise(text) == "Company info"

    def test_preserves_real_careers_content(self):
        text = "Careers\nWe're hiring globally.\nOpen roles in engineering and design."
        assert strip_linkedin_noise(text) == text

    def test_preserves_real_questions_content(self):
        text = "Questions?\nReach out to our recruiting team for details."
        assert strip_linkedin_noise(text) == text

    def test_strips_media_controls_lines(self):
        text = (
            "Feed post number 1\n"
            "Play\n"
            "Loaded: 100.00%\n"
            "Remaining time 0:07\n"
            "Playback speed\n"
            "Actual post content\n"
            "Show captions\n"
            "Close modal window"
        )
        assert strip_linkedin_noise(text) == "Feed post number 1\nActual post content"


class TestStripConversationChrome:
    THREAD = (
        "MAY 25\n"
        "Grace Hopper sent the following message at 5:27 PM\n"
        "Grace Hopper  5:27 PM\n"
        "\n"
        "Hello!"
    )
    PAGE = (
        "Messaging\n"
        "Search messages\n"
        "Compose a new message\n"
        "Inbox\n"
        "Attention screen reader users, messaging items continuously update.\n"
        "Ada Lovelace\n"
        "Jun 8\n"
        "Ada: Preview belonging to a different conversation\n"
        ". Press return to go to conversation details\n"
        "Open the options list in your conversation with Ada Lovelace and Grace Hopper\n"
        "Status is reachable\n"
        "Load more conversations\n"
        "Grace Hopper\n"
        "Status is online\n"
        "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
        + THREAD
        + "\n"
        "Maximize compose field\n"
        "Attach an image to your conversation with Grace Hopper\n"
        "Open GIF Keyboard\n"
        "Send\n"
        "Open send options"
    )

    def test_strips_sidebar_and_composer(self):
        assert strip_conversation_chrome(self.PAGE) == self.THREAD

    def test_other_conversation_previews_removed(self):
        assert "different conversation" not in strip_conversation_chrome(self.PAGE)
        assert "Ada Lovelace" not in strip_conversation_chrome(self.PAGE)

    def test_missing_composer_strips_only_leading_chrome(self):
        text = (
            "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
            + self.THREAD
        )
        assert strip_conversation_chrome(text) == self.THREAD

    def test_missing_thread_header_strips_only_composer(self):
        text = self.THREAD + "\nMaximize compose field\nOpen send options"
        assert strip_conversation_chrome(text) == self.THREAD

    def test_quoted_composer_string_in_message_survives(self):
        text = (
            "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
            "Maximize compose field\n"
            "is the label I keep seeing\n"
            "Maximize compose field\n"
            "Open send options"
        )
        assert (
            strip_conversation_chrome(text)
            == "Maximize compose field\nis the label I keep seeing"
        )

    def test_quoted_companion_with_suffix_does_not_confirm_composer(self):
        text = "Hello!\nMaximize compose field\nOpen send options is what I clicked"
        assert strip_conversation_chrome(text) == text

    def test_quoted_attach_text_does_not_confirm_composer(self):
        text = (
            "Hello!\n"
            "Maximize compose field\n"
            "Attach an image to your conversation with Grace is the label I clicked"
        )
        assert strip_conversation_chrome(text) == text

    def test_distant_companion_text_does_not_confirm_composer(self):
        filler = "\n".join(f"message {n}" for n in range(10))
        text = (
            "Maximize compose field\n"
            + filler
            + "\nOpen send options is what I clicked"
        )
        assert strip_conversation_chrome(text) == text

    def test_quoted_composer_without_companions_does_not_truncate(self):
        text = (
            "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
            "Hello!\n"
            "Maximize compose field\n"
            "is what the button says"
        )
        assert (
            strip_conversation_chrome(text)
            == "Hello!\nMaximize compose field\nis what the button says"
        )

    def test_quoted_thread_header_in_message_keeps_earlier_messages(self):
        text = (
            "Load more conversations\n"
            "Grace Hopper\n"
            "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
            "Hello!\n"
            "Open the options list in your conversation with is a label I quoted\n"
            "Bye!\n"
            "Maximize compose field\n"
            "Open send options"
        )
        assert strip_conversation_chrome(text) == (
            "Hello!\n"
            "Open the options list in your conversation with is a label I quoted\n"
            "Bye!"
        )

    def test_sidebar_end_without_thread_header_still_strips_sidebar(self):
        text = (
            "Ada: Preview belonging to a different conversation\n"
            "Load more conversations\n" + self.THREAD
        )
        assert strip_conversation_chrome(text) == self.THREAD

    def test_unknown_locale_returns_unchanged(self):
        assert strip_conversation_chrome(self.PAGE, locale="de") == self.PAGE

    def test_no_markers_returns_stripped_text(self):
        assert strip_conversation_chrome("Hello!\nHi there!") == "Hello!\nHi there!"

    def test_empty_string(self):
        assert strip_conversation_chrome("") == ""
