---
match:
  keywords:
    - "delete trajectory"
    - "delete session data"
    - "session data retention"
    - "trajectory files"
    - "session records"
    - "cleanupPeriodDays"
    - "log cleanup"
    - "retention policy"
  session_categories: [infrastructure, cleanup]
target_grade: harm
status: active
---

# Trajectory Persistence

## Rule
Never delete trajectory files, session records, session data directories, or any data that captures what happened in agent sessions. These are small, cheap to store, and invaluable for learning, debugging, and trajectory analysis.

## Context
Agent sessions generate trajectory files (.jsonl), session data dirs (tool-results/hook outputs), and session records. These are the primary evidence of what an agent did, how it reasoned, and what tools it used. They enable:
- **Post-hoc analysis**: Understanding why a session produced certain outcomes
- **Learning**: Identifying patterns in agent behavior across thousands of sessions
- **Debugging**: Reproducing issues from specific sessions
- **Trajectory research**: Training, evaluation, and behavioral analysis

## Detection
Observable signals that this rule applies:
- Writing or reviewing a cleanup/maintenance script
- Seeing trajectory files, `.jsonl` files, or session dirs in a deletion path
- Adding retention policies to any data store
- A model suggesting "clean up old session data" or similar

## Pattern

### What to preserve (indefinitely)
- `~/.claude/projects/*/*.jsonl` — Claude Code trajectory files
- `~/.claude/projects/*/UUID/` — session data directories (tool-results, hook outputs)
- Session record indexes (e.g. `state/sessions/session-records.jsonl`)
- gptme session logs (`~/.local/share/gptme/logs/`) — conversation histories

### What's safe to clean up (ephemeral operational state)
- `/tmp/claude-session-*.jsonl` — temporary session files
- `/tmp/claude-lesson-match/*.json` — per-session lesson match state
- Gate check state files (activity-gate, triage cooldown)
- Large stdout logs (compress/truncate, but don't delete entirely)

### Defense-in-depth: hardlink backup
```bash
# Hardlink trajectory files (zero extra disk while originals exist)
cp -al ~/.claude/projects/*/*.jsonl ~/data/trajectories/claude-code/

# Hardlink session data dirs (tool-results, hook outputs)
for d in ~/.claude/projects/*/[0-9a-f][0-9a-f][0-9a-f]*; do
    [ -d "$d" ] && cp -al "$d" ~/data/trajectories/claude-code/
done
```
If originals are ever deleted (by a bug, model suggestion, or future refactor), the hardlinked backups survive as the sole copy. Run this periodically (e.g. in your cleanup/maintenance timer).

### Claude Code settings.json
Claude Code's default `cleanupPeriodDays` is low (days/weeks). Set it high to prevent the harness itself from deleting trajectories:
```json
{
  "cleanupPeriodDays": 1000000
}
```
This is set in `~/.claude/settings.json` (global) or `.claude/settings.json` (project). Even with this set, external scripts can still delete the files — which is why hardlink backups and this lesson exist.

### Before writing ANY deletion logic
1. Check if the user has configured retention for this data (e.g., `cleanupPeriodDays` in `~/.claude/settings.json`)
2. Ask: "Would the user want this data back someday?" If yes, don't delete it.
3. Ask: "Is this data cheap to store relative to its value?" Session data almost always is.
4. Never write scripts that contradict user-configured safety settings.
5. If you must manage growth, compress — don't delete.

## Anti-Patterns

**Bypassing user safety settings**:
A cleanup script that deletes `.jsonl` files from `~/.claude/projects/` contradicts `cleanupPeriodDays: 1000000` in `~/.claude/settings.json`. The user explicitly said "never delete" and the script overrode that decision.

**"Cleanup" as default behavior**:
Models may suggest deleting old files as routine maintenance. For large logs, this can be appropriate. For trajectory data, it destroys irreplaceable history. The default should be preservation, not cleanup.

**Assuming data is expendable because it's old**:
A 6-month-old trajectory is just as valuable for analysis as yesterday's. Age is not a signal for deletion of session data.

## Origin
April 2026: A cleanup script deleted 3,011 trajectory files over 5 days despite the user having set `cleanupPeriodDays: 1000000`. No backup was available to recover them. The script bypassed an explicit user safety setting — a trust violation that prompted hardlink backups, health checks, and this lesson.

### Defense layers (in order of preference)
1. **Don't delete** — preserve by default, only clean ephemeral state
2. **`cleanupPeriodDays: 1000000`** — prevent the harness itself from deleting
3. **Hardlink backups** — `cp -al` to a separate path, survives if originals are deleted
4. **System-level backups** — VM snapshots, Proxmox Backup Server, etc. Last resort — effortful to restore from and may not cover the right time window

All four layers failed in the April 2026 incident: the script deleted despite layer 1, bypassed layer 2, layer 3 didn't exist yet, and layer 4 predated the data. Defense-in-depth means having all layers.

Principle: **Disk is cheap; lost history is irreplaceable.**

## Outcome
Following this rule prevents:
- **Irreversible data loss**: Trajectory files cannot be reconstructed once deleted
- **Lost learning signal**: Session records are the primary input for LOO analysis and bandit feedback
- **Broken debugging**: Post-hoc analysis becomes impossible without the original trace
- **Trust violations**: User safety settings (`cleanupPeriodDays: 1000000`) must never be overridden by scripts

## Related
- [Pre-Mortem for Risky Actions](../autonomous/pre-mortem-for-risky-actions.md) — run before any deletion logic
- [Autonomous Operation Safety](../autonomous/autonomous-operation-safety.md) — boundaries for autonomous actions
