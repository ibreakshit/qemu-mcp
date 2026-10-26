"""Real MCP stdio handshake and actionable errors, without libvirt."""
import os
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOLS = {
    'qemu_list', 'qemu_attach', 'qemu_detach', 'qemu_status', 'qemu_start',
    'qemu_shutdown', 'qemu_reboot', 'qemu_force_stop', 'qemu_screenshot',
    'qemu_get_resolution', 'qemu_wait_screen', 'qemu_type', 'qemu_key',
    'qemu_release_all', 'qemu_mouse_move', 'qemu_mouse_click', 'qemu_mouse_scroll',
    'qemu_mouse_drag',
}


def parameters():
    return StdioServerParameters(command=sys.executable, args=['-m', 'qemu_mcp'], env={**os.environ})


@pytest.mark.anyio
async def test_stdio_handshake_lists_expected_tools():
    async with stdio_client(parameters()) as (read, write), ClientSession(read, write) as session:
        result = await session.initialize()
        assert result.server_info.name == 'qemu'
        assert {tool.name for tool in (await session.list_tools()).tools} == EXPECTED_TOOLS


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent():
    async with stdio_client(parameters()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool('qemu_status', {'name': 'unattached'})
        assert result.is_error
        assert 'qemu_attach' in result.content[0].text
        result = await session.call_tool('qemu_type', {'name': 'unattached', 'text': 'secret☃'})
        assert result.is_error
        assert 'ANSI US' in result.content[0].text
        assert 'secret' not in result.content[0].text
        assert '☃' not in result.content[0].text
