# qemu-mcp

A local Linux MCP controller for **existing libvirt QEMU domains**, including Windows guests. It uses libvirt for discovery, lifecycle and primary-display screenshots, and `libvirt_qemu.qemuMonitorCommand` for explicit keyboard and mouse events. It never creates guests or opens libvirt's private QMP socket.

Keep using virt-manager and SPICE on the same desktop. Opening or closing a viewer does not affect MCP attachments. Guest video, SPICE and input device configuration stay as configured in libvirt. Simultaneous human and agent input may interfere.

## Install and connect

Use Linux, Python 3.10+, an accessible local libvirt QEMU daemon and existing domains. Install your distribution's libvirt development packages first, for example on Debian/Ubuntu:

```sh
sudo apt install libvirt-dev pkg-config python3-dev
python -m pip install .
```

The host user needs permission to connect to libvirt and run QEMU monitor input commands. Configure that access using your distribution's libvirt policy. The server runs with that user's access to the guests.

Configure your MCP client to launch:

```json
{
  "mcpServers": {
    "qemu": {
      "command": "qemu-mcp",
      "env": {"QEMU_MCP_LIBVIRT_URI": "qemu:///system"}
    }
  }
}
```

`qemu:///system` is the default; `qemu:///session` is the only alternative. Remote connections are rejected. Launch `python -m qemu_mcp` for the same stdio server. Libvirt bindings are imported on first use, so listing MCP tools works without a local libvirt installation.

## Workflow

1. Call `qemu_list()` to discover running and stopped domains.
2. Call `qemu_attach(name="Windows")` with a domain name or UUID. Attachments retain the UUID across domain renames and connection recovery. Use that UUID or the originally attached name in later calls.
3. Capture with `qemu_screenshot`, inspect dimensions, then use input tools.
4. Call `qemu_detach` when done. Detaching and server exit release held input best-effort and close connections, without shutting down guests.

Stopped guests can attach for power management. `qemu_start` starts an existing attached guest. `qemu_shutdown` and `qemu_reboot` make graceful requests and return the actual current state; an accepted request may still report running. There is no forced fallback. `qemu_force_stop` explicitly invokes libvirt destroy and can lose unsaved guest work.

## Tools

| Tool | Behavior |
| --- | --- |
| `qemu_list()` | All existing domains with name, UUID, state and attached status |
| `qemu_attach(name)` | Attach by name or UUID without starting the guest |
| `qemu_detach(name)` | Release input best-effort and detach without powering off |
| `qemu_status(name)` | Current state and identity of an attached guest |
| `qemu_start/shutdown/reboot/force_stop(name)` | Explicit lifecycle request and actual state |
| `qemu_screenshot(name)` | Full-resolution primary-display PNG plus width/height |
| `qemu_get_resolution(name)` | Width/height through the same capture path |
| `qemu_wait_screen(name, timeout_s=30, poll_interval_s=1, stable_polls=3, max_changed_pixels=64)` | Structured settled/timeout, black-image flag and unknown readiness |
| `qemu_type(name, text, delay_ms=35)` | Physical ANSI US key positions; returns `characters_sent`, `guest_text_verified=false`, `key_event_interval_ms=20` |
| `qemu_key(name, combo, state='click')` | Chords such as `ctrl-alt-f1` or `rightalt`; press, release or click |
| `qemu_release_all(name)` | Release keys/buttons held by this attachment |
| `qemu_mouse_move(name, x, y, coordinate_mode='pixels')` | Absolute pointer movement |
| `qemu_mouse_click(name, button='left', x=None, y=None, state='click', count=1, coordinate_mode='pixels')` | Click/double-click or press/release, with optional coordinate pair |
| `qemu_mouse_scroll(name, delta_y, delta_x=0)` | Signed integer wheel steps; positive down/right, up to 100 per axis |
| `qemu_mouse_drag(name, start_x, start_y, end_x, end_y, button='left', duration_ms=500, coordinate_mode='pixels')` | Serialized move, press, interpolated movement and final release |

Input actions run sequentially per domain within one server/controller instance; different domains are independent. Separate helper processes, other server instances and human viewers do not share that input lock. On input errors, held-input cleanup is attempted and uncertain input is never replayed automatically. Subsequent requests reconnect when the connection has failed and resolve attachments by UUID.

