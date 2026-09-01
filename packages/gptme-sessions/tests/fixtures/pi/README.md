# Pi v3 session fixtures

These are credential-scanned, sanitized copies of retained Pi v3 native
tree-JSONL sessions. Opaque provider response IDs/signatures, reasoning text,
and host-local absolute paths were removed or replaced. Token counts, costs,
stop reasons, tool structure, timestamps, and the real fixture commit hash are
preserved.

Bob-owned Pi 0.84.4 fixtures:

- `productive-codex.jsonl` is a real OpenAI Codex subscription run that used
  Pi's `write` and `bash` tools and created commit `526d692`.
- `noop-codex.jsonl` and `noop-xai.jsonl` are real subscription smoke runs that
  only returned sentinels. They are genuine NOOPs, not parser failures.

Upstream fixtures come from the public
[`badlogicgames/pi-mono`](https://huggingface.co/datasets/badlogicgames/pi-mono)
session dataset published by Pi's maintainer. They retain real event shapes but
replace the source checkout path; the branch fixture also shortens visible text
while preserving its actual fork topology and usage:

| Fixture | Public source session | Coverage |
|---|---|---|
| `failed-tool-upstream.jsonl` | `2026-02-01T22-15-19-818Z_196e6e47-41b3-482b-bc5d-68879f35d570.jsonl` | `toolResult.isError` |
| `provider-error-upstream.jsonl` | `2026-01-19T12-38-59-280Z_2e618260-6230-4e7f-8190-e94d965f1bbe.jsonl` | provider error followed by recovery |
| `aborted-upstream.jsonl` | `2026-03-13T19-33-24-328Z_be5d8844-6169-4ee6-a3b5-ca54a045b712.jsonl` | aborted assistant turn |
| `compaction-upstream.jsonl` | `2026-02-12T17-26-14-662Z_660753c9-2014-4d6f-9261-2092802d8795.jsonl` | compaction record and cache accounting |
| `branch-upstream.jsonl` | `2026-02-06T21-03-05-913Z_4bde89ff-567c-4afa-9125-98d443a3e973.jsonl` | abandoned child plus active branch summary |

The Bob-owned raw retained sessions are intentionally not part of this
repository. The public upstream source names above provide provenance for the
sanitized regression copies.

`scripts/check_pi_compat.py` pins live catalog membership for models still in
Pi 0.84.4 (`claude-opus-4-5`, `claude-opus-4-6`, `gpt-5.4`, `gpt-5.6-luna`,
`grok-4.6`). `provider-error-upstream.jsonl` and `branch-upstream.jsonl` use
models already retired from that pin; they stay as parser-shape fixtures.
