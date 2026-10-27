"""Credential CLI boundaries using synthetic 1Password data and fake libvirt."""
import json
import subprocess

import pytest

ACCOUNT = 'a' * 26
AGENTS = 'b' * 26
SHARED = 'c' * 26
ITEM = 'd' * 26
OTHER = 'e' * 26
VM = '12345678-1234-1234-1234-123456789abc'
SECRET = 'synthetic-secret-do-not-output'


@pytest.fixture
def configuration(tmp_path):
    data = {
        'account': ACCOUNT,
        'vaults': [{'id': AGENTS, 'name': 'Agents'}, {'id': SHARED, 'name': 'Shared Vault'}],
        'vms': {'desktop': VM},
    }
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(data))
    return path, data


@pytest.fixture
def op(monkeypatch):
    class SyntheticOp:
        def __init__(self):
            self.calls = []
            self.account = ACCOUNT
            self.fail_vault = None
            self.failure = None
            self.listings = {
                AGENTS: [{'id': ITEM, 'title': 'Example', 'vault': {'id': AGENTS},
                          'category': 'LOGIN', 'additional_information': SECRET}],
                SHARED: [{'id': OTHER, 'title': 'Shared login', 'vault': {'id': SHARED},
                          'category': 'LOGIN'}],
            }
            self.details = {'id': ITEM, 'vault': {'id': AGENTS}, 'title': 'Example',
                            'fields': [{'id': 'username', 'label': 'username', 'type': 'STRING',
                                        'value': 'synthetic-user'},
                                       {'id': 'password', 'label': 'password', 'type': 'CONCEALED',
                                        'value': SECRET}]}
            self.otp = '123456\n'

        def run(self, arguments, **kwargs):
            self.calls.append((arguments, kwargs))
            assert arguments[0] == 'op'
            assert kwargs['stdin'] == subprocess.DEVNULL
            assert kwargs['timeout'] == 30
            assert kwargs['capture_output'] is True
            assert kwargs.get('shell', False) is False
            assert arguments[arguments.index('--account') + 1] == self.account
            if self.failure:
                raise self.failure
            vault = arguments[arguments.index('--vault') + 1]
            if vault == self.fail_vault:
                return subprocess.CompletedProcess(arguments, 1, SECRET, SECRET)
            if arguments[1:3] == ['item', 'list']:
                value = json.dumps(self.listings[vault])
            else:
                assert arguments[1:4] == ['item', 'get', ITEM]
                value = self.otp if '--otp' in arguments else json.dumps(self.details)
            return subprocess.CompletedProcess(arguments, 0, value, SECRET)

    fake = SyntheticOp()
    monkeypatch.setattr(subprocess, 'run', fake.run)
    return fake


def invoke(capsys, configuration, *arguments):
    from qemu_mcp import credentials
    code = credentials.main([*arguments, '--config', str(configuration[0])])
    captured = capsys.readouterr()
    assert captured.err == ''
    assert SECRET not in captured.out
    assert 'synthetic-user' not in captured.out
    assert 'Traceback' not in captured.out
    return code, json.loads(captured.out)


def test_list_projects_only_metadata_from_both_vaults(capsys, configuration, op):
    code, result = invoke(capsys, configuration, 'list')
    assert code == 0
    assert result == {'items': [
        {'id': ITEM, 'title': 'Example', 'vault': {'id': AGENTS, 'name': 'Agents'}, 'category': 'LOGIN'},
        {'id': OTHER, 'title': 'Shared login', 'vault': {'id': SHARED, 'name': 'Shared Vault'}, 'category': 'LOGIN'},
    ]}
    assert len(op.calls) == 2


def test_list_suppresses_archived_and_outside_items(capsys, configuration, op, monkeypatch):
    monkeypatch.setenv('OP_INCLUDE_ARCHIVE', 'true')
    op.listings[AGENTS].extend([
        {'id': 'f' * 26, 'vault': {'id': AGENTS}, 'state': 'ARCHIVED', 'title': SECRET},
        {'id': 'g' * 26, 'vault': {'id': 'h' * 26}, 'title': SECRET},
        {'id': 'i' * 26, 'vault': {'id': AGENTS}, 'archived': True, 'title': SECRET},
    ])
    code, result = invoke(capsys, configuration, 'list')
    assert code == 0
    assert [item['id'] for item in result['items']] == [ITEM, OTHER]


