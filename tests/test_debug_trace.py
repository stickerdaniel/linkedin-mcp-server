import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_record_page_trace_writes_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_DEBUG_TRACE", "1")
    monkeypatch.setattr("linkedin_mcp_server.debug_trace._TRACE_DIR", tmp_path / "traces")

    from linkedin_mcp_server.debug_trace import record_page_trace

    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn")
    page.evaluate = AsyncMock(return_value="Feed")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=locator)
    page.context.cookies = AsyncMock(return_value=[])
    page.screenshot = AsyncMock()

    await record_page_trace(page, "test-step")

    trace_file = tmp_path / "traces" / "trace.jsonl"
    assert trace_file.exists()
    payload = json.loads(trace_file.read_text().splitlines()[0])
    assert payload["step"] == "test-step"
    assert payload["url"] == "https://www.linkedin.com/feed/"


@pytest.mark.asyncio
async def test_record_page_trace_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("LINKEDIN_DEBUG_TRACE", raising=False)
    monkeypatch.setattr("linkedin_mcp_server.debug_trace._TRACE_DIR", tmp_path / "traces")

    from linkedin_mcp_server.debug_trace import record_page_trace

    page = MagicMock()
    await record_page_trace(page, "test-step")

    assert not (tmp_path / "traces" / "trace.jsonl").exists()
