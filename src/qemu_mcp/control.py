"""Explicit input transitions and held-input cleanup for one domain."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from .backend import LibvirtBackend

KEY_HOLD_S = 0.02
KEY_EVENT_INTERVAL_MS = 20
CLICK_GAP_S = 0.08


def bounded_number(value: float, label: str, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be finite and between {minimum} and {maximum}')


def integer(value: int, label: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be an integer between {minimum} and {maximum}')


def transition(kind: str, code: str, down: bool) -> dict[str, Any]:
    if kind == 'key':
        return {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': code}}}
    return {'type': 'btn', 'data': {'down': down, 'button': code}}


@dataclass
class InputState:
    backend: LibvirtBackend
    held: list[tuple[str, str]] = field(default_factory=list)
    next_key_event_at: float = 0.0

    def _send(self, domain: Any, kind: str, code: str, down: bool) -> None:
        if kind == 'key':
            # USB keyboards consume queued transitions on polling ticks. Pace
            # modifiers and releases too, across whole actions on this domain.
            while (remaining := self.next_key_event_at - time.monotonic()) > 0:
                time.sleep(remaining)
        try:
            self.backend.input(domain, [transition(kind, code, down)])
        finally:
            if kind == 'key':
                # An error can still mean delivery; pace cleanup without replay.
                self.next_key_event_at = time.monotonic() + KEY_EVENT_INTERVAL_MS / 1000

    def press(self, domain: Any, kind: str, code: str) -> bool:
        token = (kind, code)
        if token in self.held:
            return False
        # Track before sending: even a failed call may have delivered a down event.
        self.held.append(token)
        self._send(domain, kind, code, True)
        return True

    def release(self, domain: Any, kind: str, code: str) -> None:
        self._send(domain, kind, code, False)
        if (kind, code) in self.held:
            self.held.remove((kind, code))

    def release_all(self, domain: Any) -> None:
        failed = False
        for kind, code in reversed(self.held.copy()):
            try:
                self.release(domain, kind, code)
            except RuntimeError:
                failed = True
        if failed:
            raise RuntimeError('Some held inputs could not be released. Retry qemu_release_all after restoring the connection.')

    def chord(self, domain: Any, codes: list[str], state: str) -> None:
        if state == 'release':
            for code in reversed(codes):
                self.release(domain, 'key', code)
            return
        pressed = [code for code in codes if self.press(domain, 'key', code)]
        if state == 'click':
            for code in reversed(pressed):
                self.release(domain, 'key', code)
