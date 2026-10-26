import math
import threading
import time

import pytest


def key_events(host):
    return [(event['data']['key']['data'], event['data']['down']) for event in host.domain.events if event['type'] == 'key']


def test_whole_text_validation_and_no_secret_echo(controller, host):
    controller.attach('windows')
    with pytest.raises(ValueError) as error:
        controller.type_text('windows', 'secret☃')
    assert 'secret' not in str(error.value)
    assert '☃' not in str(error.value)
    assert host.domain.events == []
    assert controller.type_text('windows', 'A', 0) == {'characters_sent': 1, 'guest_text_verified': False, 'key_event_interval_ms': 20}
    assert key_events(host) == [('shift', True), ('a', True), ('a', False), ('shift', False)]


def test_held_order_and_cleanup(controller, host):
    controller.attach('windows')
    controller.key('windows', 'ctrl-alt', 'press')
    controller.mouse_click('windows', state='press')
    controller.release_all('windows')
    assert key_events(host) == [('ctrl', True), ('alt', True), ('alt', False), ('ctrl', False)]
    assert host.domain.events[-3]['data'] == {'down': False, 'button': 'left'}
    controller.key('windows', 'shift', 'press')
    controller.detach('windows')
    assert key_events(host)[-1] == ('shift', False)
    assert host.domain.actions == []


def test_uncertain_input_not_replayed_and_released(controller, host):
    controller.attach('windows')
    host.domain.fail_input = True
    with pytest.raises(RuntimeError) as error:
        controller.key('windows', 'a')
    assert 'secret' not in str(error.value)
    assert key_events(host) == [('a', True), ('a', False)]


@pytest.mark.parametrize('x,y,mode', [(100, 0, 'pixels'), (-1, 0, 'pixels'), (math.nan, 0, 'pixels'), (math.inf, 0, 'normalized'), (1.1, 0, 'normalized'), (0, 0, 'bad')])
def test_invalid_coordinates_send_nothing(controller, host, x, y, mode):
    controller.attach('windows')
    with pytest.raises(ValueError):
        controller.mouse_move('windows', x, y, mode)
    assert host.domain.events == []


def test_pixel_coordinates_and_scroll(controller, host):
    controller.attach('windows')
    controller.mouse_move('windows', 99, 49)
    assert [event['data']['value'] for event in host.domain.events] == [32767, 32767]
    host.domain.events.clear()
    controller.mouse_scroll('windows', 1, -1)
    assert [event['data']['button'] for event in host.domain.events] == ['wheel-down', 'wheel-down', 'wheel-left', 'wheel-left']
    host.domain.events.clear()
    with pytest.raises(ValueError):
        controller.mouse_scroll('windows', 1.5)
    with pytest.raises(ValueError):
        controller.mouse_click('windows', x=2)
    with pytest.raises(ValueError):
        controller.mouse_click('windows', count=3)
    assert host.domain.events == []


def test_drag_releases_on_failure(controller, host):
    controller.attach('windows')
    controller.mouse_drag('windows', 0, 0, 99, 49, duration_ms=0)
    assert host.domain.events[-1]['data'] == {'button': 'left', 'down': False}


def test_actions_serialize_for_same_domain(controller, host):
    controller.attach('windows')
    first_down = threading.Event()
    original = host.qemu.qemuMonitorCommand

    def monitor(dom, command, flags):
        result = original(dom, command, flags)
        if len(dom.events) == 1:
            first_down.set()
            time.sleep(0.04)
        return result

    host.qemu.qemuMonitorCommand = monitor
    first = threading.Thread(target=controller.key, args=('windows', 'a'))
    first.start()
    assert first_down.wait(1)
    controller.key('windows', 'b')
    first.join(1)
    assert not first.is_alive()
    assert key_events(host) == [('a', True), ('a', False), ('b', True), ('b', False)]


def test_validation_failure_does_not_release_held_inputs(controller, host):
    controller.attach('windows')
    controller.key('windows', 'ctrl', 'press')
    previous = list(host.domain.events)
    with pytest.raises(ValueError):
        controller.mouse_move('windows', -1, 0)
    assert host.domain.events == previous


