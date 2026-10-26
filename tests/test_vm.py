"""Controller state and bounded screen observation."""
import io
import math
import threading

import pytest
from PIL import Image

from qemu_mcp.backend import Screenshot


def test_unattached_actions_do_not_connect(controller, host):
    for action in (controller.status, controller.screenshot, controller.release_all):
        with pytest.raises(ValueError, match='attach'):
            action('unknown')
    assert host.connections == []


def test_close_releases_inputs_without_power_change(controller, host):
    controller.attach('windows')
    controller.key('windows', 'ctrl', 'press')
    controller.close()
    assert host.domain.events[-1]['data']['down'] is False
    assert host.domain.actions == []
    assert host.connections[-1].closed


def test_black_stability_never_claims_readiness(controller, host, monkeypatch):
    controller.attach('windows')
    output = io.BytesIO()
    Image.new('RGB', (2, 2)).save(output, format='PNG')
    monkeypatch.setattr(controller.backend, 'capture', lambda domain: Screenshot(output.getvalue(), 2, 2))
    result = controller.wait_screen('windows', timeout_s=1, poll_interval_s=0.01, stable_polls=2)
    assert result['status'] == 'settled'
    assert result['black'] is True
    assert result['readiness'] == 'unknown'
    assert host.domain.events == []


def test_wait_screen_timeout_and_validation(controller):
    controller.attach('windows')
    result = controller.wait_screen('windows', timeout_s=0.01, poll_interval_s=0.01, stable_polls=100)
    assert result['status'] == 'timeout'
    assert result['settled'] is False
    for kwargs in ({'timeout_s': math.inf}, {'poll_interval_s': 0}, {'stable_polls': 0}, {'max_changed_pixels': -1}):
        with pytest.raises(ValueError):
            controller.wait_screen('windows', **kwargs)


def test_different_domains_have_independent_action_locks(controller, host):
    controller.attach('windows')
    first = host.domain
    second = type(first)()
    second.uuid = '87654321-1234-1234-1234-123456789abc'
    second.label = 'second'
    connection = host.connections[-1]
    connection.lookupByUUIDString = lambda uuid: first if uuid == first.uuid else second
    controller.attach(second.uuid)
    entered = threading.Event()
    complete = threading.Event()

    def hold_first():
        with controller._action('windows'):
            entered.set()
            assert complete.wait(1)

    thread = threading.Thread(target=hold_first)
    thread.start()
    assert entered.wait(1)
    try:
        assert controller.status(second.uuid)['uuid'] == second.uuid
    finally:
        complete.set()
        thread.join(1)
    assert not thread.is_alive()
