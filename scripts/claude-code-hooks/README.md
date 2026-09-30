# Claude Code Hooks for gptme Agent Workspaces

Claude Code hook scripts that bring gptme's lesson injection system into
Claude Code sessions.

## `match-lessons.py` — Keyword-based Lesson Injection

Replicates gptme's keyword-based lesson matching for Claude Code sessions.
When you open a CC session in a gptme agent workspace, this hook automatically
injects relevant lessons as `additionalContext` based on:

- **UserPromptSubmit**: matches the user's prompt text (fires once at session start)
- **PreToolUse**: matches tool inputs (file paths, shell commands, search patterns)
  and recent transcript output — so lessons fire on *what just happened*

### Features

- Keyword and wildcard matching (same logic as gptme's `LessonMatcher`)
- Per-session deduplication (each lesson injected at most once per session)
- Thompson sampling re-ranking (lessons that help more get higher priority)
- Co-occurrence prediction (predicts lessons likely to be needed based on
  what's already fired — requires a pre-built prediction model)
- PreToolUse cooldown throttle (15s) to avoid excessive context injection
- Self-referential match prevention (strips `<system-reminder>` blocks)

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
            "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/match-lessons.py",
            "timeout": 10
          }]
        }],
        "PreToolUse": [{
          "matcher": "Read|Bash|Grep|WebFetch|WebSearch",
          "hooks": [{
            "type": "command",
            "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/match-lessons.py",
            "timeout": 10
          }]
        }]
      }
    }
    ```

3. Ensure your workspace has a `gptme.toml` with `[lessons] dirs` configured.
   The hook reads lesson directories from there (same as gptme's config).

### Workspace Auto-Discovery

The hook walks up from its own location to find the workspace root (the directory
containing `gptme.toml`). All state directories are derived from the workspace root:

| Path | Purpose |
|------|---------|
| `workspace/state/lesson-thompson/` | Thompson sampling bandit state |
| `workspace/state/lesson-predictions/` | Co-occurrence prediction model |
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
| `MAX_PREDICTED_LESSONS` | 2 | Max co-occurrence-predicted lessons per event |
| `TS_WEIGHT` | 1.0 | Thompson sampling re-ranking weight |

#### Skill-injection A/B dial

The hook includes an opt-in, session-randomized experiment for reducing
advisory skill context. Unset is the rollback path and preserves the historical
behavior exactly:

```sh
# 50/50 deterministic assignment by Claude Code session ID
export LESSON_INJECTION_DIAL_EXPERIMENT=random

# Reproduction/debugging overrides
export LESSON_INJECTION_DIAL_EXPERIMENT=control
export LESSON_INJECTION_DIAL_EXPERIMENT=treatment
```

The control arm uses the existing limits and BM25 gate. The treatment arm caps
advisory skills at 2 for `UserPromptSubmit` and 1 for `PreToolUse`, including
co-occurrence predictions, and raises the skill-only BM25 z-score ceiling from
4.0 to 5.0. Ordinary lessons and policy-manifest `exempt` entries bypass both
treatment changes, so hard guardrails are not experiment subjects. The manifest
only indexes `lessons/` paths, so a skill stored as `skills/<name>/SKILL.md`
cannot currently be listed as `exempt`; a skill that must never be capped should
be expressed as a regular `lessons/` entry.

Each enabled session appends one record to
`state/lesson-injection-dial/YYYY-MM-DD.jsonl` (override the directory with
`LESSON_INJECTION_DIAL_LOG_DIR`). The ledger is keyed by `session_id` for joins
against transcript context spend and category-controlled outcome/LOO data:

```json
{
  "schema_version": 1,
  "experiment": "skill-injection-dial-v1",
  "session_id": "...",
  "harness": "claude-code",
  "enabled": true,
  "arm": "control",
  "assignment": "random",
  "prompt_skill_cap": null,
  "pretool_skill_cap": null,
  "skill_bm25_min_z": null,
  "ts": 1790760000.0
}
```

`assignment` is `random` for production assignment and `forced` for explicit
arm overrides. Invalid values and a missing session ID fail closed to disabled
control behavior and are not written to the experiment ledger.

### Requirements

- Python 3.11+ (uses `tomllib` from stdlib; or `tomli` as fallback for 3.10)
- Optional: `pyyaml` for YAML frontmatter parsing (falls back to regex)
- No other dependencies — runs standalone in any Python environment

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