def test_drag_finally_releases_after_movement_error(controller, host):
    controller.attach('windows')
    original = host.qemu.qemuMonitorCommand
    calls = 0

    def monitor(domain, command, flags):
        nonlocal calls
        calls += 1
        if calls == 3:
            domain.fail_input = True
        return original(domain, command, flags)

    host.qemu.qemuMonitorCommand = monitor
    with pytest.raises(RuntimeError, match='not replayed'):
        controller.mouse_drag('windows', 0, 0, 99, 49, duration_ms=0)
    assert host.domain.events[-1]['data'] == {'button': 'left', 'down': False}
    assert sum(event['type'] == 'btn' and event['data']['down'] for event in host.domain.events) == 1


def test_double_click_has_two_distinct_transitions(controller, host):
    controller.attach('windows')
    controller.mouse_click('windows', count=2)
    assert [event['data']['down'] for event in host.domain.events] == [True, False, True, False]


@pytest.mark.parametrize('delay_ms', [0, 5, 35])
def test_keyboard_transitions_are_paced_across_text_keys_and_cleanup(controller, host, keyboard_clock, delay_ms):
    controller.attach('windows')
    controller.type_text('windows', 'Aa!!', delay_ms)
    controller.key('windows', 'shift', 'press')
    controller.key(host.domain.uuid, 'a')
    controller.key('windows', 'shift', 'release')
    controller.key('windows', 'a')
    controller.key('windows', 'ctrl-alt', 'press')
    controller.release_all('windows')
    times = [timestamp for _, timestamp, _ in keyboard_clock.events]
    assert all(later - earlier >= 0.02 - 1e-9 for earlier, later in zip(times, times[1:]))
    assert key_events(host)[-10:] == [('shift', True), ('a', True), ('a', False), ('shift', False), ('a', True), ('a', False), ('ctrl', True), ('alt', True), ('alt', False), ('ctrl', False)]


def test_failed_key_transition_paces_cleanup_without_replay(controller, host, keyboard_clock):
    controller.attach('windows')
    host.domain.fail_input = True
    with pytest.raises(RuntimeError, match='not replayed'):
        controller.key('windows', 'a')
    assert key_events(host) == [('a', True), ('a', False)]
    assert keyboard_clock.events[1][1] - keyboard_clock.events[0][1] >= 0.02 - 1e-9


def test_keyboard_pacing_does_not_delay_independent_domains(controller, host, keyboard_clock):
    from conftest import FakeDomain

    second = FakeDomain()
    second.uuid = '87654321-1234-1234-1234-123456789abc'
    second.label = 'second'
    connection = controller.backend.connection()
    connection.lookupByName = lambda name: host.domain if name == 'windows' else second
    connection.lookupByUUIDString = lambda uuid: host.domain if uuid == host.domain.uuid else second
    controller.attach('windows')
    controller.attach('second')
    controller.key('windows', 'a')
    before = keyboard_clock.now
    controller.key('second', 'b', 'press')
    assert keyboard_clock.now == before


def test_type_result_reports_transport_count_and_unverified_guest(controller, keyboard_clock):
    controller.attach('windows')
    assert controller.type_text('windows', 'Aa', 0) == {
        'characters_sent': 2, 'guest_text_verified': False, 'key_event_interval_ms': 20,
    }


def test_requested_text_delay_adds_to_key_transition_floor(controller, keyboard_clock):
    controller.attach('windows')
    controller.type_text('windows', 'aa', 35)
    times = [timestamp for _, timestamp, _ in keyboard_clock.events]
    assert times == pytest.approx([0, 0.02, 0.055, 0.075])


def test_detach_paces_release_of_held_keys(controller, host, keyboard_clock):
    controller.attach('windows')
    controller.key('windows', 'ctrl-alt', 'press')
    controller.detach('windows')
    times = [timestamp for _, timestamp, _ in keyboard_clock.events]
    assert times == pytest.approx([0, 0.02, 0.04, 0.06])


def test_held_modifier_survives_typing_until_explicit_release(controller, host, keyboard_clock):
    controller.attach('windows')
    controller.key('windows', 'shift', 'press')
    controller.type_text('windows', 'A', 0)
    assert key_events(host) == [('shift', True), ('a', True), ('a', False)]
    controller.release_all('windows')
    assert key_events(host)[-1] == ('shift', False)


def test_idle_domain_does_not_accumulate_keyboard_delay(controller, keyboard_clock):
    controller.attach('windows')
    controller.key('windows', 'a')
    keyboard_clock.now += 10
    before = keyboard_clock.now
    controller.key('windows', 'a', 'press')
    assert keyboard_clock.now == before
