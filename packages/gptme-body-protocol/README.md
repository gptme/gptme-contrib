# gptme-body-protocol

Small, transport-neutral wire models (DTOs + newline-framed JSON) for connecting
a gptme "brain" — such as [gptme-voice](../gptme-voice/README.md) — to a
physical or simulated **body node** (robot, drone, rover) that accepts bounded
movement goals.

**Status:** experimental (`0.1.0`, protocol `bob-body/0`). The wire format may
change in a new protocol version; the version string is checked on every
handshake so mismatched peers fail loudly.

## Why it exists

`gptme-voice` and a body node need to agree on controller-authenticated
handshakes and bounded goal commands without importing one another's runtime.
This package is that shared seam: zero runtime dependencies, stdlib only. The
`bob-body/0` protocol supports:

- a controller handshake: the client presents the bearer token, and the body
  must echo `bob-body/0`. That authenticates the controller to the body, not
  the body to the controller. Mutual proof is a later protocol.
- stable controller and command IDs (command results must repeat `command_id`);
- command TTLs (`ttl_ms`, default 2000) and a send timestamp (`sent_at_ms`);
- `status`, relative `move`, relative `turn`, preemptive `stop`, and `interact`;
- newline-framed JSON encoding for the current native-local transport.

The package defines DTOs and framing only. Controller leases, idempotency,
deadman behavior, collision safety, and telemetry production belong to the body
node. Model-facing schemas, global request bounds, and the loopback-only
transport policy belong to `gptme-voice` (see its
[remote body node](../gptme-voice/README.md#remote-body-node) section).

## Install

Not published to PyPI. Install from the repository subdirectory:

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-body-protocol"
```

Inside the gptme-contrib uv workspace it is a workspace member and is pulled in
automatically as a dependency of `gptme-voice`.

## Usage

```python
from gptme_body_protocol import (
    Command,
    Handshake,
    decode_message,
    encode_message,
    require_command_result,
    require_handshake_ok,
)

# Client -> body: one JSON object per line
frame = encode_message(Handshake(token="<token>", controller_id="voice"))
# b'{"token":"<token>","controller_id":"voice","protocol":"bob-body/0","type":"handshake"}\n'

# Body -> client: validate the handshake reply
require_handshake_ok(decode_message(reply_line))  # raises on rejection/mismatch

cmd = Command(
    command_id="move-1",
    controller_id="voice",
    command="move",
    args={"forward_m": 1.5},
    ttl_ms=250,
)
writer.write(encode_message(cmd))
require_command_result(decode_message(await reader.readline()), cmd.command_id)
```

## API

| Name | Purpose |
|------|---------|
| `PROTOCOL_VERSION` | `"bob-body/0"` |
| `Handshake(token, controller_id)` | Frozen dataclass; `type="handshake"` |
| `Command(command_id, controller_id, command, args={}, ttl_ms=2000)` | Frozen dataclass; `command` is one of `status`, `move`, `turn`, `stop`, `interact` |
| `encode_message(msg) -> bytes` | Compact JSON + trailing newline |
| `decode_message(line) -> dict` | Rejects non-object JSON and a foreign `protocol` value |
| `require_handshake_ok(resp)` | Raises `PermissionError` unless `type == "handshake_ok"`; `ValueError` on protocol mismatch |
| `require_command_result(resp, command_id)` | Raises `ValueError` on wrong `type` or mismatched `command_id` |

The `args` payload for each command and the `capabilities` list in
`handshake_ok` are defined by the body node and its client adapter, not by this
package. The reference client is `gptme_voice.body.remote_adapter.RemoteAdapter`
in [gptme-voice](../gptme-voice/README.md).
