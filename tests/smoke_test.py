"""Opt-in MCP exercise of an existing guest; safe to collect with pytest.

Run: python tests/smoke_test.py --name EXISTING_DOMAIN --output screenshot.png
Add --exercise-input to send Shift and a pointer move. No power/snapshot changes.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ImageContent, TextContent


async def exercise(name: str, output: Path, exercise_input: bool) -> None:
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'qemu_mcp'], env={**os.environ})
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(tool: str, arguments: dict[str, Any]) -> Any:
            result = await session.call_tool(tool, arguments)
            if result.is_error:
                message = ' '.join(item.text for item in result.content if isinstance(item, TextContent))
                raise RuntimeError(f'{tool}: {message}')
            print(f'{tool}: OK')
            return result

        await call('qemu_list', {})
        await call('qemu_attach', {'name': name})
        try:
            await call('qemu_status', {'name': name})
            if exercise_input:
                await call('qemu_key', {'name': name, 'combo': 'shift'})
                await call('qemu_mouse_move', {'name': name, 'x': 0, 'y': 0})
            await call('qemu_get_resolution', {'name': name})
            settled = await call('qemu_wait_screen', {'name': name, 'timeout_s': 5, 'poll_interval_s': 0.5})
            for item in settled.content:
                if isinstance(item, TextContent):
                    print(item.text)
            screenshot = await call('qemu_screenshot', {'name': name})
            images = [item for item in screenshot.content if isinstance(item, ImageContent)]
            if len(images) != 1:
                raise RuntimeError('Expected one primary-display PNG')
            output.write_bytes(base64.b64decode(images[0].data))
            print(f'PNG saved to {output}')
        finally:
            await call('qemu_detach', {'name': name})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True, help='Existing domain name or UUID')
    parser.add_argument('--output', type=Path, default=Path('screenshot.png'))
    parser.add_argument('--exercise-input', action='store_true', help='Explicitly send Shift and move pointer to the top-left')
    arguments = parser.parse_args()
    anyio.run(exercise, arguments.name, arguments.output, arguments.exercise_input)


if __name__ == '__main__':
    main()