def test_empty_vaults_succeed_but_failed_vault_is_not_partial(capsys, configuration, op):
    op.listings = {AGENTS: [], SHARED: []}
    assert invoke(capsys, configuration, 'list') == (0, {'items': []})
    op.fail_vault = SHARED
    code, result = invoke(capsys, configuration, 'list')
    assert code != 0
    assert result['error']['code'] == 'authentication_failed'
    assert 'items' not in result


@pytest.mark.parametrize('change', [
    {'account': 'op://bad'}, {'vaults': []},
    {'vaults': [{'id': AGENTS, 'name': 'Agents'}, {'id': AGENTS, 'name': 'Shared'}]},
    {'vaults': [{'id': AGENTS, 'name': 'Agents'}, {'id': SHARED, 'name': 'Elsewhere'}]},
    {'vms': {'desktop': 'a-name'}}, {'libvirt_uri': 'qemu+ssh://remote/system'},
    {'service_account_keyring': 'true'},
])
def test_invalid_configuration_has_sanitized_error(capsys, configuration, op, change):
    path, data = configuration
    data.update(change)
    path.write_text(json.dumps(data))
    code, result = invoke(capsys, configuration, 'list')
    assert code != 0
    assert result['error']['code'] == 'invalid_config'
    assert op.calls == []


def test_shared_vault_name_must_be_exactly_shared_vault(capsys, configuration, op):
    path, data = configuration
    code, result = invoke(capsys, configuration, 'list')
    assert code == 0
    assert result['items'][1]['vault'] == {'id': SHARED, 'name': 'Shared Vault'}

    data['vaults'][1]['name'] = 'Shared'
    path.write_text(json.dumps(data))
    code, result = invoke(capsys, configuration, 'list')
    assert code != 0
    assert result['error']['code'] == 'invalid_config'
    assert 'Agents, Shared Vault and VM UUIDs' in result['error']['message']
    assert len(op.calls) == 2


@pytest.mark.parametrize('content', [None, SECRET, '[]'])
def test_missing_or_malformed_configuration(capsys, configuration, op, content):
    if content is None:
        configuration[0].unlink()
    else:
        configuration[0].write_text(content)
    code, result = invoke(capsys, configuration, 'list')
    assert code != 0
    assert result['error']['code'] == 'invalid_config'


def test_denied_vm_and_item_selectors_never_query_op(capsys, configuration, op):
    for arguments in [('list', '--vm', 'other'), ('list', '--item', 'op://outside/password'),
                      ('list', '--item', 'Example'), ('type', '--item', ITEM)]:
        code, result = invoke(capsys, configuration, *arguments)
        assert code != 0
        assert result['error']['code'] in ('invalid_arguments', 'denied_vm', 'inaccessible_item')
    assert op.calls == []


@pytest.mark.parametrize('failure,expected', [
    (FileNotFoundError(SECRET), 'op_unavailable'),
    (PermissionError(SECRET), 'op_unavailable'),
    (subprocess.TimeoutExpired(['op', SECRET], 30, output=SECRET, stderr=SECRET), 'op_timeout'),
])
def test_op_errors_are_sanitized(capsys, configuration, op, failure, expected):
    op.failure = failure
    code, result = invoke(capsys, configuration, 'list')
    assert code != 0
    assert result['error']['code'] == expected


def test_policy_environment_is_removed_but_supported_auth_is_kept(capsys, configuration, op, monkeypatch):
    for key in ('OP_CONNECT_HOST', 'OP_CONNECT_TOKEN', 'OP_VAULT', 'OP_INCLUDE_ARCHIVE', 'OP_ACCOUNT'):
        monkeypatch.setenv(key, 'unexpected')
    monkeypatch.setenv('OP_SERVICE_ACCOUNT_TOKEN', 'synthetic-service-token')
    monkeypatch.setenv('OP_SESSION_test', 'synthetic-desktop-session')
    assert invoke(capsys, configuration, 'list', '--vm', 'desktop')[0] == 0
    environment = op.calls[0][1]['env']
    for key in ('OP_CONNECT_HOST', 'OP_CONNECT_TOKEN', 'OP_VAULT', 'OP_INCLUDE_ARCHIVE', 'OP_ACCOUNT'):
        assert key not in environment
    assert environment['OP_SERVICE_ACCOUNT_TOKEN'] == 'synthetic-service-token'
    assert environment['OP_SESSION_test'] == 'synthetic-desktop-session'



