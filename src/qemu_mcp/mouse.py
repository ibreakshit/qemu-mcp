"""Absolute QEMU pointer events with explicit coordinate conversion."""
from __future__ import annotations

from typing import Any

from .control import bounded_number

_BUTTONS = {'left', 'right', 'middle'}


def validate_button(button: str) -> None:
    if button not in _BUTTONS:
        raise ValueError('button must be left, right or middle')


def coordinates(x: float, y: float, width: int, height: int, mode: str) -> tuple[float, float]:
    if mode not in ('pixels', 'normalized'):
        raise ValueError('coordinate_mode must be pixels or normalized')
    if mode == 'normalized':
        bounded_number(x, 'x', 0, 1)
        bounded_number(y, 'y', 0, 1)
        return x, y
    bounded_number(x, 'x', 0, width - 1)
    bounded_number(y, 'y', 0, height - 1)
    return x / max(width - 1, 1), y / max(height - 1, 1)


def move_events(x: float, y: float) -> list[dict[str, Any]]:
    bounded_number(x, 'x', 0, 1)
    bounded_number(y, 'y', 0, 1)
    return [{'type': 'abs', 'data': {'axis': axis, 'value': round(value * 32767)}} for axis, value in [('x', x), ('y', y)]]
