# gptme-user-memories

A gptme plugin that automatically extracts user facts from past conversations and stores them for inclusion in future sessions — like ChatGPT's "memory" feature but fully local.

## How it works

1. A `SESSION_END` hook runs after each conversation
2. It checks if the session was a personal (non-autonomous) conversation
3. Uses Claude Haiku to extract key user facts from the conversation
4. Stores them in `~/.local/share/gptme/user-memories.md`
5. You include that file in your gptme.toml or context script

## Installation

```sh
pip install gptme-user-memories
# or
uv pip install gptme-user-memories
```

## Configuration

### 1. Enable the plugin in `gptme.toml`

```toml
[plugins]
paths = ["~/.config/gptme/plugins"]
enabled = ["user_memories"]
```

### 2. Include extracted memories in future sessions

Add to your `gptme.toml`:

```toml
[prompt]
files = [
    # ... other files ...
    "~/.local/share/gptme/user-memories.md",
]
```

Or in your context script (`context.sh`):

```bash
if [[ -f ~/.local/share/gptme/user-memories.md ]]; then
    echo "# User Memories"
    cat ~/.local/share/gptme/user-memories.md
fi
```

### 3. Provide an Anthropic API key

The plugin uses Claude Haiku for fact extraction. It reads the key from:

1. `ANTHROPIC_API_KEY` environment variable
2. `~/.config/gptme/config.toml` under `[env] ANTHROPIC_API_KEY`

### 4. (Optional) Override the extraction model

The hook uses `claude-haiku-4-5-20251001` by default. To use a different model:

```sh
export GPTME_MEMORIES_MODEL=claude-3-5-haiku-20241022
```

This applies to both the `SESSION_END` hook and the `gptme-user-memories` CLI.

## What gets extracted

The plugin looks for personalisation-relevant facts:

- Technical preferences (languages, frameworks, editors, workflows)
- Communication style (terse vs detailed, what they find helpful)
- Ongoing projects and goals
- Personal facts relevant to work (timezone, company, role)

It deliberately skips:
- Autonomous agent sessions (looks for gptme/Claude Code autonomous patterns)
- Generic preferences ("prefers code that works")
- Short conversations (< 50 chars of user content)
- Already-processed sessions (sentinel files prevent re-mining)

## Privacy

All processing is local. The only external call is to Anthropic's API to extract facts — the conversation text is sent to Claude Haiku for analysis. No data is sent to any other service. To avoid sending sensitive conversations, the plugin includes autonomous session filtering and skips conversations with < 50 chars of user content.

## Storage

Memories are stored in `~/.local/share/gptme/user-memories.md` as a simple markdown list:

```markdown
# User Memories

Facts about the user extracted from past gptme conversations.
Last updated: 2026-03-11

- Prefers Python for scripting, TypeScript for web
- Works on open-source time tracking (ActivityWatch)
- Uses Vim as primary editor
```

Facts are deduplicated and sorted alphabetically on each update.

## CLI usage

After installation, the `gptme-user-memories` command is available for backfilling memories from past sessions:

```sh
# Dry-run: show what would be extracted without writing anything
gptme-user-memories --dry-run

# Backfill the last 30 days, up to 50 sessions
gptme-user-memories --days 30 --limit 50

# Re-process already-handled sessions
gptme-user-memories --force

# Write to a custom output file
gptme-user-memories --output ~/my-memories.md

# Use a specific model
gptme-user-memories --model claude-3-5-haiku-20241022
```

## Manual extraction (Python API)

You can also drive extraction directly from the `extractor` module:

```python
from gptme_user_memories.extractor import process_logdir, USER_MEMORIES_FILE
from gptme_user_memories.extractor import load_existing_memories, merge_facts, save_memories
from pathlib import Path

# Process a specific session
new_facts = process_logdir(Path("~/.local/share/gptme/logs/my-session").expanduser())

# Merge and save (process_logdir returns None if session was filtered)
if new_facts is not None:
    existing = load_existing_memories(USER_MEMORIES_FILE)
    merged = merge_facts(existing, new_facts)
    save_memories(USER_MEMORIES_FILE, merged)
```