def _enable_keyring(configuration):
    path, data = configuration
    data['service_account_keyring'] = True
    path.write_text(json.dumps(data))


def _install_synthetic_keyring(monkeypatch, token='ops_synthetic-keyring-token', failure=None, import_failure=False):
    from qemu_mcp import credentials

    string_attribute_type = object()

    class Schema:
        @staticmethod
        def new(name, flags, attributes):
            assert name == 'local.qemu-credential'
            assert attributes == {'application': string_attribute_type, 'credential': string_attribute_type}
            return ('schema', name, flags, attributes)

    class Secret:
        class SchemaFlags:
            NONE = 0

        class SchemaAttributeType:
            STRING = string_attribute_type

        @staticmethod
        def password_lookup_sync(schema, attributes, cancellable):
            assert schema[0] == 'schema'
            assert attributes == {'application': 'qemu-credential', 'credential': 'service-account'}
            assert cancellable is None
            if failure:
                raise RuntimeError('synthetic-keyring-error-do-not-leak')
            return token

    Secret.Schema = Schema

    class GI:
        @staticmethod
        def require_version(namespace, version):
            assert (namespace, version) == ('Secret', '1')

    original = credentials.importlib.import_module

    def import_module(name):
        if name == 'gi':
            if import_failure:
                raise RuntimeError('synthetic-missing-keyring-do-not-leak')
            return GI
        if name == 'gi.repository.Secret':
            return Secret
        return original(name)

    monkeypatch.setattr(credentials.importlib, 'import_module', import_module)


def test_keyring_auth_injects_token_only_into_op_environment(capsys, configuration, op, monkeypatch):
    token = 'ops_synthetic-keyring-token'
    _enable_keyring(configuration)
    _install_synthetic_keyring(monkeypatch, token=token)
    monkeypatch.setenv('OP_SERVICE_ACCOUNT_TOKEN', 'inherited-token-must-not-win')

    code, result = invoke(capsys, configuration, 'list')
    assert code == 0
    assert token not in json.dumps(result)
    assert all(token not in str(call[0]) for call in op.calls)
    assert all(call[1]['env']['OP_SERVICE_ACCOUNT_TOKEN'] == token for call in op.calls)
    assert all(call[1]['env']['OP_SERVICE_ACCOUNT_TOKEN'] != 'inherited-token-must-not-win' for call in op.calls)
    assert __import__('os').environ['OP_SERVICE_ACCOUNT_TOKEN'] == 'inherited-token-must-not-win'


@pytest.mark.parametrize('failure', ['missing', 'invalid', 'lookup', 'import'])
def test_keyring_auth_failure_never_falls_back_or_calls_op(capsys, configuration, op, monkeypatch, failure):
    _enable_keyring(configuration)
    _install_synthetic_keyring(monkeypatch, token=None if failure == 'missing' else ('bad-token' if failure == 'invalid' else 'ops_synthetic-token'),
                               failure=failure == 'lookup', import_failure=failure == 'import')
    monkeypatch.setenv('OP_SERVICE_ACCOUNT_TOKEN', 'inherited-token-must-not-fallback')
    code, result = invoke(capsys, configuration, 'list')
    assert code != 0
    assert result['error']['code'] == 'service_account_unavailable'
    assert 'synthetic-keyring-error-do-not-leak' not in json.dumps(result)
    assert 'synthetic-missing-keyring-do-not-leak' not in json.dumps(result)
    assert 'inherited-token-must-not-fallback' not in json.dumps(result)
    assert op.calls == []


def test_keyring_is_not_accessed_when_disabled_or_unspecified(capsys, configuration, op, monkeypatch):
    from qemu_mcp import credentials
    original = credentials.importlib.import_module

    def import_module(name):
        assert name != 'gi'
        return original(name)

    monkeypatch.setattr(credentials.importlib, 'import_module', import_module)
    assert invoke(capsys, configuration, 'list')[0] == 0
    configuration[1]['service_account_keyring'] = False
    configuration[0].write_text(json.dumps(configuration[1]))
    assert invoke(capsys, configuration, 'list')[0] == 0


