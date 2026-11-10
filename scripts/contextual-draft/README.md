# Contextual draft: preview-only prototype

A composable CLI foundation for a draft-at-cursor shortcut. Goldfish supplies
observations; explicit context files supply knowledge from an existing agent or
project. Generation delegates to `gptme llm generate`, without tools or automatic
workspace loading. No separate assistant, collector, index, or memory engine.

## Status

Implemented: read-only Goldfish retrieval, full-source fetching after search,
bounded context composition, one-shot macOS focused-field capture, and explicit
model generation to a private preview file.

A user-invoked Tk preview launcher is available below. Not implemented: installed
global hotkey, automatic agent memory search, surrounding live-window capture,
automatic sending. Experimental direct insertion is described below. A saved
request is a historical capture, **not** an insertion handle. No complete
end-to-end live application acceptance is claimed. The native helper can refuse
apps whose editable fields aren't exposed through macOS Accessibility.

## Requirements

- Python 3.10+, standard library only.
- A local Goldfish SQLite database with the inspected snapshot/working-memory/FTS5
  schema (observed user_version 62). Use `--goldfish-db /absolute/path/to/goldfish.db`
  to override the macOS default. Goldfish need not be running. The adapter opens
  SQLite with `mode=ro` and `query_only=ON`; it never reads API discovery/token files
  or connects to the daemon. Private schema changes can break this adapter.
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
bounded recent working memory instead. Search uses a literal FTS5 phrase and
returns source snapshots, not excerpts. This is not Goldfish's API ranking or
semantic retrieval. Large observations are truncated with an explicit marker.
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

The preview launcher below provides a user-invoked UI and local cancellation.
Insertion needs a separate live target identity/revalidation mechanism and tests;
the PID/role/window metadata here is deliberately insufficient to authorize it.

## Safety and limits

- Secure fields, unsupported roles, unavailable AX permissions, focus switches
  during capture, and non-writable fields fail closed. Any failed role, subrole or
  selection read refuses the field (only an explicit "no value"/"unsupported"
  answer counts as an absent subrole), so a transient AX error can never make a
  secure field look ordinary. A focused field over 20 KB once JSON-encoded is
  refused, which keeps every valid capture inside the 100 KB protocol line; explicit context files are limited to 20 KB each, source content
  to 12,000 characters per observation, observations to five, and the complete
  request to 100 KB. Target and explicit context always fit or the request is
  refused; observations are then added in retrieval order against the remaining
  byte budget, truncating the first that does not fit and dropping the rest (their
  snapshot IDs are recorded under `coverage.budget`). Source metadata is also
  bounded during SQL selection.
- These checks cannot detect every sensitive *non-secure* field. Pick the target
  deliberately, keep Goldfish exclusions configured, and review the local request.
- Observations and reference files are untrusted evidence. The generation prompt
  separates them from the explicit instruction. Prompt separation is mitigation,
  not a guarantee against injection or private-information disclosure. Review drafts.
- A request is not an accepted task; a draft is not a sent promise. This tool does
  not mutate Goldfish, import tasks, or learn durable memories.
- Retrieval deliberately uses read-only SQLite instead of the bearer-token HTTP
  API: an unauthenticated loopback health response does not prove daemon identity.
  A PID/port precheck alone would have a replacement race. No API token is needed
  or accessed, and there is no HTTP fallback. A future API adapter requires
  daemon-supported server authentication, not another health check.
- The database and selected reference files are trusted local input locations,
  not authenticated remote sources. Protect their filesystem permissions. This
  adapter does not defend against another process already able to alter the user's
  private files. SQLite manages its ordinary WAL reader coordination; this tool
  performs no application-data or schema writes and never checkpoints the DB.
- Input files must be regular files. Actual reads are bounded before decoding,
  including reference files; FIFOs and other special files are refused without
  waiting for a writer. Linux capture/insertion and alternate observation providers
  remain future work; retrieval/generation are platform independent.

## Validation

From the checkout, run the focused pytest test file and Swift typecheck:

```sh
python3 -m pytest tests/test_contextual_draft.py
swiftc -typecheck scripts/contextual-draft/capture.swift
swift scripts/contextual-draft/capture.swift --self-test  # AX-free policy checks
```

Tests use real temporary SQLite databases, a loopback impersonator that must
receive no requests or credentials, and synthetic generator executables. They
cover oversized inputs, special-file refusal, nonzero/empty/timeout generation,
generators whose descendants hold the output pipes, cancellation, and private
output. Generation and native helpers run in their own sessions and their whole
process group is killed before the leader is reaped. They do not read a personal archive, call a model, or edit an app.

## User-invoked preview launcher

