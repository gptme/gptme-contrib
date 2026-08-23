---
name: end
description: "Wrap up the session safely: gate on uncommitted/unpushed work and missing journal, refuse to end if anything is unsaved, write the closing summary, then exit the harness when the session was light (stay alive for review when it was substantial). Invoke as /end (Claude Code, Codex) or /skill:end (gptme)."
when_to_use: "Use when the user says /end, 'wrap up', 'end the session', 'we're done', or when an autonomous session has finished its work and is about to stop. Do not use mid-task as a way to bail on unfinished work — the check will refuse."
argument-hint: "[--exit | --stay | --dry-run]"
license: MIT
compatibility: "Claude Code (/end), Codex (/end via ~/.codex/skills), gptme (/skill:end, gptme#pending). Linux /proc required for harness detection; stdlib-only Python 3.10+."
metadata:
  author: bob
  version: "1.0.0"
  tags: [session, wrap-up, lifecycle, cross-runtime]
keywords:
  - "/end"
  - "end the session"
  - "wrap up the session"
  - "we're done for now"
  - "close out this session"
---

# /end — wrap up, or refuse to

The skill is a **gate, then a closeout, then (maybe) an exit**. It exists so
"I'm done" is a checked claim, not a feeling. Three rules:

1. **Refuse to end with unsaved work.** Uncommitted files you touched, unpushed
   commits, dirty worktrees, or a missing journal entry → you are *not* done.
2. **Closeout is short and stands alone.** The last message must make sense to
   someone who reads only it.
3. **Exit only when it's cheap to come back.** Light session → exit the harness.
   Substantial session → stay alive so the human can review before the context
   is gone. (`--exit` / `--stay` override.)

## Arguments

`$ARGUMENTS` — optional flags:

| Flag | Effect |
|---|---|
| *(none)* | check → closeout → exit iff `CLEAN_LIGHT` |
| `--exit` | check → closeout → exit even if `CLEAN_SUBSTANTIAL` |
| `--stay` | check → closeout → never exit |
| `--dry-run` | run the check and report; change nothing, exit nothing |

## Step 1 — Run the gate (always)

```bash
# SKILL_DIR: $CLAUDE_SKILL_DIR in Claude Code; otherwise the directory of this
# SKILL.md (e.g. gptme-contrib/skills/end or ~/.codex/skills/end).
python3 "$SKILL_DIR/scripts/end-check.py"
```

It reports, per check, `✅ ok` / `ℹ️ info` / `⚠️ warn` / `❌ block`, and ends with
one of:

| Verdict | Meaning | What you do |
|---|---|---|
| `BLOCKED` | unsaved work attributed to this session | **Refuse.** Go to Step 2. |
| `CLEAN_LIGHT` | ≤2 commits, ≤10 files, no PRs/worktrees pending | Closeout → exit (Step 3+4) |
| `CLEAN_SUBSTANTIAL` | more than that, or open PRs / warnings | Closeout → hand-off → stay alive |

"This session" = files/commits newer than the harness process start (from
`/proc`), or commits carrying this session's id. Other sessions' dirt in a
shared worktree is listed as `info`, not a blocker. Pass `--since 2h` if the
process-start heuristic is wrong (e.g. a resumed session).

