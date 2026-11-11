"""A tool registered with a longer timeout keeps it through the proxy.

The owner gives `sync_my_connections` longer than the shared tool timeout, so
both of the proxy's limits -- the frontend's call budget and the forwarder's
read timeout on the hop -- have to give it the same, or the proxy cuts the
call off first.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from linkedin_mcp_server.config.schema import (
    LONG_RUNNING_TOOL_SECONDS,
    SYNC_MY_CONNECTIONS_MAX_SECONDS,
    tool_timeout_for,
)
from linkedin_mcp_server.daemon.forwarder import OwnerForwarder
from linkedin_mcp_server.daemon.proxy import FrontendCallMiddleware, call_budget
from linkedin_mcp_server.server import _long_call_budgets, create_mcp_server


def test_a_long_tool_never_gets_less_than_the_configured_timeout():
    assert tool_timeout_for("sync_my_connections", 180.0) == (
        SYNC_MY_CONNECTIONS_MAX_SECONDS + 60.0
    )
    assert tool_timeout_for("sync_my_connections", 5000.0) == 5000.0
    assert tool_timeout_for("search_people", 180.0) == 180.0


async def test_the_registered_timeout_comes_from_the_same_table():
    tool = await create_mcp_server().get_tool("sync_my_connections")
    assert tool is not None
    assert tool.timeout == tool_timeout_for("sync_my_connections", 180.0)


def test_the_proxy_budgets_cover_every_long_tool():
    budgets = _long_call_budgets(180.0)
    assert set(budgets) == set(LONG_RUNNING_TOOL_SECONDS)
    assert budgets["sync_my_connections"] == call_budget(
        tool_timeout_for("sync_my_connections", 180.0)
    )


def test_the_frontend_budget_is_chosen_per_tool():
    middleware = FrontendCallMiddleware(
        MagicMock(), budget=210.0, budgets={"sync_my_connections": 990.0}
    )

    def context(name: str):
        return SimpleNamespace(message=SimpleNamespace(name=name))

    assert middleware._budget_for(context("sync_my_connections")) == 990.0  # type: ignore[arg-type]
    assert middleware._budget_for(context("search_people")) == 210.0  # type: ignore[arg-type]


async def test_the_forwarder_reads_with_the_tools_own_timeout():
    forwarder = OwnerForwarder(
        MagicMock(), timeout=210.0, timeouts={"sync_my_connections": 990.0}
    )
    result = SimpleNamespace(
        content=[], structured_content=None, meta=None, is_error=False
    )
    forwarder._exchange = AsyncMock(return_value=result)  # type: ignore[method-assign]

    await forwarder.call("sync_my_connections", {})
    await forwarder.call("search_people", {"keywords": "x"})

    timeouts = [c.kwargs["timeout"] for c in forwarder._exchange.await_args_list]
    assert timeouts == [990.0, 210.0]
