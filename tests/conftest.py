"""Libvirt stand-ins: no daemon, display server, or guest is needed."""
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image


class FakeStream:
    def __init__(self):
        data = io.BytesIO()
        Image.new('RGB', (100, 50), 'white').save(data, format='PPM')
        self.data = data.getvalue()
        self.finished = False
        self.aborted = False
        self.fail = False

    def recv(self, size):
        if self.fail:
            raise RuntimeError('stream failed')
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def finish(self):
        self.finished = True

    def abort(self):
        self.aborted = True


class FakeDomain:
    def __init__(self):
        self.uuid = '12345678-1234-1234-1234-123456789abc'
        self.label = 'windows'
        self.state_code = 5
        self.actions = []
        self.events = []
        self.fail_input = False

    def UUIDString(self):
        return self.uuid

    def name(self):
        return self.label

    def state(self):
        return self.state_code, 0

    def create(self):
        self.actions.append('start')
        self.state_code = 1

    def shutdown(self):
        self.actions.append('shutdown')

    def reboot(self, flags):
        self.actions.append('reboot')

    def destroy(self):
        self.actions.append('destroy')
        self.state_code = 5

    def screenshot(self, stream, screen, flags):
        assert (screen, flags) == (0, 0)
        return 'image/x-portable-pixmap'


class FakeConnection:
    def __init__(self, domain):
        self.domain = domain
        self.alive = True
        self.closed = False
        self.streams = []

    def isAlive(self):
        return int(self.alive)

    def listAllDomains(self, flags):
        return [self.domain]

    def lookupByName(self, name):
        if name != self.domain.label:
            raise RuntimeError('not found')
        return self.domain

    def lookupByUUIDString(self, uuid):
        if uuid != self.domain.uuid:
            raise RuntimeError('not found')
        return self.domain

    def newStream(self, flags):
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def close(self):
        self.closed = True


@pytest.fixture
def host():
    domain = FakeDomain()
    connections = []

    def connect(uri):
        assert uri in ('qemu:///system', 'qemu:///session')
        connection = FakeConnection(domain)
        connections.append(connection)
        return connection

    def monitor(dom, command, flags):
        assert flags == 0
        events = json.loads(command)['arguments']['events']
        dom.events.extend(events)
        if dom.fail_input:
            dom.fail_input = False
            raise RuntimeError('uncertain input secret')
        return '{"return": {}}'

    return SimpleNamespace(domain=domain, connections=connections, binding=SimpleNamespace(open=connect), qemu=SimpleNamespace(qemuMonitorCommand=monitor))


@pytest.fixture
def controller(host):
    from qemu_mcp.backend import LibvirtBackend
    from qemu_mcp.vm import Controller
    return Controller(LibvirtBackend(binding=host.binding, qemu_binding=host.qemu))


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def keyboard_clock(monkeypatch, host):
    from qemu_mcp import control

    clock = SimpleNamespace(now=0.0, events=[])

    def sleep(seconds):
        clock.now += seconds

    monitor = host.qemu.qemuMonitorCommand

    def record(domain, command, flags):
        for event in json.loads(command)['arguments']['events']:
            if event['type'] == 'key':
                clock.events.append((domain.uuid, clock.now, event['data']))
        return monitor(domain, command, flags)

    monkeypatch.setattr(control.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(control.time, 'sleep', sleep)
    monkeypatch.setattr(host.qemu, 'qemuMonitorCommand', record)
    return clock
