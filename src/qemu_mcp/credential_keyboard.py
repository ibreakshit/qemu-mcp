"""Match enrolled public key images locally; never return secret click positions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image

from .vm import Controller


class KeyboardError(Exception):
    """Only fixed codes may cross the credential CLI boundary."""


def _read(path: str) -> dict[str, Any]:
    try:
        with open(path, 'rb') as source:
            data = source.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError):
        raise KeyboardError('invalid_keyboard_profile') from None


def _rectangle(value: Any, width: int, height: int) -> list[int]:
    if (not isinstance(value, list) or len(value) != 4
            or any(type(n) is not int for n in value)):
        raise KeyboardError('invalid_keyboard_profile')
    x, y, w, h = value
    if x < 0 or y < 0 or w < 1 or h < 1 or x + w > width or y + h > height:
        raise KeyboardError('invalid_keyboard_profile')
    return value


def _crop(image: Image.Image, rectangle: list[int]) -> Image.Image:
    x, y, width, height = rectangle
    return image.crop((x, y, x + width, y + height))


def _digest(image: Image.Image) -> str:
    return hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()


def _key_digest(image: Image.Image, rectangle: list[int], inset: int) -> str:
    x, y, w, h = rectangle
    # Borders can repaint after focus or relocation without changing the key.
    # Match the enrolled face exactly, never a fuzzy/OCR approximation.
    return _digest(_crop(image, [x + inset, y + inset, w - 2 * inset, h - 2 * inset]))


def _state(value: Any, width: int, height: int, *, enrolled: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KeyboardError('invalid_keyboard_profile')
    bounds = _rectangle(value.get('bounds'), width, height)
    keys, transitions = value.get('keys'), value.get('transitions', {})
    inset = value.get('inset', 4)
    if type(inset) is not int or not 0 <= inset <= 16:
        raise KeyboardError('invalid_keyboard_profile')
    if not isinstance(keys, dict) or not 1 <= len(keys) <= 128 or not isinstance(transitions, dict):
        raise KeyboardError('invalid_keyboard_profile')
    if any(not isinstance(k, str) or k not in keys or len(k) < 2
           or not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', v)
           for k, v in transitions.items()):
        raise KeyboardError('invalid_keyboard_profile')
    rectangles: list[list[int]] = []
    digests = set()
    for label, key in keys.items():
        if (not isinstance(label, str) or not 1 <= len(label) <= 32
                or (label not in transitions and (len(label) != 1 or not 32 <= ord(label) <= 126))):
            raise KeyboardError('invalid_keyboard_profile')
        if enrolled:
            if not isinstance(key, dict):
                raise KeyboardError('invalid_keyboard_profile')
            digest = key.get('digest')
            if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest) or digest in digests:
                raise KeyboardError('invalid_keyboard_profile')
            digests.add(digest)
            key = key.get('rect')
        rectangle = _rectangle(key, width, height)
        x, y, w, h = rectangle
        if w <= 2 * inset or h <= 2 * inset:
            raise KeyboardError('invalid_keyboard_profile')
        bx, by, bw, bh = bounds
        if x < bx or y < by or x + w > bx + bw or y + h > by + bh:
            raise KeyboardError('invalid_keyboard_profile')
        for px, py, pw, ph in rectangles:
            if x < px + pw and px < x + w and y < py + ph and py < y + h:
                raise KeyboardError('invalid_keyboard_profile')
        rectangles.append(rectangle)
    return {'bounds': bounds, 'keys': keys, 'transitions': transitions, 'inset': inset}


def _load(path: str, uuid: str, width: int, height: int) -> dict[str, Any]:
    profile = _read(path)
    if (profile.get('version') != 1 or profile.get('vm_uuid') != uuid
            or profile.get('display') != [width, height]):
        raise KeyboardError('invalid_keyboard_profile')
    states = profile.get('states')
    if (not isinstance(states, dict) or not 1 <= len(states) <= 16
            or any(not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', name) for name in states)):
        raise KeyboardError('invalid_keyboard_profile')
    for state in states.values():
        _state(state, width, height, enrolled=True)
    return profile


def _parking_point(bounds: list[int], width: int, height: int) -> tuple[int, int]:
    x, y, w, h = bounds
    # Avoid display-edge zero coordinates and browser fullscreen reveal areas.
    for px, py in ((width - 2, height - 2), (1, height - 2), (width - 2, 1), (1, 1)):
        if not (0 <= px < width and 0 <= py < height):
            continue
        if not (x <= px < x + w and y <= py < y + h):
            return px, py
    raise KeyboardError('invalid_keyboard_profile')


def _settled(controller: Controller, uuid: str, bounds: list[int], display: list[int], settle_ms: int) -> Image.Image:
    # Move the pointer to a public, non-clicked point to remove hover styling.
    controller.mouse_move(uuid, *_parking_point(bounds, *display))
    time.sleep(settle_ms / 1000)
    previous = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        image = controller.screenshot(uuid).image()
        if list(image.size) != display:
            raise KeyboardError('keyboard_layout_changed')
        current = _digest(_crop(image, bounds))
        if current == previous:
            return image
        previous = current
        time.sleep(0.1)
    raise KeyboardError('keyboard_not_settled')


def enroll(controller: Controller, uuid: str, layout_path: str, profile_path: str,
           name: str, settle_ms: int) -> dict[str, Any]:
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', name):
        raise KeyboardError('invalid_keyboard_profile')
    frame = controller.screenshot(uuid)
    layout = _state(_read(layout_path), frame.width, frame.height, enrolled=False)
    display = [frame.width, frame.height]
    image = _settled(controller, uuid, layout['bounds'], display, settle_ms)
    keys = {label: {'rect': rectangle, 'digest': _key_digest(image, rectangle, layout['inset'])}
            for label, rectangle in layout['keys'].items()}
    state = {**layout, 'keys': keys}
    _state(state, *display, enrolled=True)  # Reject visually ambiguous keys.
    path = Path(profile_path).expanduser()
    if path.exists():
        profile = _load(str(path), uuid, *display)
    else:
        profile = {'version': 1, 'vm_uuid': uuid, 'display': display, 'states': {}}
    profile['states'][name] = state
    if len(profile['states']) > 16:
        raise KeyboardError('invalid_keyboard_profile')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Store only hashes of public key pixels, never frames or entered values.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.keyboard-')
    try:
        with os.fdopen(fd, 'w') as destination:
            json.dump(profile, destination)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'enrolled': True, 'states': sorted(profile['states']),
            'layout_id': hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()}


def _match(image: Image.Image, states: dict[str, Any]) -> tuple[str, dict[str, tuple[int, int]]]:
    matches = []
    for name, state in states.items():
        labels = {key['digest']: label for label, key in state['keys'].items()}
        positions = {}
        for key in state['keys'].values():
            rectangle = key['rect']
            label = labels.get(_key_digest(image, rectangle, state.get('inset', 4)))
            if label is None or label in positions:
                break
            x, y, w, h = rectangle
            positions[label] = (x + w // 2, y + h // 2)
        else:
            matches.append((name, positions))
    if len(matches) != 1:
        raise KeyboardError('keyboard_layout_changed')
    return matches[0]


def _route(states: dict[str, Any], start: str, character: str) -> list[str]:
    queue: deque[tuple[str, list[str]]] = deque([(start, [])])
    visited = {start}
    while queue:
        name, path = queue.popleft()
        if character in states[name]['keys']:
            return path
        for label, target in states[name]['transitions'].items():
            if target not in states:
                raise KeyboardError('invalid_keyboard_profile')
            if target not in visited:
                visited.add(target)
                queue.append((target, [*path, label]))
    raise KeyboardError('keyboard_character_unavailable')


def enter(controller: Controller, uuid: str, profile_path: str, value: str, settle_ms: int) -> dict[str, bool]:
    attempted = False
    try:
        frame = controller.screenshot(uuid)
        profile = _load(str(Path(profile_path).expanduser()), uuid, frame.width, frame.height)
        states = profile['states']
        bounds = [min(s['bounds'][0] for s in states.values()), min(s['bounds'][1] for s in states.values()),
                  0, 0]
        bounds[2] = max(s['bounds'][0] + s['bounds'][2] for s in states.values()) - bounds[0]
        bounds[3] = max(s['bounds'][1] + s['bounds'][3] for s in states.values()) - bounds[1]
        image = _settled(controller, uuid, bounds, profile['display'], settle_ms)
        current, positions = _match(image, states)
        # Validate every character before entering anything, including every
        # enrolled layer that may be observed after an auto-resetting Shift.
        if not 1 <= len(value) <= 256:
            raise KeyboardError('keyboard_character_unavailable')
        for state in states:
            for character in set(value):
                _route(states, state, character)
        deadline = time.monotonic() + 120
        for index, character in enumerate(value):
            for _ in range(len(states) + 1):
                if time.monotonic() >= deadline:
                    raise KeyboardError('keyboard_not_settled')
                route = _route(states, current, character)
                label = route[0] if route else character
                # Guard against a layout change between inspection and input.
                check = controller.screenshot(uuid).image()
                if check.size != image.size or _digest(_crop(check, bounds)) != _digest(_crop(image, bounds)):
                    raise KeyboardError('keyboard_layout_changed')
                attempted = True
                controller.mouse_click(uuid, x=positions[label][0], y=positions[label][1])
                if not route:
                    break
                expected = states[current]['transitions'][label]
                image = _settled(controller, uuid, bounds, profile['display'], settle_ms)
                current, positions = _match(image, states)
                if current != expected:
                    raise KeyboardError('keyboard_layout_changed')
            else:
                raise KeyboardError('keyboard_layout_changed')
            # Refresh even for repeated characters; position is never reused.
            # The last character may submit payment and close the keyboard.
            if index + 1 < len(value):
                image = _settled(controller, uuid, bounds, profile['display'], settle_ms)
                current, positions = _match(image, states)
        return {'entered': True, 'guest_text_verified': False}
    except (Exception, KeyboardInterrupt) as error:
        if attempted:
            raise KeyboardError('input_may_be_partial') from None
        if isinstance(error, KeyboardError):
            raise
        raise KeyboardError('backend_failure') from None