**Shared worktree with parallel sessions** (Bob's brain repo): siblings share
your time window, so declare your footprint — the repo paths you touched plus
the absolute paths of any linked worktrees you created. Only dirt/commits under
those paths and only declared worktrees can block; the rest is info:

```bash
python3 "$SKILL_DIR/scripts/end-check.py" \
  --paths journal/2026-08-22/my-session.md scripts/foo.py /tmp/worktrees/my-feature
```

A parallel session's fresh worktree can still show as a blocker; if
`git -C <wt> log -1` proves it isn't yours, say so in the closeout and treat
it as info — don't touch it.

Also do the **judgment checks** the script can't:

- Did the user ask something you never answered? → not done.
- Did you promise "I'll …" anywhere in the last few messages? → do it or retract it.
- Is a subagent or background job still running that produces a deliverable? → wait or kill it explicitly.
- Is a task/claim still marked active for this session? → release it (Bob: `uv run coordination work-list --claimed`, then `work-complete`; `gptodo edit <id> --set state …`).

## Step 2 — If BLOCKED: say so, then fix

Open with the refusal, verbatim shape:

> **Not ending — N blocker(s):** `<summary list from the check>`

Then fix what is fixable without the human:

```bash
git commit <explicit paths> -m "..."          # brain repo: git-safe-commit --scope-only <paths> -m "..."
git push                                      # brain repo: git-safe-push-master
# journal entry for the work (Bob: journal/YYYY-MM-DD/<session-file>.md, append-only)
```

Re-run the check. If a blocker needs the human (a decision, a credential, a
conflict you shouldn't resolve alone), **stay alive and ask** — one question,
with the default you'd pick. Never "wrap up anyway".

## Step 3 — Closeout message (≤ 12 lines)

```
## Session closeout
**Shipped**: <commits/PRs with links — or "nothing shipped">
**Verified**: <how you know it works — tests run, symptom re-checked — or "not verified: …">
**Not done / deferred**: <explicitly, so nobody assumes it happened>
**Next**: <the one action that unblocks continuation>
**Closing**: exiting now | staying alive for review (say `/end --exit` to close)
```

Bob-specific: the closeout goes into the journal file too if it isn't there
already (append-only), and the journal commit must be pushed before Step 4 —
re-run the check once more after writing it.

## Step 4 — Exit decision

| Verdict | Default | `--exit` | `--stay` |
|---|---|---|---|
| `CLEAN_LIGHT` | exit | exit | stay |
| `CLEAN_SUBSTANTIAL` | stay | exit | stay |
| `BLOCKED` | stay (refused) | stay (refused) | stay |

To exit, the closeout message must already be sent; then, as the final tool call:

```bash
python3 "$SKILL_DIR/scripts/end-exit.py"        # SIGTERM to the harness after 3s
python3 "$SKILL_DIR/scripts/end-exit.py" --dry-run   # show the target first if unsure
```

It finds the interactive harness process (Claude Code via `CLAUDE_PID`; gptme
and Codex via the `/proc` parent chain) and schedules the signal from a
detached helper so your tool call returns first. Non-interactive runs
(`claude -p`, `gptme -n`, `codex exec`) are left alone — they end with the
turn. gptme caveat: SIGTERM skips `SESSION_END` hooks; when a human is present
prefer telling them to type `/exit`.

## Rationalizations this skill exists to block

| Thought | Reality |
|---|---|
| "It's just a journal file, I'll commit it next session." | Next session is a different process with no memory of this one. Commit now. |
| "The other session's changes are in the way, I'll skip the push." | Shared worktree: use `git-safe-push-master`; your commits are still yours to land. |
| "CI is still running, I'll call it shipped." | Say "not verified: CI pending" in the closeout. Unverified ≠ done. |
| "The session was big, so exiting saves the human time." | The opposite: big sessions are exactly the ones a human wants to inspect while the context still exists. |
| "Exit is just `kill`, no big deal." | It's irreversible for this context. The closeout must already be on screen and in the journal. |

## Per-runtime install

Run once per machine (idempotent):

```bash
bash <this-dir>/install.sh          # symlinks into ~/.claude/skills/end and ~/.codex/skills/end
bash <this-dir>/install.sh --project   # also .claude/skills/end in the current repo
```

- **Claude Code**: `~/.claude/skills/end` (or project `.claude/skills/end`) → `/end`.
- **Codex**: `~/.codex/skills/end` → skills are slash-command packages; `/end` or `$end`.
- **gptme**: skills in configured lesson dirs are already indexed; `/skill:end` lands with gptme PR "invoke skills as slash commands". Until then, say "wrap up the session" — the `when_to_use` matcher loads this skill.

## Related

- `skills/autonomous-session-workflow` (Bob) — Phase 5 "Persist" is what this gate enforces.
- `lessons/workflow/merged-is-not-live` (Bob) — why the closeout has a **Verified** line.
- `scripts/forward-drive-probe.py` (Bob) — the harness-side sibling: the same invariants checked on every *completion event* fleet-wide, dispatching a full session when a gap is found. Design: `knowledge/technical-designs/forward-drive-completion-reactor.md`.
- `scripts/closed-loop-check.py` (Bob) — post-hoc session-altitude check; the report-only ancestor of `end-check.py`.
