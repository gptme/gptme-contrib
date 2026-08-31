# Pi v3 session fixtures

These are credential-scanned, sanitized copies of retained Pi 0.84.4 native
tree-JSONL sessions. Opaque provider response IDs/signatures, reasoning text,
and host-local absolute paths were removed or replaced. Token counts, costs,
stop reasons, tool structure, timestamps, and the real fixture commit hash are
preserved.

- `productive-codex.jsonl` is a real OpenAI Codex subscription run that used
  Pi's `write` and `bash` tools and created commit `526d692`.
- `noop-codex.jsonl` and `noop-xai.jsonl` are real subscription smoke runs that
  only returned sentinels. They are genuine NOOPs, not parser failures.

The raw retained sessions are intentionally not part of this repository.