### Input coordinates and layout

The default coordinate mode is native screenshot pixels: `0 <= x <= width-1` and `0 <= y <= height-1`. Pass `coordinate_mode="normalized"` explicitly for 0..1 coordinates. Values must be finite and within bounds; there is no clamping or hidden screenshot scaling. Absolute positioning requires a suitable existing guest input device, commonly a USB tablet. The controller never adds devices or calibrates the pointer.

Text is validated in full before sending any keys. Only physical ANSI US positions are mapped; Windows or the guest owns keyboard layout and IME. There is no automatic layout change, Unicode injection or clipboard. Every keyboard press and release is spaced by at least 20 ms per domain, including modifiers, cleanup and consecutive tool calls; chords release in reverse order. `delay_ms` accepts 0..1000 (default 35) as an additional inter-character pause and cannot bypass this floor. `characters_sent` counts characters whose input events the transport accepted, with `guest_text_verified=false`; no typed content is echoed. Inspect screenshots and application counts to verify focus and delivery. The floor addresses observed USB-keyboard loss in the tested Windows guest; arbitrary guest load can still delay or lose input.

Use `rightalt`, `ralt`, `alt_r` or `altgr` for QEMU's right Alt key. `right-alt` retains its chord delimiter meaning (Right arrow plus left Alt). Right Ctrl aliases are `rightctrl`/`rctrl`/`ctrl_r`, and right Shift aliases are `rightshift`/`rshift`/`shift_r`. In the tested Windows Korean IME, focus the editor first, toggle with `rightalt`, type two-set physical keys and commit composition with space or Enter, then toggle back to English. Clicking the taskbar IME control followed by an editor click can reset the mode in this guest; the controller does not change language automatically.

Mouse buttons are left/right/middle; click count is 1 or 2, held states require 1. Drag duration is 0..10000 ms.

### Screens and readiness

Screenshots are read-only and never wake the display. A black frame can mean display sleep. Send explicit input if you want to wake the guest, then allow it to settle. `qemu_wait_screen` measures image stability, not application or OS readiness, and always reports `readiness="unknown"`. Its timeout is 0.01..300 seconds, polling interval 0.01..300 seconds, and stable polls 1..1000. Synchronous libvirt calls remain subject to the daemon's own response time.

## Credential CLI

Installing this package also installs `qemu-credential`, a local CLI that reads an authorized 1Password field and types it through the existing libvirt keyboard path. Install the 1Password CLI (`op`) separately and make it available on `PATH`. This helper is separate from the MCP tool interface. Local agent skills are not bundled or published in this repository.

### Configuration and authentication

Create `~/.config/qemu-credential/config.json` with this structure. All identifiers below are synthetic; replace them with your account, vault and existing VM identifiers:

```json
{
  "account": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
  "vaults": [
    {"id": "aaaaaaaaaaaaaaaaaaaaaaaaaa", "name": "Agents"},
    {"id": "bbbbbbbbbbbbbbbbbbbbbbbbbb", "name": "Shared Vault"}
  ],
  "libvirt_uri": "qemu:///system",
  "vms": {
    "windows-demo": "12345678-1234-5678-9abc-123456789abc"
  },
  "service_account_keyring": false
}
```

`account` is the 26-character alphanumeric account UUID; uppercase letters are valid. `vaults` must contain exactly two distinct 26-character lowercase alphanumeric IDs, with the names `Agents` and `Shared Vault` exactly once each. `vms` maps nonempty CLI aliases to existing libvirt domain UUIDs, rather than domain names. `libvirt_uri` defaults to `qemu:///system` and also accepts `qemu:///session`; remote URIs are rejected. The optional boolean `service_account_keyring` defaults to `false`. Pass `--config PATH` before or after the subcommand to use another configuration file. This CLI configuration is independent of the MCP server's environment setting.

With `service_account_keyring: false`, the helper uses the preauthenticated 1Password CLI session or an inherited `OP_SERVICE_ACCOUNT_TOKEN`. It explicitly selects the configured account and vaults and excludes archived items. For a service account, grant read-only access to the custom `Agents` and `Shared Vault` vaults. The default 1Password `Shared` vault is ineligible for service-account access; create a custom vault named `Shared Vault` instead.

