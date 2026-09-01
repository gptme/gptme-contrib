# gptme-body-protocol

Small, transport-neutral wire models for a gptme Brain↔Body connection.

`gptme-voice` and a body node need to agree on controller-authenticated
handshakes and bounded goal commands without importing one another's runtime.
This package is that shared seam. The initial `bob-body/0` protocol supports:

- a controller handshake: the client presents the bearer token, and the body
  must echo `bob-body/0`. That authenticates the controller to the body, not
  the body to the controller. Mutual proof is a later protocol.
- stable controller and command IDs (command results must repeat `command_id`);
- command TTLs;
- `status`, relative `move`, relative `turn`, preemptive `stop`, and `interact`;
- newline-framed JSON encoding for the current native-local transport.

The package defines DTOs and framing only. Controller leases, idempotency,
deadman behavior, collision safety, and telemetry production belong to the body
node. Model-facing schemas and global request bounds belong to `gptme-voice`.
