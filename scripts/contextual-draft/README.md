# Contextual draft: preview-only prototype

A composable CLI foundation for a draft-at-cursor shortcut. Goldfish supplies
observations; explicit context files supply knowledge from an existing agent or
project. Generation delegates to `gptme llm generate`, without tools or automatic
workspace loading. No separate assistant, collector, index, or memory engine.

## Status

Implemented: read-only Goldfish retrieval, full-source fetching after search,
bounded context composition, one-shot macOS focused-field capture, and explicit
model generation to a private preview file.

Not implemented: graphical preview, installed global hotkey, automatic agent
memory search, surrounding live-window capture, insertion, or sending. A saved
request is a historical capture, **not** an insertion handle. No complete
end-to-end live application acceptance is claimed. The native helper can refuse
apps whose editable fields aren't exposed through macOS Accessibility.

## Requirements

- Python 3.10+, standard library only.
- Running Goldfish with its local API enabled. The adapter verifies the health
  service before reading/sending its token, refuses redirects, and bypasses proxies.
- macOS and Swift command-line tools for `--capture`, plus Accessibility permission
  for the launching process. The helper does not prompt for permissions or change
  focus. Synthetic target JSON works on other platforms for development.
- `gptme` on PATH for generation. Model and disclosure consent must be explicit.

## Workflow

Set `DRAFT_SCRIPT` to the **absolute** path of `scripts/contextual_draft.py` in
your checkout. Set `PRIVATE_DIR` to an existing private directory outside git.
For example, create a new directory with `mktemp -d`; keep its contents private
and choose their retention yourself.

### 1. Prepare locally

Run from a shortcut launcher while an editable field is focused:

```sh
python3 "$DRAFT_SCRIPT" prepare --capture \
  --instruction 'Draft a concise reply; preserve my existing intent' \
  --query 'the relevant person or project' \
  --context /absolute/path/to/selected-agent-context.txt \
  --output "$PRIVATE_DIR/request.json"
```

`--context` is repeatable and optional. Select only relevant material; the
prototype does not read the whole agent workspace. Omit `--query` to retrieve
bounded recent working memory instead. Search hits are fetched as full snapshots,
not used as excerpts. Large observations are truncated with an explicit marker.
The resulting request records source IDs and coverage limitations.

The output must not exist. Files are created with mode 0600. The prepare command
never invokes a model and does not print captured text. Review the JSON locally
before generation; it contains sensitive desktop and agent context.

For tests or initial development, replace `--capture` with
`--target /absolute/path/to/target.json`:

```json
{
  "schema_version": 1,
  "role": "AXTextArea",
  "secure": false,
  "editable": true,
  "text": "Could we meet tomorrow?",
  "selection": null
}
```

Explicit fixture input is caller-provided evidence, not verification of a live
field. Do not use a fixture to assert that an app is currently safe to edit.

### 2. Generate a preview, with explicit disclosure

```sh
python3 "$DRAFT_SCRIPT" generate \
  --request "$PRIVATE_DIR/request.json" \
  --model provider/model \
  --allow-model-disclosure \
  --output "$PRIVATE_DIR/draft.txt"
```

Replace `provider/model` with your chosen configured gptme model. This sends the
request to that model; local capture does not imply local inference. The model's
provider/configuration determines routing. The subprocess receives the request
on stdin, not in process arguments. It receives no execution tools. Provider
errors are not echoed because they can contain request data.

Read the resulting draft in your chosen local viewer. No automatic opening,
clipboard replacement, focus change, insertion, Return key, or send occurs.
Source references stay in `request.json`, outside the proposed message body.

## Hotkey boundary

The `prepare --capture` invocation is suitable for an existing shortcut launcher
such as skhd. A hotkey must launch it without activating a terminal first, or it
will capture the terminal instead of the intended field. No shortcut is installed
by this prototype. Use fresh output filenames per invocation; a repeated fixed
filename fails instead of overwriting earlier private captures.

The next UI integration should present progress/cancel and a user-invoked preview.
Insertion needs a separate live target identity/revalidation mechanism and tests;
the PID/role/window metadata here is deliberately insufficient to authorize it.

## Safety and limits

- Secure fields, unsupported roles, unavailable AX permissions, focus switches
  during capture, and non-writable fields fail closed. A focused field over 20 KB
  is refused; explicit context files are limited to 20 KB each, API responses to
  2 MB, observations to five, and the complete request to 100 KB.
- These checks cannot detect every sensitive *non-secure* field. Pick the target
  deliberately, keep Goldfish exclusions configured, and review the local request.
- Observations and reference files are untrusted evidence. The generation prompt
  separates them from the explicit instruction. Prompt separation is mitigation,
  not a guarantee against injection or private-information disclosure. Review drafts.
- A request is not an accepted task; a draft is not a sent promise. This tool does
  not mutate Goldfish, import tasks, or learn durable memories.
- Access uses the documented local API rather than depending on Goldfish's private
  SQLite schema. Linux capture/insertion and alternate observation providers are
  future work; the JSON/retrieval/generation path is platform independent.

## Validation

From the checkout, run the focused pytest test file and Swift typecheck:

```sh
python3 -m pytest tests/test_contextual_draft.py
swiftc -typecheck scripts/contextual-draft/capture.swift
```

Tests use a real loopback HTTP fixture and a synthetic generator executable.
They do not read a personal Goldfish archive, call a model, or edit an app.
