"""Handlers stay thin and return dimensions together with PNG content."""
import pytest
from mcp.types import ImageContent, TextContent

from qemu_mcp import server


def test_screenshot_returns_metadata_and_image(controller, monkeypatch):
    controller.attach('windows')
    monkeypatch.setattr(server, 'controller', controller)
    result = server.qemu_screenshot('windows')
    assert result.structured_content == {'width': 100, 'height': 50, 'format': 'png'}
    assert isinstance(result.content[1], ImageContent)



@pytest.mark.anyio
async def test_wire_screenshot_contains_dimensions_and_png(controller, monkeypatch):
    controller.attach('windows')
    monkeypatch.setattr(server, 'controller', controller)
    result = await server.mcp.call_tool('qemu_screenshot', {'name': 'windows'})
    assert not result.is_error
    assert any(isinstance(item, ImageContent) and item.mime_type == 'image/png' for item in result.content)
    text = ' '.join(item.text for item in result.content if isinstance(item, TextContent))
    assert '100' in text and '50' in text


@pytest.mark.anyio
async def test_lifespan_releases_without_stopping(controller, host, monkeypatch):
    monkeypatch.setattr(server, 'controller', controller)
    controller.attach('windows')
    controller.key('windows', 'ctrl', 'press')
    async with server.lifespan(server.mcp):
        assert host.domain.events[-1]['data']['down'] is True
    assert host.domain.events[-1]['data']['down'] is False
    assert host.domain.actions == []
    assert host.connections[-1].closed


@pytest.mark.anyio
async def test_wire_type_result_marks_guest_text_unverified(controller, monkeypatch, keyboard_clock):
    controller.attach('windows')
    monkeypatch.setattr(server, 'controller', controller)
    result = await server.mcp.call_tool('qemu_type', {'name': 'windows', 'text': 'Aa', 'delay_ms': 0})
    assert not result.is_error
    assert result.structured_content == {'characters_sent': 2, 'guest_text_verified': False, 'key_event_interval_ms': 20}
    assert 'characters_typed' not in str(result.content)
