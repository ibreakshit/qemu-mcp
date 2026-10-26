# Contributing

Use Python 3.10+ on Linux. Install distribution libvirt development packages (`libvirt-dev`, `pkg-config`, `python3-dev` on Debian/Ubuntu), then:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
ruff check src tests
mypy --strict src
pytest
```

The fake-libvirt tests and real MCP stdio handshake require no guest, daemon or desktop. Importing the server must remain usable without libvirt bindings. Keep handlers thin; backend and controller tests cover UUID identity/reconnection, stream completion/abort, input ordering/serialization, validation and no guest shutdown on detach/exit.

For an opt-in live exercise, explicitly choose an existing running guest:

```sh
python tests/smoke_test.py --name Windows --output screenshot.png
```

This lists, attaches, reads status, captures resolution/screens and detaches. It performs no power or snapshot operations. Add `--exercise-input` only when you intend to send Shift and move the pointer to the top-left. Screenshots never wake a sleeping display automatically. The script is guarded so ordinary pytest collection never runs live actions.

Use `QEMU_MCP_LIBVIRT_URI=qemu:///session` when testing session domains; the default is `qemu:///system`. Keep virt-manager open during live acceptance to check viewer coexistence. Verify the guest layout/IME before expecting typed ANSI US positions to produce particular text.

Record non-blocking limitations for review after the first test drive in `docs/testing/mvp-deferred-issues.md`. Do not add guest creation, private-QMP socket access, snapshots, serial, guest-agent or clipboard features to this MVP.