`preview.py` captures the focused field and prepares a private request **before**
creating its Tk window. Run it through a non-activating shortcut launcher:

```sh
python3 /absolute/path/to/scripts/contextual-draft/preview.py
```

The window shows the request/source evidence, an editable instruction and model
field, and a Generate button. No model is called until Generate is clicked with
an explicit model. That click discloses the prepared request to that model.
Generation runs off the UI thread. Close / Cancel terminates the launcher's local
generation process group; it cannot recall input already disclosed to a provider.
There is no insertion, copy button, clipboard mutation, or send action.

Requires Python with tkinter support and POSIX process groups. `--model` prefills
the model without invoking it; `--context`, `--query`, and `--limit` control the
prepared context. `--target` accepts a fixture for tests; `--prepare-only` saves
without opening a window. Requests/drafts are retained in private temporary
session directories printed by the launcher; choose their retention yourself.
No global shortcut is installed automatically.

Live Notes validation: empty editor, exact title/body including trailing newlines,
and a four-character selection at UTF-16 offset 12 passed read-only capture.
This does not establish other apps, secure-field acceptance, or insertion safety.

## Direct draft action (experimental macOS)

`direct.py` is the primary context-first action: invoke at a cursor with no text
selection, generate using `openai-subscription/gpt-6.1-sol` by default, then insert
through the retained native AX element only if focus, field contents and selection
are unchanged. Invoking this action authorizes model disclosure and insertion.
It never sends, activates a window, replaces the clipboard, or simulates keys.
It refuses selected-text rewriting; that is lower priority than contextual drafting.
Unsupported selected-text insertion fails with no keyboard fallback. A local lock
prevents overlapping direct drafts. Private request/draft files remain available
when insertion fails. Actual insertion compatibility still needs per-app validation.

Example shortcut bindings (install deliberately in your own skhd configuration):

```text
cmd + alt - g : /absolute/path/to/launcher direct
cmd + alt + shift - g : /absolute/path/to/launcher preview
```

The launcher should set PATH explicitly and execute direct.py or preview.py without
opening a terminal. Preview defaults to the same model and context-first task, but
retains the instruction field for debugging and does not insert. Current retrieval
is bounded Goldfish working memory plus explicitly configured context files, not
an automatic complete personal-context search. No claim of Goldfish drafting parity.

### Draft relevance and progress

Direct mode displays a best-effort non-activating, click-through native spinner
near the captured field while preparing/generating/inserting. Its stdin lifetime
is tied to the launcher, with a timeout fail-safe. It does not activate the app or
move the text cursor. Placement is field-level, not a caret-tracking animation.

Default retrieval now samples the recent snapshot timeline, with related FTS
hits for project/issue identifiers in the window title. Long snapshots preserve
both the beginning and recent end, explicitly marking omitted middle content.
This is still a lexical baseline, not Goldfish's semantic retrieval or full agent
memory. Model instructions prohibit writing assistant-style clarification questions
into a recipient field; insufficient evidence should abstain. Draft quality and
privacy still need realistic user testing. No quality parity is claimed.

### Diagnostics and recovery

Direct sessions persist under `${XDG_STATE_HOME:-~/.local/state}/contextual-draft/runs/`.
The launcher prints the exact private session path before capture. Each directory
is mode 0700 and files are mode 0600. `events.jsonl` records stage timestamps,
model/exit status, source IDs, request/output byte counts and elapsed generation
time, without message bodies. `native-stderr.txt` preserves capture/insertion
errors. Provider stderr, when present, is saved privately beside the draft as one
`draft.txt.provider-stderr-<ns>.txt` file per attempt, so a retry never collides
with earlier diagnostics; it may contain sensitive request data and must not be
copied into issues or notifications.
`request.json` and `draft.txt` permit inspection/recovery without another model call.
No automatic retry or retention deletion occurs. These files are private evidence,
not a safe-to-publish log bundle. Do not recursively dump sessions into transcripts.

Producer, direct insertion and preview share a 16,000-byte draft limit. Generated
whitespace is preserved; only the framing newline added by the gptme plain-text
CLI is removed. Whitespace-only responses are checked separately for abstention.
Preview workers are non-daemon so closing during process creation cannot abandon
a model process before its cancellation check. Notifications use terminal-notifier
when available; its absence does not replace the original error with a traceback.

### External personal context

Use explicit `--context` files for approved writing preferences or selected factual
material. A short distilled style profile is preferable to loading a raw personal
conversation corpus. Agent journals/tasks can provide facts unavailable in desktop
history, but their timestamps and statuses need checking; an old task is not current
truth. A future retrieval adapter should reuse existing agent memory recall rather
than create another corpus/index. This prototype does not automatically search a
private agent repository or disclose its contents simply because it exists.
