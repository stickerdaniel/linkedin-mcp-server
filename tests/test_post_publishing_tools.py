"""MCP registration contracts for personal-post publishing."""

from linkedin_mcp_server.server import create_mcp_server


async def test_the_server_registers_preview_and_publish_with_the_call_timeout():
    server = create_mcp_server(tool_timeout=17)
    tools = {tool.name: tool for tool in await server.list_tools()}

    for name in [
        "prepare_post",
        "publish_post",
    ]:
        assert name in tools
        assert tools[name].timeout == 17
    preview = tools["prepare_post"].annotations
    assert preview is not None
    assert preview.read_only_hint is True
    publishing = tools["publish_post"].annotations
    assert publishing is not None
    assert publishing.read_only_hint is False
    assert publishing.idempotent_hint is False
