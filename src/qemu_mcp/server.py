"""Thin MCP handlers for the local libvirt controller."""
from __future__ import annotations

import functools
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .backend import LibvirtBackend
from .vm import Controller

# Importing/registering tools never opens a libvirt connection.
controller = Controller(LibvirtBackend(os.environ.get('QEMU_MCP_LIBVIRT_URI', 'qemu:///system')))


@asynccontextmanager
async def lifespan(server: MCPServer[Any]) -> AsyncIterator[None]:
    try:
        yield None
    finally:
        controller.close()


mcp = MCPServer('qemu', version='0.1.0', lifespan=lifespan, instructions=(
    'Control existing local libvirt domains. Use qemu_list, qemu_attach, '
    'qemu_screenshot, then input tools. Attach stopped guests for power management. '
    'Detaching or exiting never stops a guest. Coordinates default to native '
    'screenshot pixels; normalized coordinates must be explicit. Text uses '
    'physical ANSI US key positions; the guest owns layout and IME. Screenshots '
    'never wake displays. A stable or black image does not prove guest readiness. '
    'SPICE/virt-manager can stay open; simultaneous human input may interfere.'
))

F = TypeVar('F', bound=Callable[..., Any])


def tool(*, read_only: bool = False, destructive: bool = True, idempotent: bool = False) -> Callable[[F], F]:
    annotations = ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive, idempotent_hint=idempotent, open_world_hint=False)

    def register(function: F) -> F:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return function(*args, **kwargs)
            except (ValueError, RuntimeError, OSError) as error:
                raise ToolError(str(error)) from None

        mcp.tool(annotations=annotations)(wrapper)
        return function

    return register


@tool(read_only=True, destructive=False, idempotent=True)
def qemu_list() -> list[dict[str, Any]]:
    """List all existing domains, including stopped domains and attachment status."""
    return controller.list()


@tool(destructive=False, idempotent=True)
def qemu_attach(name: str) -> dict[str, Any]:
    """Attach an existing domain by name or UUID; retain UUID identity across reconnects."""
    return controller.attach(name)


@tool(idempotent=False)
def qemu_detach(name: str) -> dict[str, Any]:
    """Release held input best-effort and detach. Never shut down the guest."""
    return controller.detach(name)


@tool(read_only=True, destructive=False, idempotent=True)
def qemu_status(name: str) -> dict[str, Any]:
    """Read the current identity and state of an attached domain."""
    return controller.status(name)


@tool()
def qemu_start(name: str) -> dict[str, Any]:
    """Start an attached existing domain and report its actual current state."""
    return controller.power(name, 'start')


@tool()
def qemu_shutdown(name: str) -> dict[str, Any]:
    """Request graceful shutdown; report actual state without forced escalation."""
    return controller.power(name, 'shutdown')


@tool()
def qemu_reboot(name: str) -> dict[str, Any]:
    """Request graceful reboot; report actual state without forced escalation."""
    return controller.power(name, 'reboot')


@tool()
def qemu_force_stop(name: str) -> dict[str, Any]:
    """Immediately destroy the attached running domain. Unsaved guest work may be lost."""
    return controller.power(name, 'force_stop')


@tool(read_only=True, destructive=False, idempotent=True)
def qemu_screenshot(name: str) -> CallToolResult:
    """Capture the primary display as full-resolution PNG plus width/height. Never auto-wake."""
    frame = controller.screenshot(name)
    dimensions = {'width': frame.width, 'height': frame.height, 'format': 'png'}
    return CallToolResult(content=[TextContent(type='text', text=json.dumps(dimensions)), Image(data=frame.png, format='png').to_image_content()], structured_content=dimensions)


@tool(read_only=True, destructive=False, idempotent=True)
def qemu_get_resolution(name: str) -> dict[str, int]:
    """Read primary display dimensions through the same read-only screenshot path."""
    return controller.resolution(name)


@tool(read_only=True, destructive=False, idempotent=True)
def qemu_wait_screen(name: str, timeout_s: float = 30, poll_interval_s: float = 1, stable_polls: int = 3, max_changed_pixels: int = 64) -> dict[str, Any]:
    """Wait for bounded framebuffer stability, returning settled/timeout and black status.

    Stability is not readiness. A black display may be asleep; wake only with explicit input.
    """
    return controller.wait_screen(name, timeout_s, poll_interval_s, stable_polls, max_changed_pixels)


@tool()
def qemu_type(name: str, text: str, delay_ms: float = 35) -> dict[str, int | bool]:
    """Send physical ANSI US keys after whole-text validation; no text is echoed.

    delay_ms (0..1000, default 35) adds inter-character pauses to a 20 ms
    keyboard-transition floor. characters_sent counts transport-accepted input;
    guest_text_verified is false. Inspect screenshots for guest text and focus.
    """
    return controller.type_text(name, text, delay_ms)


@tool()
def qemu_key(name: str, combo: str, state: str = 'click') -> dict[str, str]:
    """Press/release/click paced chords such as ctrl-alt-f1 or rightalt (ralt/alt_r/altgr).

    Use rightalt for the right Alt key, not right-alt. Releases run in reverse order.
    The guest controls IME/layout; input does not verify the resulting guest text.
    """
    return controller.key(name, combo, state)


@tool(idempotent=True)
def qemu_release_all(name: str) -> dict[str, bool]:
    """Release all keys and buttons held by this MCP attachment."""
    return controller.release_all(name)


@tool()
def qemu_mouse_move(name: str, x: float, y: float, coordinate_mode: str = 'pixels') -> dict[str, bool]:
    """Move in native screenshot pixels, or explicit normalized 0..1 coordinates. No clamping."""
    return controller.mouse_move(name, x, y, coordinate_mode)


@tool()
def qemu_mouse_click(name: str, button: str = 'left', x: float | None = None, y: float | None = None, state: str = 'click', count: int = 1, coordinate_mode: str = 'pixels') -> dict[str, Any]:
    """Click/double-click or hold/release left/right/middle. Optional coordinates require x and y.

    count is 1 or 2 for clicks; press/release require count=1. Pixels are the default.
    """
    return controller.mouse_click(name, button, x, y, state, count, coordinate_mode)


@tool()
def qemu_mouse_scroll(name: str, delta_y: int, delta_x: int = 0) -> dict[str, int]:
    """Scroll signed integer wheel steps (-100..100 per axis). Positive means down/right."""
    return controller.mouse_scroll(name, delta_y, delta_x)


@tool()
def qemu_mouse_drag(name: str, start_x: float, start_y: float, end_x: float, end_y: float, button: str = 'left', duration_ms: float = 500, coordinate_mode: str = 'pixels') -> dict[str, bool]:
    """Move, press, interpolate and release as one action, with final release on failure.

    Duration is 0..10000 ms; coordinates default to native screenshot pixels.
    """
    return controller.mouse_drag(name, start_x, start_y, end_x, end_y, button, duration_ms, coordinate_mode)


def main() -> None:
    mcp.run()
