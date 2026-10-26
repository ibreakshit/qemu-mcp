"""Local libvirt operations. No guest creation or private QMP sockets."""
from __future__ import annotations

import importlib
import io
import json
import threading
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from PIL import Image

_STATES = {0: 'nostate', 1: 'running', 2: 'blocked', 3: 'paused', 4: 'shutdown', 5: 'shutoff', 6: 'crashed', 7: 'pmsuspended'}


@dataclass(frozen=True)
class Screenshot:
    png: bytes
    width: int
    height: int

    def image(self) -> Image.Image:
        with Image.open(io.BytesIO(self.png)) as image:
            return image.convert('RGB')


class LibvirtBackend:
    def __init__(self, uri: str = 'qemu:///system', *, binding: Any = None, qemu_binding: Any = None):
        if uri not in ('qemu:///system', 'qemu:///session'):
            raise ValueError('Use a local qemu:///system or qemu:///session URI')
        self.uri = uri
        self._binding = binding
        self._qemu_binding = qemu_binding
        self._connection: Any = None
        self._connection_lock = threading.RLock()

    def connection(self) -> Any:
        with self._connection_lock:
            if self._connection is not None:
                try:
                    if self._connection.isAlive() == 1:
                        return self._connection
                except Exception:
                    pass
                self.close()
            try:
                if self._binding is None:
                    self._binding = importlib.import_module('libvirt')
                self._connection = self._binding.open(self.uri)
                if self._connection is None:
                    raise RuntimeError('connection unavailable')
                return self._connection
            except Exception:
                raise RuntimeError('Cannot connect to local libvirt. Install libvirt-python, start libvirtd/virtqemud and check access to the configured URI.') from None

    def close(self) -> None:
        with self._connection_lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def resolve(self, identifier: str, *, uuid_only: bool = False) -> Any:
        connection = self.connection()
        try:
            uuid = str(UUID(identifier))
        except ValueError:
            uuid = None
        if uuid is not None:
            try:
                return connection.lookupByUUIDString(uuid)
            except Exception:
                pass
        if not uuid_only:
            try:
                return connection.lookupByName(identifier)
            except Exception:
                pass
        raise RuntimeError('Domain not found or unavailable. Check qemu_list and the local libvirt connection.')

    def domains(self) -> list[Any]:
        try:
            return list(self.connection().listAllDomains(0))
        except Exception:
            raise RuntimeError('Cannot list domains. Check the local libvirt connection and permissions.') from None

    def identity(self, domain: Any) -> dict[str, Any]:
        try:
            return {'name': domain.name(), 'uuid': domain.UUIDString(), 'state': _STATES.get(domain.state()[0], 'unknown')}
        except Exception:
            raise RuntimeError('Cannot read domain state. Retry after checking the local libvirt connection.') from None

    def power(self, domain: Any, action: str) -> None:
        try:
            if action == 'start':
                domain.create()
            elif action == 'shutdown':
                domain.shutdown()
            elif action == 'reboot':
                domain.reboot(0)
            elif action == 'force_stop':
                domain.destroy()
            else:
                raise ValueError('Unknown power action')
        except ValueError:
            raise
        except Exception:
            raise RuntimeError('Power request failed. Check domain state and permissions; no automatic escalation was attempted.') from None

    def capture(self, domain: Any) -> Screenshot:
        stream = None
        try:
            stream = self.connection().newStream(0)
            domain.screenshot(stream, 0, 0)
            data = bytearray()
            while chunk := stream.recv(65536):
                data.extend(chunk)
            stream.finish()
            stream = None
            with Image.open(io.BytesIO(data)) as source:
                image = source.convert('RGB')
            output = io.BytesIO()
            image.save(output, format='PNG')
            return Screenshot(output.getvalue(), image.width, image.height)
        except Exception:
            raise RuntimeError('Cannot capture primary display. Check that the domain is running and has a supported graphics display.') from None
        finally:
            if stream is not None:
                try:
                    stream.abort()
                except Exception:
                    pass

    def input(self, domain: Any, events: list[dict[str, Any]]) -> None:
        try:
            if self._qemu_binding is None:
                self._qemu_binding = importlib.import_module('libvirt_qemu')
            response = self._qemu_binding.qemuMonitorCommand(domain, json.dumps({'execute': 'input-send-event', 'arguments': {'events': events}}), 0)
            result = json.loads(response)
            if 'error' in result:
                raise RuntimeError('monitor rejected input')
        except Exception:
            raise RuntimeError('Input failed; delivery may be partial. Check domain state, monitor permissions and an absolute pointer device. Input was not replayed.') from None
