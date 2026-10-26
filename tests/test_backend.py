import pytest

from qemu_mcp.backend import LibvirtBackend


def test_local_connections_only():
    for uri in ('qemu+ssh://host/system', 'qemu://host/system', 'test:///default'):
        with pytest.raises(ValueError):
            LibvirtBackend(uri)


def test_attachment_identity_reconnect_and_power(controller, host):
    assert controller.list()[0]['attached'] is False
    attached = controller.attach('windows')
    assert attached['uuid'] == host.domain.uuid
    assert attached['state'] == 'shutoff'
    host.domain.label = 'renamed'
    host.connections[-1].alive = False
    assert controller.status('windows')['name'] == 'renamed'
    assert len(host.connections) == 2
    assert host.connections[0].closed
    assert controller.list()[0]['attached'] is True
    assert controller.power('windows', 'start')['state'] == 'running'
    assert controller.power('windows', 'shutdown')['state'] == 'running'
    assert controller.power('windows', 'reboot')['state'] == 'running'
    assert host.domain.actions == ['start', 'shutdown', 'reboot']
    controller.detach('windows')
    controller.close()
    assert 'destroy' not in host.domain.actions
    assert host.connections[-1].closed


def test_force_stop_is_explicit(controller, host):
    controller.attach(host.domain.uuid)
    controller.power(host.domain.uuid, 'force_stop')
    assert host.domain.actions == ['destroy']


def test_screenshot_stream_completion_and_failure(controller, host):
    controller.attach('windows')
    frame = controller.screenshot('windows')
    assert (frame.width, frame.height) == (100, 50)
    assert frame.png.startswith(b'\x89PNG')
    assert host.connections[-1].streams[-1].finished
    connection = host.connections[-1]
    new_stream = connection.newStream

    def failed_stream(flags):
        stream = new_stream(flags)
        stream.fail = True
        return stream

    connection.newStream = failed_stream
    with pytest.raises(RuntimeError, match='capture'):
        controller.screenshot('windows')
    assert connection.streams[-1].aborted
    assert not host.domain.actions


def test_uuid_attachment_does_not_follow_reused_name(controller, host):
    controller.attach('windows')
    host.domain.uuid = '87654321-1234-1234-1234-123456789abc'
    with pytest.raises(RuntimeError, match='Domain not found'):
        controller.status('windows')
    assert host.domain.actions == []


def test_graceful_failure_never_escalates(controller, host):
    controller.attach('windows')

    def fail():
        raise RuntimeError('request rejected')

    host.domain.shutdown = fail
    with pytest.raises(RuntimeError, match='no automatic escalation'):
        controller.power('windows', 'shutdown')
    assert host.domain.actions == []


def test_detach_failure_still_removes_attachment(controller, host):
    controller.attach('windows')
    controller.key('windows', 'ctrl', 'press')
    host.domain.fail_input = True
    result = controller.detach('windows')
    assert result['inputs_released'] is False
    assert controller.list()[0]['attached'] is False
    assert host.domain.actions == []


def test_missing_libvirt_has_actionable_guidance(monkeypatch):
    import qemu_mcp.backend as backend_module

    def missing(name):
        raise ImportError('not installed')

    monkeypatch.setattr(backend_module.importlib, 'import_module', missing)
    backend = LibvirtBackend()
    with pytest.raises(RuntimeError, match='Install libvirt-python'):
        backend.connection()


@pytest.mark.parametrize('identifier,expected', [
    ('windows', [('name', 'windows')]),
    ('12345678-1234-1234-1234-123456789abc', [('uuid', '12345678-1234-1234-1234-123456789abc')]),
])
def test_resolve_uses_uuid_lookup_only_for_uuid_identifiers(controller, host, identifier, expected):
    connection = controller.backend.connection()
    calls = []
    uuid_lookup = connection.lookupByUUIDString
    name_lookup = connection.lookupByName

    def lookup_uuid(value):
        calls.append(('uuid', value))
        return uuid_lookup(value)

    def lookup_name(value):
        calls.append(('name', value))
        return name_lookup(value)

    connection.lookupByUUIDString = lookup_uuid
    connection.lookupByName = lookup_name
    assert controller.backend.resolve(identifier) is host.domain
    assert calls == expected


def test_uuid_looking_domain_name_retains_name_fallback(controller, host):
    host.domain.label = '87654321-1234-1234-1234-123456789abc'
    assert controller.backend.resolve(host.domain.label) is host.domain


def test_uuid_only_resolution_never_falls_back_to_name(controller, host):
    connection = controller.backend.connection()
    calls = []
    connection.lookupByName = lambda name: calls.append(name)
    with pytest.raises(RuntimeError, match='Domain not found'):
        controller.backend.resolve('missing', uuid_only=True)
    assert calls == []


def test_uuid_identifier_is_normalized_before_libvirt_lookup(controller, host):
    assert controller.backend.resolve('{' + host.domain.uuid.upper() + '}') is host.domain