def test_item_detail_exposes_field_descriptors_without_values(capsys, configuration, op):
    code, result = invoke(capsys, configuration, 'list', '--item', ITEM)
    assert code == 0
    assert result == {
        'item': {'id': ITEM, 'title': 'Example', 'vault': {'id': AGENTS, 'name': 'Agents'}, 'category': 'LOGIN'},
        'fields': [{'id': 'username', 'label': 'username', 'type': 'STRING'},
                   {'id': 'password', 'label': 'password', 'type': 'CONCEALED'}],
    }
    assert len(op.calls) == 3
    assert '--format=json' in op.calls[-1][0]


@pytest.mark.parametrize('mutation', ['outside', 'wrong_id', 'archived'])
def test_item_detail_rejects_mismatched_or_archived_response(capsys, configuration, op, mutation):
    if mutation == 'outside':
        op.details['vault']['id'] = 'f' * 26
    elif mutation == 'wrong_id':
        op.details['id'] = OTHER
    else:
        op.details['state'] = 'ARCHIVED'
    code, result = invoke(capsys, configuration, 'list', '--item', ITEM)
    assert code != 0
    assert result['error']['code'] == 'inaccessible_item'


@pytest.fixture
def guest(monkeypatch, host, keyboard_clock):
    import sys

    monkeypatch.setattr(host.binding, 'registerErrorHandler', lambda handler, context: None, raising=False)
    monkeypatch.setitem(sys.modules, 'libvirt', host.binding)
    monkeypatch.setitem(sys.modules, 'libvirt_qemu', host.qemu)
    return host


def pressed(host):
    return [event['data']['key']['data'] for event in host.domain.events
            if event['type'] == 'key' and event['data']['down']]


@pytest.mark.parametrize('selector,value,expected', [
    ('password', ' Ab! ', ['spc', 'shift', 'a', 'b', 'shift', '1', 'spc']),
    ('username', 'synthetic-user', list('synthetic') + ['minus'] + list('user')),
    ('Custom label', 'xY', ['x', 'shift', 'y']),
    ('custom_id', 'xY', ['x', 'shift', 'y']),
])
def test_success_types_selected_value_once_and_closes(capsys, configuration, op, guest, selector, value, expected):
    if selector == 'password':
        op.details['fields'][1]['value'] = value
    elif selector in ('Custom label', 'custom_id'):
        op.details['fields'].append({'id': 'custom_id', 'label': 'Custom label', 'type': 'STRING', 'value': value})
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM, '--field', selector)
    assert code == 0
    assert result == {'typed': True, 'guest_text_verified': False}
    assert pressed(guest) == expected
    assert 'ret' not in pressed(guest)
    assert guest.domain.actions == []
    assert all(connection.closed for connection in guest.connections)
    assert len(guest.connections) == 1
    assert guest.connections[0].streams == []


def test_otp_uses_authorized_otp_command(capsys, configuration, op, guest):
    op.details['fields'].append({'id': 'totp', 'label': 'One-time password', 'type': 'OTP', 'value': SECRET})
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM, '--field', 'otp')
    assert code == 0
    assert result['typed'] is True
    assert pressed(guest) == list('123456')
    arguments = op.calls[-1][0]
    assert arguments[:4] == ['op', 'item', 'get', ITEM]
    assert arguments[arguments.index('--vault') + 1] == AGENTS
    assert '--otp' in arguments


@pytest.mark.parametrize('value', ['', None, 123, 'a\nb', 'a\rb', 'a\tb', 'a\x00b', 'a\x7fb', 'é', '한'])
def test_invalid_value_never_connects_to_guest(capsys, configuration, op, guest, value):
    op.details['fields'][1]['value'] = value
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM)
    assert code != 0
    assert result['error']['code'] in ('unavailable_field', 'invalid_input')
    assert guest.connections == []
    assert guest.domain.events == []


def test_missing_and_ambiguous_fields_never_connect(capsys, configuration, op, guest):
    op.details['fields'] = [{'id': 'one', 'label': 'duplicate', 'type': 'STRING', 'value': SECRET},
                           {'id': 'two', 'label': 'duplicate', 'type': 'STRING', 'value': SECRET}]
    for selector in ('password', 'duplicate', 'op://elsewhere/password'):
        code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM, '--field', selector)
        assert code != 0
        assert result['error']['code'] == 'unavailable_field'
    assert guest.connections == []


