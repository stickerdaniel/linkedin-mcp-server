"""What the middleware records about a tool call before it queues it.

Nothing here mocks the lock wait away: the point is that a bunch's deadline
sees the time its call spent queued, and a test that only watched a code path
would still pass with the arrival stamped after the wait.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from linkedin_mcp_server.pacing import request_arrived_at
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)

# Long enough to sit well clear of scheduling noise, short enough that the
# suite does not crawl.
HOLD = 0.3


def _call_context(tool_name: str = "get_inbox") -> MagicMock:
    context = MagicMock()
    context.message.name = tool_name
    context.fastmcp_context = None
    return context


class TestArrivalIsRecordedBeforeTheQueue:
    """A bunch's deadline has to see the time its call spent queued.

    The tool timeout starts only once the middleware lets a call through, so a
    call queued behind another session's can already be past the frontend
    proxy's deadline when it starts. The middleware records the arrival for the
    tool to start its deadline from.
    """

    async def test_the_tool_sees_the_arrival_from_before_the_wait(self):
        middleware = SequentialToolExecutionMiddleware()
        seen: list[tuple[float | None, float]] = []

        async def hold_the_lock(context):
            await asyncio.sleep(HOLD)
            return None

        async def record(context):
            seen.append((request_arrived_at.get(), time.monotonic()))
            return None

        await asyncio.gather(
            middleware.on_call_tool(_call_context(), hold_the_lock),
            middleware.on_call_tool(_call_context(), record),
        )

        arrived, ran = seen[0]
        assert arrived is not None
        # The second call sat out the first one's hold between arrival and
        # running, so an arrival stamped after the wait would read as (almost)
        # now.
        assert ran - arrived >= HOLD * 0.8

    async def test_the_arrival_is_reset_after_the_call(self):
        middleware = SequentialToolExecutionMiddleware()

        await middleware.on_call_tool(_call_context(), AsyncMock())

        assert request_arrived_at.get() is None
