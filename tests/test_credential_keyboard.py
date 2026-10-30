"""Secret entry through real framebuffer matching and native input events."""
import io
import json

import pytest
from PIL import Image, ImageDraw

from test_credentials import ITEM, configuration as configuration, guest as guest, invoke, op as op


@pytest.fixture
def board(guest, tmp_path, monkeypatch):
    class Keyboard:
        state = 'digits'
        layouts = {'digits': list('012345'), 'lower': list('ab0! ') + ['Shift'],
                   'upper': list('AB0! ') + ['Shift']}
        transitions = {'digits': {}, 'lower': {'Shift': 'upper'}, 'upper': {'Shift': 'lower'}}
        reset_shift = False
        entered = ''
        clicks = 0
        fail_after = None
        obscure_after = None
        edge_noise = False
        point = [0, 0]
        pressed = False
        profile = tmp_path / 'keyboard.json'

        def rectangles(self):
            return {label: [20 + i % 3 * 55, 20 + i // 3 * 35, 45, 25]
                    for i, label in enumerate(self.layouts[self.state])}

        def image(self):
            image = Image.new('RGB', (200, 110), 'white')
            draw = ImageDraw.Draw(image)
            for label, (x, y, w, h) in self.rectangles().items():
                draw.rectangle((x, y, x + w - 1, y + h - 1), fill='#dddddd')
                draw.text((x + 5, y + 5), {' ': 'Space'}.get(label, label), fill='black')
                if self.edge_noise:
                    draw.rectangle((x, y, x + w - 1, y + h - 1), outline='red')
            if self.obscure_after is not None and self.clicks >= self.obscure_after:
                draw.rectangle((10, 10, 190, 100), fill='black')
            return image

        def enroll(self, capsys, config):
            layout = tmp_path / 'layout.json'
            layout.write_text(json.dumps({'bounds': [10, 10, 180, 90], 'keys': self.rectangles(),
                                          'transitions': self.transitions[self.state]}))
            return invoke(capsys, config, 'inspect-keyboard', '--vm', 'desktop', '--layout', str(layout),
                          '--profile', str(self.profile), '--state', self.state)

    keyboard = Keyboard()

    def screenshot(stream, screen, flags):
        out = io.BytesIO()
        keyboard.image().save(out, format='PPM')
        stream.data = out.getvalue()
        return 'image/x-portable-pixmap'

    monitor = guest.qemu.qemuMonitorCommand

    def input_event(domain, command, flags):
        result = monitor(domain, command, flags)
        for event in json.loads(command)['arguments']['events']:
            if event['type'] == 'abs':
                axis = 0 if event['data']['axis'] == 'x' else 1
                keyboard.point[axis] = round(event['data']['value'] * (199 if axis == 0 else 109) / 32767)
            if event['type'] == 'btn' and event['data']['down']:
                keyboard.pressed = True
            if event['type'] == 'btn' and not event['data']['down'] and keyboard.pressed:
                keyboard.pressed = False
                for label, (x, y, w, h) in keyboard.rectangles().items():
                    if x <= keyboard.point[0] < x + w and y <= keyboard.point[1] < y + h:
                        keyboard.clicks += 1
                        if label in keyboard.transitions[keyboard.state]:
                            keyboard.state = keyboard.transitions[keyboard.state][label]
                        else:
                            keyboard.entered += label
                            if keyboard.reset_shift and keyboard.state == 'upper':
                                keyboard.state = 'lower'
                        current = keyboard.layouts[keyboard.state]
                        keyboard.layouts[keyboard.state] = current[1:] + current[:1]
                        if keyboard.fail_after == keyboard.clicks:
                            raise RuntimeError('synthetic-sensitive-native-error')
                        break
        return result

    monkeypatch.setattr(guest.domain, 'screenshot', screenshot)
    monkeypatch.setattr(guest.qemu, 'qemuMonitorCommand', input_event)
    return keyboard


def enter(capsys, configuration, board):
    return invoke(capsys, configuration, 'enter-keyboard', '--vm', 'desktop', '--item', ITEM,
                  '--profile', str(board.profile))


def test_pin_rematches_after_every_shuffle_without_exposing_secret(capsys, configuration, op, board):
    op.details['fields'][1]['value'] = '0503'
    assert board.enroll(capsys, configuration)[0] == 0
    assert op.calls == []  # Public layout enrollment never queries 1Password.
    code, result = enter(capsys, configuration, board)
    assert code == 0
    assert result == {'entered': True, 'guest_text_verified': False}
    assert board.entered == '0503'
    assert board.clicks == 4
    assert '0503' not in json.dumps(result)
    assert board.profile.stat().st_mode & 0o777 == 0o600


def test_qwerty_switches_layers_and_preserves_space(capsys, configuration, op, board):
    board.state = 'lower'
    assert board.enroll(capsys, configuration)[0] == 0
    board.state = 'upper'
    assert board.enroll(capsys, configuration)[0] == 0
    board.state = 'lower'
    op.details['fields'][1]['value'] = 'aB! 0b'
    assert enter(capsys, configuration, board)[0] == 0
    assert board.entered == 'aB! 0b'
    assert board.clicks == 8  # Six characters plus two software Shift clicks.


def test_key_border_repainting_does_not_change_glyph_identity(capsys, configuration, op, board):
    assert board.enroll(capsys, configuration)[0] == 0
    board.edge_noise = True
    op.details['fields'][1]['value'] = '010'
    assert enter(capsys, configuration, board)[0] == 0
    assert board.entered == '010'


def test_auto_resetting_shift_is_detected_from_the_next_frame(capsys, configuration, op, board):
    for state in ('lower', 'upper'):
        board.state = state
        assert board.enroll(capsys, configuration)[0] == 0
    board.state = 'lower'
    board.reset_shift = True
    op.details['fields'][1]['value'] = 'ABba'
    assert enter(capsys, configuration, board)[0] == 0
    assert board.entered == 'ABba'
    assert board.clicks == 6


def test_symbol_layer_routes_through_the_letter_layer(capsys, configuration, op, board):
    board.layouts['lower'] = ['a', '0', '!', ' ', 'Shift', 'Symbols']
    board.layouts['symbols'] = ['#', '@', '0', '!', ' ', 'ABC']
    board.transitions['lower']['Symbols'] = 'symbols'
    board.transitions['symbols'] = {'ABC': 'lower'}
    for state in ('lower', 'upper', 'symbols'):
        board.state = state
        assert board.enroll(capsys, configuration)[0] == 0
    board.state = 'lower'
    op.details['fields'][1]['value'] = '#A!@a'
    assert enter(capsys, configuration, board)[0] == 0
    assert board.entered == '#A!@a'


@pytest.mark.parametrize('problem', ['unmapped_character', 'stale_pixels', 'wrong_vm', 'invalid_profile'])
def test_bad_layout_never_enters_any_secret(capsys, configuration, op, board, problem):
    assert board.enroll(capsys, configuration)[0] == 0
    op.details['fields'][1]['value'] = '01'
    if problem == 'unmapped_character':
        op.details['fields'][1]['value'] = '019'
    elif problem == 'stale_pixels':
        board.obscure_after = 0
    elif problem == 'wrong_vm':
        saved = json.loads(board.profile.read_text())
        saved['vm_uuid'] = '87654321-1234-1234-1234-123456789abc'
        board.profile.write_text(json.dumps(saved))
    else:
        board.profile.write_text('not-json-sensitive-fixture')
    code, result = enter(capsys, configuration, board)
    assert code == 1
    assert 'error' in result
    assert board.entered == ''
    assert board.clicks == 0


@pytest.mark.parametrize('failure', ['native', 'screen'])
def test_partial_entry_stops_without_replay_or_secret_errors(capsys, configuration, op, board, failure):
    assert board.enroll(capsys, configuration)[0] == 0
    op.details['fields'][1]['value'] = '012'
    if failure == 'native':
        board.fail_after = 1
    else:
        board.obscure_after = 1
    code, result = enter(capsys, configuration, board)
    assert code == 1
    assert result['error']['code'] == 'input_may_be_partial'
    assert board.entered == '0'
    assert board.clicks == 1
    assert 'synthetic-sensitive-native-error' not in json.dumps(result)


def test_duplicate_visible_key_is_rejected_before_input(capsys, configuration, op, board):
    assert board.enroll(capsys, configuration)[0] == 0
    board.layouts['digits'] = list('001234')
    op.details['fields'][1]['value'] = '01'
    code, result = enter(capsys, configuration, board)
    assert code == 1
    assert result['error']['code'] == 'keyboard_layout_changed'
    assert board.entered == ''


@pytest.mark.parametrize('rectangle', [[-1, 20, 45, 25], [20, 20, 500, 25], [20, 20, 1, 1],
                                       [True, 20, 45, 25], [20, 20, 45]])
def test_invalid_geometry_cannot_be_enrolled(capsys, configuration, op, board, rectangle, tmp_path):
    path = tmp_path / 'bad-layout.json'
    path.write_text(json.dumps({'bounds': [10, 10, 180, 90], 'keys': {'0': rectangle}}))
    code, result = invoke(capsys, configuration, 'inspect-keyboard', '--vm', 'desktop',
                          '--layout', str(path), '--profile', str(board.profile))
    assert code == 1
    assert result['error']['code'] == 'invalid_keyboard_profile'
    assert not board.profile.exists()
    assert op.calls == []
    assert board.entered == ''


def test_unenrolled_layer_is_rejected_before_any_secret_click(capsys, configuration, op, board):
    board.state = 'lower'
    assert board.enroll(capsys, configuration)[0] == 0
    op.details['fields'][1]['value'] = 'aB'
    code, result = enter(capsys, configuration, board)
    assert code == 1
    assert result['error']['code'] == 'invalid_keyboard_profile'
    assert board.entered == ''