def test_membership_is_rechecked_before_every_detail_or_type(capsys, configuration, op, guest):
    assert invoke(capsys, configuration, 'list', '--item', ITEM)[0] == 0
    op.listings[AGENTS] = []
    calls = len(op.calls)
    for arguments in [('list', '--item', ITEM), ('type', '--vm', 'desktop', '--item', ITEM)]:
        code, result = invoke(capsys, configuration, *arguments)
        assert code != 0
        assert result['error']['code'] == 'inaccessible_item'
    assert len(op.calls) == calls + 4
    assert guest.connections == []


def test_partial_input_failure_is_sanitized_without_replay(capsys, configuration, op, guest):
    guest.domain.fail_input = True
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM)
    assert code != 0
    assert result['error']['code'] == 'input_may_be_partial'
    assert pressed(guest) == ['s']
    assert guest.connections[0].closed


@pytest.mark.parametrize('stage', ['attach', 'typing', 'interrupt'])
def test_backend_exception_cannot_leak_secret(capsys, configuration, op, guest, monkeypatch, stage):
    from qemu_mcp import credentials

    def fail(*args, **kwargs):
        if stage == 'interrupt':
            raise KeyboardInterrupt(SECRET)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(credentials.Controller, 'attach' if stage == 'attach' else 'type_text', fail)
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM)
    assert code != 0
    assert result['error']['code'] == ('backend_failure' if stage == 'attach' else 'input_may_be_partial')
    assert all(connection.closed for connection in guest.connections)


def test_invalid_subprocess_response_is_sanitized(capsys, configuration, op):
    op.listings[AGENTS] = SECRET
    code, result = invoke(capsys, configuration, 'list')
    assert code != 0
    assert result['error']['code'] == 'op_failed'


def test_uuid_name_fallback_cannot_target_another_guest(capsys, configuration, op, guest):
    guest.domain.uuid = '99999999-1234-1234-1234-123456789abc'
    guest.domain.label = VM
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM)
    assert code != 0
    assert result['error']['code'] == 'backend_failure'
    assert guest.domain.events == []
    assert guest.connections[0].closed


def test_real_cli_process_uses_synthetic_op_only(tmp_path, configuration, monkeypatch):
    import os
    import sys

    script = tmp_path / 'op'
    script.write_text(f'''#!{sys.executable}
import json
import os
import sys
assert sys.stdin.read() == ''
assert sys.argv[1:3] == ['item', 'list']
assert sys.argv[sys.argv.index('--account') + 1] == {ACCOUNT!r}
vault = sys.argv[sys.argv.index('--vault') + 1]
assert vault in ({AGENTS!r}, {SHARED!r})
assert 'OP_CONNECT_HOST' not in os.environ
assert 'OP_INCLUDE_ARCHIVE' not in os.environ
print('synthetic-private-stderr', file=sys.stderr)
print(json.dumps([{{'id': {ITEM!r}, 'title': 'Synthetic', 'vault': {{'id': vault}},
                   'category': 'LOGIN', 'additional_information': {SECRET!r}}}]))
''')
    script.chmod(0o700)
    environment = dict(os.environ, PATH=str(tmp_path), OP_CONNECT_HOST='synthetic-redirect', OP_INCLUDE_ARCHIVE='true')
    result = subprocess.run([sys.executable, '-m', 'qemu_mcp.credentials', 'list', '--config', str(configuration[0])],
                            capture_output=True, text=True, timeout=30, env=environment, stdin=subprocess.DEVNULL)
    assert result.returncode == 0
    assert result.stderr == ''
    assert SECRET not in result.stdout
    assert 'synthetic-private-stderr' not in result.stdout
    items = json.loads(result.stdout)['items']
    assert [item['vault']['name'] for item in items] == ['Agents', 'Shared Vault']
    assert all(set(item) == {'id', 'title', 'vault', 'category'} for item in items)


