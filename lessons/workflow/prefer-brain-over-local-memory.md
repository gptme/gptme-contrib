---
match:
  keywords:
    - memory
    - remember
    - save
    - persist
    - knowledge
    - brain
    - MEMORY.md
    - auto-memory
---

# Prefer Brain Repo Over Local Machine Memory

## Rule
When an agent has a brain repo (git workspace), persist knowledge there — not in runtime-local memory systems like Claude Code's `~/.claude/projects/*/memory/`.

## Context
Claude Code has a local auto-memory system (`MEMORY.md` + memory files) that persists across conversations on the same machine+project. Agents like Alice and Bob have their own git-based brain repos with `knowledge/`, `journal/`, and task systems. The local memory is strictly inferior — it's not versioned, not portable across machines, and invisible to other runtimes.

## Detection
Observable signals you're about to make this mistake:
- Writing to `~/.claude/projects/*/memory/` when the project IS an agent's brain repo
- Saving knowledge about data sources, architecture, or findings to local memory instead of `knowledge/`
- Information that other runtimes (gptme, other machines) would also benefit from

## Pattern

**Wrong**: Save to Claude Code's local memory
```
~/.claude/projects/-Users-erb-Programming-alice/memory/qs-data.md
```
This only exists on one machine, in one runtime. If Alice runs via gptme on alice-vm, this knowledge is invisible.

**Correct**: Save to the brain repo
```
~/Programming/alice/knowledge/quantified-self/qs-data-landscape.md
```
This is versioned in git, available on all machines, included in all runtimes.

**Everything goes in the brain repo** — domain knowledge, user preferences, interaction style, feedback. There's no good reason for any of it to be local-only. Even "runtime-specific" preferences (like "user wants terse responses") are valuable across runtimes.

**Symlink approach** (recommended — no trade-off):
```bash
# Move Claude memory into brain repo, symlink back
mkdir -p knowledge/claude-memory
cp ~/.claude/projects/-Users-.../memory/* knowledge/claude-memory/
rm -rf ~/.claude/projects/-Users-.../memory
ln -s /path/to/brain/knowledge/claude-memory ~/.claude/projects/-Users-.../memory
```
Claude Code's auto-retrieval (MEMORY.md index + description-based relevance matching) still works, but the files live in git. You get Claude's retrieval mechanism AND git durability/portability.

## Outcome
Following this pattern ensures:
- Knowledge persists across all runtimes (gptme, Claude Code, Codex)
- Knowledge is versioned and reviewable via git
- Other agents or sessions on different machines can access it
- The agent's brain is the single source of truth, not scattered across runtime-local stores

## Related
- Agent workspace architecture (ARCHITECTURE.md in each brain repo)
- Task and journal systems are also brain-repo-local, not runtime-local

## Origin
2026-04-13: Alice saved QS data landscape to Claude Code local memory instead of brain repo's `knowledge/` directory. Erik corrected: "store stuff in alice's brain, not this local machine's memory."
