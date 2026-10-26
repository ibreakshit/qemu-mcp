"""Pin the explicit tool annotations clients receive over MCP stdio."""
import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from test_stdio_handshake import EXPECTED_TOOLS, parameters

READ_ONLY = {'qemu_list', 'qemu_status', 'qemu_screenshot', 'qemu_get_resolution', 'qemu_wait_screen'}


@pytest.mark.anyio
async def test_every_tool_declares_its_four_hints():
    async with stdio_client(parameters()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        annotations = tool.annotations
        assert annotations is not None
        expected = (name in READ_ONLY, name not in READ_ONLY | {'qemu_attach'}, name in READ_ONLY | {'qemu_attach', 'qemu_release_all'}, False)
        actual = (annotations.read_only_hint, annotations.destructive_hint, annotations.idempotent_hint, annotations.open_world_hint)
        assert actual == expected, name