def test_malformed_op_json_never_echoes_raw_response(capsys, configuration, monkeypatch):
    def malformed(arguments, **kwargs):
        return subprocess.CompletedProcess(arguments, 0, SECRET, SECRET)

    monkeypatch.setattr(subprocess, 'run', malformed)
    code, result = invoke(capsys, configuration, 'list')
    assert code != 0
    assert result['error']['code'] == 'op_failed'


def test_libvirt_error_containing_secret_is_sanitized(capsys, configuration, op, guest, monkeypatch):
    def fail(*args):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(guest.qemu, 'qemuMonitorCommand', fail)
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM)
    assert code != 0
    assert result['error']['code'] == 'input_may_be_partial'
    assert guest.connections[0].closed


def test_uppercase_account_uuid_is_preserved_for_op(capsys, configuration, op):
    account = 'A1B2C3D4E5F6G7H8I9J0K1L2M3'
    path, data = configuration
    data['account'] = account
    path.write_text(json.dumps(data))
    op.account = account
    code, result = invoke(capsys, configuration, 'list')
    assert code == 0
    assert len(result['items']) == 2
    assert len(op.calls) == 2
    assert all(arguments[arguments.index('--account') + 1] == account
               for arguments, _ in op.calls)


@pytest.mark.parametrize('selector', ['otp', 'secondary_otp', 'Secondary OTP'])
def test_multiple_otp_fields_are_rejected_without_retrieval_or_guest_input(capsys, configuration, op, guest, selector):
    op.details['fields'].extend([
        {'id': 'primary_otp', 'label': 'Primary OTP', 'type': 'OTP', 'value': SECRET},
        {'id': 'secondary_otp', 'label': 'Secondary OTP', 'type': 'OTP', 'value': SECRET},
    ])
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM, '--field', selector)
    assert code != 0
    assert result['error']['code'] == 'unavailable_field'
    assert all('--otp' not in arguments for arguments, _ in op.calls)
    assert guest.connections == []
    assert guest.domain.events == []


def test_otp_alias_without_otp_field_is_rejected_before_retrieval(capsys, configuration, op, guest):
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM, '--field', 'otp')
    assert code != 0
    assert result['error']['code'] == 'unavailable_field'
    assert all('--otp' not in arguments for arguments, _ in op.calls)
    assert guest.connections == []


@pytest.mark.parametrize('field', [
    {'id': 'otp', 'label': 'Literal code', 'type': 'STRING', 'value': 'literal'},
    {'id': 'custom', 'label': 'otp', 'type': 'STRING', 'value': 'literal'},
])
def test_non_otp_field_named_otp_uses_its_stored_string(capsys, configuration, op, guest, field):
    op.details['fields'].extend([
        {'id': 'totp', 'label': 'One-time password', 'type': 'OTP', 'value': SECRET}, field,
    ])
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM, '--field', 'otp')
    assert code == 0
    assert result['typed'] is True
    assert pressed(guest) == list('literal')
    assert all('--otp' not in arguments for arguments, _ in op.calls)


def test_otp_label_ambiguity_is_rejected(capsys, configuration, op, guest):
    op.details['fields'].extend([
        {'id': 'totp', 'label': 'otp', 'type': 'OTP', 'value': SECRET},
        {'id': 'literal', 'label': 'otp', 'type': 'STRING', 'value': 'literal'},
    ])
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM, '--field', 'otp')
    assert code != 0
    assert result['error']['code'] == 'unavailable_field'
    assert all('--otp' not in arguments for arguments, _ in op.calls)
    assert guest.connections == []


def test_native_libvirt_diagnostic_is_suppressed_before_connect(capsys, configuration, op, guest, monkeypatch):
    import sys

    state = {'handler': None, 'context': None}

    def register(handler, context):
        state.update(handler=handler, context=context)

    def fail_connection(uri):
        if state['handler'] is None:
            print(SECRET, file=sys.stderr)
        else:
            state['handler'](state['context'], (SECRET,))
        raise RuntimeError(SECRET)

    monkeypatch.setattr(guest.binding, 'registerErrorHandler', register)
    monkeypatch.setattr(guest.binding, 'open', fail_connection)
    code, result = invoke(capsys, configuration, 'type', '--vm', 'desktop', '--item', ITEM)
    assert code != 0
    assert result['error']['code'] == 'backend_failure'
    assert state['handler'] is not None
    assert guest.domain.events == []
