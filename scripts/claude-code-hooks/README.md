# Claude Code Hooks for gptme Agent Workspaces

Claude Code hook scripts that bring gptme's lesson injection system into
Claude Code sessions.

## `match-lessons.py` — Keyword-based Lesson Injection

Brings gptme's lesson injection system into Claude Code sessions. Keyword
matching and BM25 scoring are delegated to `gptme_rag.lesson_matcher`; this
script is the Claude Code harness wrapper: workspace discovery, Thompson
sampling, holdout/dropout, session dedup, and the
`additionalContext` envelope.

When you open a CC session in a gptme agent workspace, this hook automatically
injects relevant lessons as `additionalContext` based on:

- **UserPromptSubmit**: matches the user's prompt text (fires once at session start)
- **PreToolUse**: matches tool inputs (file paths, shell commands, search patterns)
  and recent transcript output — so lessons fire on *what just happened*

### Features

- Keyword and wildcard matching via `gptme_rag.lesson_matcher`
- BM25 semantic scoring (same library used by gptme's native lesson matcher)
- `session_categories` gating (restrict a lesson to specific session types)
- Per-session deduplication (each lesson injected at most once per session)
- Thompson sampling re-ranking (lessons that help more get higher priority)
- PreToolUse cooldown throttle (15s) to avoid excessive context injection
- Self-referential match prevention (strips `<system-reminder>` blocks)
- Fixed-prompt window: prompt-time matching sees only lines not already seen in
  the last 20 prompt events (per-run ids normalized), so a launcher's boilerplate
  cannot pin the same documents into every run — see
  [Launcher adoption](#launcher-adoption)
- Byte budgets: at most 8,000 bytes of lesson text per event and 24,000 per
  session by default; see [Byte budget](#byte-budget)

### Installation

1. Copy (or symlink) the hook into your agent workspace:

    ```sh
    # Option A: copy
    cp scripts/claude-code-hooks/match-lessons.py /path/to/workspace/.claude/hooks/

    # Option B: symlink (picks up updates automatically)
    mkdir -p /path/to/workspace/.claude/hooks
    ln -s $(pwd)/scripts/claude-code-hooks/match-lessons.py \
          /path/to/workspace/.claude/hooks/match-lessons.py
    ```

2. Register the hook in your workspace Claude Code settings
   (`/path/to/workspace/.claude/settings.json`):

    ```json
    {
      "hooks": {
        "UserPromptSubmit": [{
          "hooks": [{
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR/.venv/bin/python3\" \"$CLAUDE_PROJECT_DIR/.claude/hooks/match-lessons.py\"",
            "timeout": 10
          }]
        }],
        "PreToolUse": [{
          "matcher": "Read|Bash|Grep|WebFetch|WebSearch",
          "hooks": [{
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR/.venv/bin/python3\" \"$CLAUDE_PROJECT_DIR/.claude/hooks/match-lessons.py\"",
            "timeout": 10
          }]
        }]
      }
    }
    ```

3. Ensure your workspace has a `gptme.toml` with `[lessons] dirs` configured.
   The hook reads lesson directories from there (same as gptme's config).

4. Install `gptme-rag` in that same workspace venv, not only in a global tool
   environment. For example, from the workspace root:

    ```sh
    uv venv .venv
    uv pip install --python .venv/bin/python3 ./gptme-contrib/packages/gptme-rag
    .venv/bin/python3 .claude/hooks/match-lessons.py --self-check
    ```

   The self-check needs no stdin and exits non-zero if the matcher cannot be
   imported or none of the configured lesson directories exists. Run it after
   submodule/dependency updates or from a daily workspace health check. It checks
   installation health, not whether a particular prompt will match a lesson.

   Normal hook calls remain fail-open. An import failure prints one warning to
   stderr per invocation and marks the final receipt with
   `reason: dependency_missing`, rather than masquerading as `no_match`.
   Independent keyword-memory matching still runs: the receipt measures zero
   bytes when nothing is injected, or the actual emitted bytes for memory hits.

### Workspace Auto-Discovery

The hook walks up from its own location to find the workspace root (the directory
containing `gptme.toml`). All state directories are derived from the workspace root:

| Path | Purpose |
|------|---------|
| `workspace/state/lesson-thompson/` | Thompson sampling bandit state |
| `workspace/state/lesson-trajectories/` | Trajectory logs for analysis |

These are created automatically on first use. If your workspace has no TS state yet,
lessons are ranked by keyword matches alone (neutral prior = 0.5).

### Configuration

The hook reads lesson directories from `gptme.toml`:

```toml
[lessons]
dirs = [
    "lessons",
    "gptme-contrib/lessons",
]
```

Tunable constants (edit at the top of the script):

| Constant | Default | Description |
|----------|---------|-------------|
| `PRETOOL_COOLDOWN_SECONDS` | 15 | Minimum seconds between PreToolUse matches |
| `MAX_PROMPT_LESSONS` | 5 | Max lessons injected per UserPromptSubmit |
| `MAX_PRETOOL_LESSONS` | 3 | Max lessons injected per PreToolUse |
| `TS_WEIGHT` | 1.0 | Thompson sampling re-ranking weight |

Environment overrides:

| Variable | Default | Description |
|----------|---------|-------------|
| `MATCH_LESSONS_PROMPT_REPEAT_WINDOW` | `20` | Prompt events before a seen prompt line is matched again; `0` disables the fixed-prompt window |
| `MATCH_LESSONS_HOOK_BUDGET` | `8.0` | Internal wall-clock budget in seconds before degrading to empty output |
| `MATCH_LESSONS_EVENT_BYTES` | `8000` | Max bytes of injected lesson text per hook event; `0` disables |
| `MATCH_LESSONS_SESSION_BYTES` | `24000` | Max bytes of injected lesson text per session; `0` disables |

### Byte budget

The count caps above bound how many documents are injected, not how large they
are. The byte budgets bound the UTF-8 size of the emitted `additionalContext`
(heading included):

- Lessons are kept whole, first-fit in the hook's priority order. A lesson that
  does not fit is skipped for this event and a later, smaller one may still fit.
  A lesson is never truncated.
- A single lesson larger than the per-event budget (or the remaining session
  budget) is dropped. If nothing fits, the event injects nothing and its receipt
  has `reason: "byte_budget"`.
- Skipped lessons are **not** marked injected, so a later event can still inject
  them while session budget remains.
- The session total is kept in the per-session state file (`budget_bytes`) and
  updated under a per-session file lock, so parallel tool-call hooks of one
  session cannot both spend the same remainder.
- Receipts (`cc-session-<id>-lessons-receipts.jsonl`) of events that ran the
  budget carry `budget_dropped` (lessons skipped for size) and `session_bytes`
  (session total after this event).
- Fail-open: if budget accounting raises, the event injects untrimmed and its
  receipt carries `budget_error: true`.

Set both variables to `0` to restore the previous, unbounded behaviour.

### Launcher adoption

**No launcher change is required.** Autonomous launchers (Bob's `autonomous-run.sh`,
`gptme`, Codex, Grok) send a fixed boilerplate prompt on every run, and every run
is a new session, so per-session dedup can never see the repeat. The hook keeps a
rolling window of prompt *lines* per workspace in `/tmp/claude-lesson-match/`
instead. Real launcher prompts are never byte-identical (session label, UUIDs,
journal basename, deadline time, a few varying bullets), so each non-blank line
is normalized before hashing: UUIDs, `HH:MM[:SS]` times, digit-bearing segments of
`-`/`_`/`.`/`/`-joined identifiers (`bob-autonomous-claude-code-4107`,
`autonomous-session-883d.md`) and standalone mixed hex (`883d`, `2f8387ce`)
become placeholders. Standalone plain numbers are kept, so "fix issue 123" and
"fix issue 456" stay distinct. Each line hash includes the resolved project
`cwd`, `GPTME_CURRENT_REPO`, session category, harness, and parsed
`HOLDOUT_LESSONS`, so different matching scopes (including experiment holdout
arms) do not suppress each other:

- a prompt whose every line was seen within the window skips prompt-time
  matching and receipts `{outcome: empty, bytes: 0,
  reason: fixed_prompt_repeat}`;
- a prompt with some seen lines is matched on its novel lines only, so
  event-specific text (a bound work item, a changed bullet) still drives
  matching instead of being drowned by boilerplate under the prompt-time cap;
- `UserPromptSubmit` receipts carry `prompt_lines` / `prompt_novel_lines`
  (distinct normalized lines) so the effect is measurable per session;
- `PreToolUse` matching and task-text matching are untouched — the tool channel is
  where lessons relevant to the work actually come from;
- a seen line does **not** refresh its stored position, so boilerplate is
  matched again after `window` events instead of being blocked forever;
- `MATCH_LESSONS_PROMPT_REPEAT_WINDOW=0` restores the previous behaviour.

Stable `flock` sidecars serialize matching/emission only for the same scoped
prompt. Distinct prompts and matching scopes can run concurrently; a short
workspace-wide lock protects state reads and commits, never matching or selector
execution. Each commit merges fresh state so parallel completions retain each
other's hashes and event counts. Two *different* prompts that share lines and
overlap may both treat the shared lines as novel (an extra injection, never a
lost one). The window counts successfully completed prompt
events and is committed only after processing and output complete normally
(including a successful empty decision). A timeout, exception or killed process
does not mark the prompt processed; the next call can retry. Waiting for a lock
shares the hook's existing time budget. Once matching
has completed, the internal alarm is disarmed before writing the final response;
post-output session/window bookkeeping cannot append a timeout response to it.
The post-output workspace lock reacquire is bounded (`LOCK_NB` with a 0.5s
retry): if another process still holds it after that, the commit is skipped
rather than hanging after the budget is disarmed. Extra injection on the next
identical prompt is the fail-open path.
Expired scoped-prompt lock sidecars are unlinked after a successful commit when
they are not currently held, so inode use tracks the live window rather than
every historical prompt. After acquiring a scoped lock, an opener checks that
its device/inode still matches the pathname and retries if pruning removed or
replaced it. Waiting on an expired inode therefore cannot bypass serialization.
Session dedup is saved only after successful emission. Authorized selector
sessions retain the user query even on a suppressed prompt, so subsequent tool
selection still has the user's intent. State I/O errors fail open, leaving normal
matching available rather than suppressing it. On platforms without `fcntl`
(including native Windows), the repeat window is disabled rather than disabling
the hook; ordinary matching still runs.

The window is per workspace, so several agents sharing `/tmp` do not suppress each
other. Interactive sessions are affected too: a line already sent within the
window (the identical prompt twice in a row, or the same error pasted again) is
not matched again until it ages out (a deliberate trade for the measured
saturation — 5 distinct documents, 3 in 14-15 of 20 runs, ~29 KB per run; see
[gptme/gptme-contrib#2170](https://github.com/gptme/gptme-contrib/issues/2170)).
If interactive repeats feel suppressed, lower the window rather than disabling the
mechanism: `MATCH_LESSONS_PROMPT_REPEAT_WINDOW=2` injects at most once every second
prompt event (`1` is equivalent to disabled).

### Requirements

- Python 3.11+ (uses `tomllib` from stdlib; or `tomli` as fallback for 3.10)
- `gptme-rag` (from `packages/gptme-rag/` in this repo, or `pip install gptme-rag`) — provides `gptme_rag.lesson_matcher`
- Optional: `pyyaml` for YAML frontmatter parsing (falls back to regex)

### Testing

Pipe mock hook input directly to the script:

```sh
# UserPromptSubmit test
echo '{"hook_event_name":"UserPromptSubmit","session_id":"test","prompt":"git merge conflict"}' \
  | python3 match-lessons.py | python3 -m json.tool

# PreToolUse test
echo '{"hook_event_name":"PreToolUse","session_id":"test","tool_name":"Bash","tool_input":{"command":"git status"}}' \
  | python3 match-lessons.py | python3 -m json.tool
```

## See Also

- [`gptme-lessons-extras`](../../packages/gptme-lessons-extras/) — lesson validation,
  effectiveness analysis, and generation utilities
- [gptme docs: Lessons](https://gptme.org/docs/lessons.html) — lesson system overview
- [gptme-agent-template](https://github.com/gptme/gptme-agent-template) — workspace
  template that uses this hook