To store a service-account token in the desktop Secret Service keyring, use a Linux desktop session with an available, unlocked keyring. On Ubuntu, the optional dependencies and a virtual environment that can import the system GI bindings can be installed as follows (from the repository):

```sh
sudo apt install libvirt-dev pkg-config python3-dev python3-venv python3-gi gir1.2-secret-1 gnome-keyring
/usr/bin/python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install .
```

The 1Password CLI is still installed separately. Run this snippet locally in an interactive terminal, using that same Python environment. It accepts the token through hidden input, stores it in the keyring and prints only a success message. Do not put the token in command arguments, environment dumps or files.

```sh
python - <<'PYTHON'
import getpass
import warnings
import gi

gi.require_version("Secret", "1")
from gi.repository import Secret

attributes = {"application": "qemu-credential", "credential": "service-account"}
schema = Secret.Schema.new(
    "local.qemu-credential",
    Secret.SchemaFlags.NONE,
    {name: Secret.SchemaAttributeType.STRING for name in attributes},
)
warnings.simplefilter("error", getpass.GetPassWarning)
with open("/dev/tty", "w") as terminal:
    token = getpass.getpass("1Password service-account token: ", stream=terminal)
if not token.startswith("ops_"):
    raise ValueError("Expected a 1Password service-account token")
if not Secret.password_store_sync(
    schema, attributes, Secret.COLLECTION_DEFAULT,
    "qemu-credential service account", token, None,
):
    raise RuntimeError("Keyring storage failed")
print("Service-account token stored successfully.")
PYTHON
```

The schema and both lookup attributes must match the snippet; creating an entry with only the label `qemu-credential service account` is insufficient. Set `service_account_keyring` to `true` after storing the token. In that mode, missing GI bindings, an unavailable keyring or a missing token cause `service_account_unavailable`; the helper does not fall back to desktop CLI authentication.

### List and type

```sh
qemu-credential list
qemu-credential list --item cccccccccccccccccccccccccc
qemu-credential type --vm windows-demo --item cccccccccccccccccccccccccc
qemu-credential type --vm windows-demo --item cccccccccccccccccccccccccc --field username
qemu-credential type --vm windows-demo --item cccccccccccccccccccccccccc --field otp
```

`list` returns JSON item metadata (`id`, `title`, `vault` and `category`) from the two configured vaults. `list --item ID` adds field metadata (`id`, `label` and `type`), without field values. Item selection requires a current item UUID. `type` requires `--vm` and `--item`; `--field` defaults to `password`. A field ID takes priority, then an exact unique label. `otp` also selects an OTP field when no ID or label matches. OTP delivery requires exactly one OTP field in the item because `op --otp` cannot select between multiple OTP fields.

Typing returns only `{"typed": true, "guest_text_verified": false}` on success. Failures return a fixed JSON error code/message and exit nonzero, without printing the selected value or upstream diagnostics. The entire nonempty value must be printable US ASCII (space through `~`); controls, including Tab, Enter and DEL, and non-ASCII text are rejected before typing.

The caller must focus the intended field, choose a compatible guest keyboard layout/IME and pause all competing guest input before invoking `type`. Separate helper processes, MCP servers and human viewers do not share an input lock. The helper does not clear fields, press Enter, take screenshots or retry input. If it reports `input_may_be_partial`, inspect the field before any manual retry. Transport acceptance does not guarantee the guest received the intended text. This local helper does not provide an isolation boundary against trusted code running as the same host user.

## Compatibility and testing

Version remains 0.1.0 for this MVP, with an intentional interface break. Boot, QEMU-version, serial, snapshots, raw-QMP and the old combined mouse/stop tools have been removed. There is no VM creation, guest agent, clipboard, guest command execution or file transfer.

See [CONTRIBUTING.md](CONTRIBUTING.md) for offline tests and the opt-in existing-guest exercise. Known limitations and post-test-drive follow-ups are in [docs/testing/mvp-deferred-issues.md](docs/testing/mvp-deferred-issues.md).
