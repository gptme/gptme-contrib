# Trajectory preservation

Harness-agnostic defense-in-depth for agent trajectories — the full record of
what a session did and reasoned. They are the substrate for self-improvement,
debugging and behavioural analysis. They are small and cheap to keep, and their
loss is irreversible.

## The incident this exists to prevent

On **2026-04-09** a cleanup script deleted **3,011 Claude Code trajectories** —
everything before that date — despite the harness's own retention being set to
never-delete. The script bypassed that setting by removing files externally, and
no system-level backup covered the window. A hardlink backup on a separate path
would have made it recoverable.

There is a second, subtler failure mode: a backup path **hardcoded to one
harness**. If you back up `~/.claude/projects/...` but not
`~/.local/share/gptme/logs/`, then the day you fail over to the gptme harness you
are running on the one lane nobody preserves. A hardcoded per-harness path is a
latent incident — it does not lose data today, it loses whichever harness you
adopt next. So here, **harness coverage is data, not code**: one table that both
the backup and the health check walk.

## Files

| File | Role |
|---|---|
| `trajectory-sources.sh` | The harness table + unit-counting helpers. Single source of truth for "what must never be lost". Sourced by the other two. |
| `trajectory-backup.sh` | Hardlink backup (`rsync --link-dest`) of every harness to `TRAJECTORY_BACKUP_ROOT`. Zero extra disk; survives external deletion of the live copy. |
| `trajectory-health-check.sh` | Per-harness alarms: deletion, missing/short backup, and stalled-writing on the *active* harness. Exit 1 on any warning. |

## Defense layers (all of them, not one)

1. **Don't delete** — preserve by default; only clean ephemeral state.
2. **Harness retention** — e.g. Claude Code's `cleanupPeriodDays: 1000000`.
3. **Hardlink backup** — `trajectory-backup.sh`. Protects against *unlink* of the
   live copy. Does **not** protect against loss of the filesystem (hardlinks
   cannot cross filesystems, so the backup is same-disk by design).
4. **Off-host backup** — the only thing that survives disk loss (PBS, rsync to
   another machine). Layer 3 makes deletion recoverable; only layer 4 makes a
   dead disk recoverable. All four failed in the April incident — defense-in-depth
   means keeping all of them live.

## Usage

```bash
# Back up every harness (run on a timer, e.g. daily)
scripts/trajectory-preservation/trajectory-backup.sh
scripts/trajectory-preservation/trajectory-backup.sh --dry-run   # preview

# Verify preservation is healthy (run on a timer; exit 1 = something to fix)
scripts/trajectory-preservation/trajectory-health-check.sh
```

## Adding a harness

Append one line to `TRAJECTORY_SOURCES` in `trajectory-sources.sh`, or set
`TRAJECTORY_EXTRA_SOURCES` in the environment (newline-separated, same format).
Both the backup and the health check pick it up. Format:

```
name|source dir|destination dir|unit spec
```

`unit spec` is `<kind>:<glob>` where kind is `file` (files directly in source,
e.g. Claude Code `.jsonl`), `dir` (directories directly in source, e.g. gptme
conversation dirs), or `rfile` (files anywhere under source, e.g. the Codex date
tree). Listing a harness whose directory does not exist yet is deliberate: it is
covered from its first session rather than discovered missing later.

Claude Code project dirs are auto-discovered from `~/.claude/projects/*`, so new
workspaces are protected without editing the table.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `TRAJECTORY_BACKUP_ROOT` | `~/data/trajectories` | Where hardlink backups go |
| `TRAJECTORY_EXTRA_SOURCES` | — | Extra `name|src|dest|spec` lines, newline-separated |
| `WORKSPACE` | script's dir | Used to locate the primary Claude Code project dir |
| `SESSION_RECORDS` | `$WORKSPACE/state/sessions/session-records.jsonl` | Optional gptme-sessions store to report on |
| `ACTIVE_WINDOW_DAYS` | `30` | A harness is "active" if within this of the newest trajectory anywhere |
| `STALE_DAYS` | `3` | Active harness with no trajectory this recent = stalled |
| `BACKUP_GRACE_HOURS` | `24` | Units younger than this are not yet expected in the backup |
