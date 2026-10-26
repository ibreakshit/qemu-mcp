"""UUID attachments and serialized whole actions on existing libvirt domains."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import keys, mouse
from .backend import LibvirtBackend, Screenshot
from .control import CLICK_GAP_S, KEY_EVENT_INTERVAL_MS, KEY_HOLD_S, InputState, bounded_number, integer
from .screen import StabilityTracker


@dataclass
class Attachment:
    uuid: str
    inputs: InputState
    lock: Any = field(default_factory=threading.RLock)
    active: bool = True


class Controller:
    def __init__(self, backend: LibvirtBackend):
        self.backend = backend
        self._attachments: dict[str, Attachment] = {}
        self._aliases: dict[str, str] = {}
        self._lock = threading.RLock()

    def list(self) -> list[dict[str, Any]]:
        result = []
        for domain in self.backend.domains():
            info = self.backend.identity(domain)
            with self._lock:
                info['attached'] = info['uuid'] in self._attachments
            result.append(info)
        return result

    def attach(self, name: str) -> dict[str, Any]:
        domain = self.backend.resolve(name)
        info = self.backend.identity(domain)
        uuid = str(info['uuid'])
        with self._lock:
            if uuid not in self._attachments:
                self._attachments[uuid] = Attachment(uuid, InputState(self.backend))
            self._aliases[name] = uuid
            self._aliases[str(info['name'])] = uuid
            self._aliases[uuid] = uuid
        return {**info, 'attached': True}

    @contextmanager
    def _action(self, name: str, *, cleanup: bool = False) -> Iterator[tuple[Attachment, Any]]:
        with self._lock:
            attached = self._attachments.get(self._aliases.get(name, name))
        if attached is None:
            raise ValueError('Domain is not attached. Call qemu_attach with its name or UUID first.')
        with attached.lock:
            if not attached.active:
                raise ValueError('Domain is detached. Call qemu_attach first.')
            domain = self.backend.resolve(attached.uuid, uuid_only=True)
            try:
                yield attached, domain
            except RuntimeError:
                if cleanup:
                    try:
                        attached.inputs.release_all(domain)
                    except RuntimeError:
                        pass
                raise

    def status(self, name: str) -> dict[str, Any]:
        with self._action(name) as (_, domain):
            return {**self.backend.identity(domain), 'attached': True}

    def detach(self, name: str) -> dict[str, Any]:
        with self._lock:
            attached = self._attachments.get(self._aliases.get(name, name))
        if attached is None:
            raise ValueError('Domain is not attached. Call qemu_attach first.')
        with attached.lock:
            released = True
            try:
                domain = self.backend.resolve(attached.uuid, uuid_only=True)
                attached.inputs.release_all(domain)
            except RuntimeError:
                released = False
            with self._lock:
                attached.active = False
                self._attachments.pop(attached.uuid, None)
                self._aliases = {alias: uuid for alias, uuid in self._aliases.items() if uuid != attached.uuid}
            return {'uuid': attached.uuid, 'attached': False, 'inputs_released': released}

    def close(self) -> None:
        with self._lock:
            uuids = list(self._attachments)
        for uuid in uuids:
            try:
                self.detach(uuid)
            except (RuntimeError, ValueError):
                pass
        self.backend.close()

    def power(self, name: str, action: str) -> dict[str, Any]:
        with self._action(name) as (_, domain):
            self.backend.power(domain, action)
            return {**self.backend.identity(domain), 'requested': action, 'attached': True}

    def screenshot(self, name: str) -> Screenshot:
        with self._action(name) as (_, domain):
            return self.backend.capture(domain)

    def resolution(self, name: str) -> dict[str, int]:
        frame = self.screenshot(name)
        return {'width': frame.width, 'height': frame.height}

    def wait_screen(self, name: str, timeout_s: float = 30, poll_interval_s: float = 1, stable_polls: int = 3, max_changed_pixels: int = 64) -> dict[str, Any]:
        bounded_number(timeout_s, 'timeout_s', 0.01, 300)
        bounded_number(poll_interval_s, 'poll_interval_s', 0.01, 300)
        integer(stable_polls, 'stable_polls', 1, 1000)
        integer(max_changed_pixels, 'max_changed_pixels', 0, 100000000)
        tracker = StabilityTracker(stable_polls, max_changed_pixels)
        deadline = time.monotonic() + timeout_s
        polls = 0
        while True:
            frame = self.screenshot(name)
            image = frame.image()
            polls += 1
            settled = tracker.update(image)
            black = image.getbbox() is None
            result = {'status': 'settled' if settled else 'timeout', 'settled': settled, 'black': black, 'readiness': 'unknown', 'width': frame.width, 'height': frame.height, 'polls': polls, 'changed_pixels': tracker.last_changed}
            if settled:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            time.sleep(min(poll_interval_s, remaining))

    def type_text(self, name: str, text: str, delay_ms: float = 35) -> dict[str, int | bool]:
        bounded_number(delay_ms, 'delay_ms', 0, 1000)
        try:
            chords = [keys.char_to_keys(character) for character in text]
        except ValueError:
            raise ValueError('Text contains unsupported characters. Only physical ANSI US key positions are supported; the guest controls layout and IME.') from None
        with self._action(name, cleanup=True) as (attached, domain):
            for index, chord in enumerate(chords):
                attached.inputs.chord(domain, chord, 'click')
                if index + 1 < len(chords):
                    time.sleep(delay_ms / 1000)
        return {'characters_sent': len(text), 'guest_text_verified': False, 'key_event_interval_ms': KEY_EVENT_INTERVAL_MS}

    def key(self, name: str, combo: str, state: str = 'click') -> dict[str, str]:
        self._validate_state(state)
        try:
            codes = list(dict.fromkeys(keys.parse_combo(combo)))
        except ValueError:
            raise ValueError('Invalid key combo. Use ctrl-alt-f1, rightalt, enter, arrows or ANSI US keys.') from None
        with self._action(name, cleanup=True) as (attached, domain):
            attached.inputs.chord(domain, codes, state)
        return {'state': state}

    @staticmethod
    def _validate_state(state: str) -> None:
        if state not in ('click', 'press', 'release'):
            raise ValueError('state must be click, press or release')

    def release_all(self, name: str) -> dict[str, bool]:
        with self._action(name) as (attached, domain):
            attached.inputs.release_all(domain)
        return {'inputs_released': True}

    def _position(self, domain: Any, x: float, y: float, mode: str) -> tuple[float, float]:
        frame = self.backend.capture(domain)
        return mouse.coordinates(x, y, frame.width, frame.height, mode)

    def mouse_move(self, name: str, x: float, y: float, coordinate_mode: str = 'pixels') -> dict[str, bool]:
        with self._action(name, cleanup=True) as (_, domain):
            position = self._position(domain, x, y, coordinate_mode)
            self.backend.input(domain, mouse.move_events(*position))
        return {'moved': True}

    def mouse_click(self, name: str, button: str = 'left', x: float | None = None, y: float | None = None, state: str = 'click', count: int = 1, coordinate_mode: str = 'pixels') -> dict[str, Any]:
        mouse.validate_button(button)
        self._validate_state(state)
        integer(count, 'count', 1, 2)
        if state != 'click' and count != 1:
            raise ValueError('Held button states require count=1')
        if (x is None) != (y is None):
            raise ValueError('Provide both x and y, or neither')
        if coordinate_mode not in ('pixels', 'normalized'):
            raise ValueError('coordinate_mode must be pixels or normalized')
        with self._action(name, cleanup=True) as (attached, domain):
            if x is not None and y is not None:
                position = self._position(domain, x, y, coordinate_mode)
                self.backend.input(domain, mouse.move_events(*position))
            if state == 'release':
                attached.inputs.release(domain, 'btn', button)
            else:
                if state == 'click' and ('btn', button) in attached.inputs.held:
                    raise ValueError('Button already held. Release it before clicking.')
                for index in range(count):
                    attached.inputs.press(domain, 'btn', button)
                    if state == 'click':
                        time.sleep(KEY_HOLD_S)
                        attached.inputs.release(domain, 'btn', button)
                    if index + 1 < count:
                        time.sleep(CLICK_GAP_S)
        return {'state': state, 'count': count}

    def mouse_scroll(self, name: str, delta_y: int, delta_x: int = 0) -> dict[str, int]:
        integer(delta_y, 'delta_y', -100, 100)
        integer(delta_x, 'delta_x', -100, 100)
        with self._action(name, cleanup=True) as (attached, domain):
            for delta, positive, negative in [(delta_y, 'wheel-down', 'wheel-up'), (delta_x, 'wheel-right', 'wheel-left')]:
                button = positive if delta > 0 else negative
                for _ in range(abs(delta)):
                    attached.inputs.press(domain, 'btn', button)
                    attached.inputs.release(domain, 'btn', button)
        return {'delta_y': delta_y, 'delta_x': delta_x}

    def mouse_drag(self, name: str, start_x: float, start_y: float, end_x: float, end_y: float, button: str = 'left', duration_ms: float = 500, coordinate_mode: str = 'pixels') -> dict[str, bool]:
        mouse.validate_button(button)
        bounded_number(duration_ms, 'duration_ms', 0, 10000)
        with self._action(name, cleanup=True) as (attached, domain):
            frame = self.backend.capture(domain)
            start = mouse.coordinates(start_x, start_y, frame.width, frame.height, coordinate_mode)
            end = mouse.coordinates(end_x, end_y, frame.width, frame.height, coordinate_mode)
            if ('btn', button) in attached.inputs.held:
                raise ValueError('Button already held. Release it before dragging.')
            self.backend.input(domain, mouse.move_events(*start))
            try:
                attached.inputs.press(domain, 'btn', button)
                steps = max(1, round(duration_ms / 20))
                for step in range(1, steps + 1):
                    time.sleep(duration_ms / 1000 / steps)
                    position = tuple(a + (b - a) * step / steps for a, b in zip(start, end))
                    self.backend.input(domain, mouse.move_events(*position))
            finally:
                attached.inputs.release(domain, 'btn', button)
        return {'dragged': True}
