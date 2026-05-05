from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.scraping.extractor import LinkedInExtractor


class FakePage:
    def __init__(self):
        self.evaluate = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.url = "https://www.linkedin.com/jobs/view/4252026496/"
        self.keyboard = AsyncMock()


@pytest.fixture
def extractor(monkeypatch):
    page = FakePage()
    instance = LinkedInExtractor(page)
    instance._navigate_to_page = AsyncMock()
    monkeypatch.setattr(
        "linkedin_mcp_server.scraping.extractor.detect_rate_limit", AsyncMock()
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.scraping.extractor.handle_modal_close", AsyncMock()
    )
    return instance


async def test_open_easy_apply_dialog_passes_locale_terms(extractor):
    extractor._page.evaluate = AsyncMock(
        side_effect=[
            {"has_easy_apply": True, "already_applied": False},
            True,
        ]
    )

    result = await extractor._open_easy_apply_dialog("4252026496")

    assert result["ok"] is True
    _, state_kwargs = extractor._page.evaluate.call_args_list[0].args
    assert state_kwargs["labels"]["easy_apply"] == ["Easy Apply"]
    assert state_kwargs["labels"]["applied"] == ["Applied"]


async def test_save_job_passes_locale_terms(extractor):
    extractor._page.evaluate = AsyncMock(
        side_effect=[
            {"found": True, "pressed": False, "label": "Save"},
            True,
            True,
        ]
    )

    result = await extractor.save_job("4252026496", confirm_send=True)

    assert result["status"] == "saved"
    _, state_kwargs = extractor._page.evaluate.call_args_list[0].args
    assert state_kwargs["labels"]["save"] == ["Save"]
    assert state_kwargs["labels"]["saved"] == ["Saved"]


async def test_easy_apply_submit_returns_unconfirmed_when_linkedin_does_not_confirm(
    extractor,
):
    extractor._open_easy_apply_dialog = AsyncMock(return_value={"ok": True})
    extractor._inspect_easy_apply_dialog = AsyncMock(
        return_value={"questions": [], "step_count": 1, "submit_button": True}
    )
    extractor._click_easy_apply_submit = AsyncMock(return_value=True)
    extractor._confirm_easy_apply_submission = AsyncMock(return_value=False)
    extractor._dismiss_dialog = AsyncMock()

    result = await extractor.easy_apply_submit("4252026496", confirm_send=True)

    assert result["status"] == "submit_unconfirmed"
    assert "sent" not in result


async def test_easy_apply_submit_requires_textual_submit_button(extractor):
    extractor._page.evaluate = AsyncMock(
        return_value={"step_count": 1, "questions": [], "submit_button": False}
    )

    await extractor._inspect_easy_apply_dialog()

    script = extractor._page.evaluate.call_args.args[0]
    assert "b.type === 'submit'" not in script


async def test_click_easy_apply_submit_requires_textual_submit_button(extractor):
    extractor._page.evaluate = AsyncMock(return_value=False)

    await extractor._click_easy_apply_submit()

    script = extractor._page.evaluate.call_args.args[0]
    assert "b.type === 'submit'" not in script
