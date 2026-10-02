# gptme-user-memories

Long-term user memory for [gptme](https://gptme.org), similar to ChatGPT's "memory" feature: after each conversation it extracts durable facts about you (preferences, projects, working style) and saves them to a local Markdown file that future sessions can include in context.

**Status:** experimental.

## How it works

1. A `SESSION_END` hook runs when a gptme conversation ends.
2. It skips sessions that look autonomous (agent run-loop prompts), sessions with fewer than 50 characters of user text, and sessions it has already processed (a `.memories-extracted` marker file in the conversation's log directory).
3. It sends **your messages only** (capped at 400 characters per message and 8,000 characters in total) to an Anthropic model, `claude-haiku-4-5-20251001` by default, and asks for a list of facts about you.
4. New facts are merged into `~/.config/gptme/user-memories/facts.md`, case-insensitively deduplicated and sorted.
5. You include that file in future sessions (see below). The plugin does not inject it for you.

If the API call fails or no API key is configured, the conversation is left unmarked, so it is retried the next time that conversation ends (for example after resuming it) or by the backfill CLI below.

## Install

Install the package into the same Python environment as gptme (it depends on the `anthropic` SDK and provides the `gptme-user-memories` CLI):

```sh
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-user-memories"
# or, for a pipx-installed gptme:
pipx inject --include-apps gptme "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-user-memories"
```

Then point gptme at the plugin so the hook is registered, in `~/.config/gptme/config.toml` (recommended, since memories are per-user) or a project's `gptme.toml`:

```toml
[plugins]
paths = ["/path/to/gptme-contrib/plugins/gptme-user-memories"]
enabled = ["gptme_user_memories"]
```

### API key

The extractor reads `ANTHROPIC_API_KEY` from the environment, or from the `[env]` table of `~/.config/gptme/config.toml`.

## Use the memories in future sessions

Add the file to the prompt files in your user config, `~/.config/gptme/config.toml` (a project `gptme.toml` only includes files inside the workspace, so it can't reference this path):

```toml
[prompt]
files = ["~/.config/gptme/user-memories/facts.md"]
```

or print it from a context script:

```sh
cat ~/.config/gptme/user-memories/facts.md 2>/dev/null
```

The file is plain Markdown that you can read and edit:

```markdown
# User Memories

Facts about the user extracted from past gptme conversations.
Last updated: 2026-03-11

- Prefers Python for scripting, TypeScript for web
- Uses Vim as primary editor
```

## Backfill from past sessions (CLI)

`gptme-user-memories` scans recent conversations and writes the results to the same file. It reads gptme logs (`~/.local/share/gptme/logs/`) **and Claude Code transcripts (`~/.claude/projects/`)**, applying the same autonomous-session and length filters.

```sh
gptme-user-memories --dry-run              # print what would be extracted, write nothing
gptme-user-memories --days 30 --limit 50   # scan the last 30 days, at most 50 sessions
gptme-user-memories --force                # re-process sessions already marked as done
gptme-user-memories --output ~/my-memories.md
gptme-user-memories --model claude-haiku-4-5-20251001
gptme-user-memories --categorize           # write preferences.md, projects.md, personal.md instead of facts.md
```

Defaults: `--days 14`, `--limit 30`. With `--categorize`, files go to `~/.config/gptme/user-memories/` and `--output` is ignored.

### Choosing the model

- Hook: set the `GPTME_MEMORIES_MODEL` environment variable to an Anthropic model ID.
- CLI: pass `--model`; the CLI does not read `GPTME_MEMORIES_MODEL`.

## Privacy

Conversation text, limited to your own messages, is sent to Anthropic's API for extraction; nothing else leaves your machine, and the results are stored locally. The extraction prompt asks the model not to keep private or sensitive information, but this is not guaranteed, so review `facts.md` from time to time. To keep certain conversations out entirely, don't enable the plugin for those projects, and avoid running the CLI backfill over logs you don't want processed.

## Python API

```python
from pathlib import Path
from gptme_user_memories.extractor import (
    USER_MEMORIES_FILE, process_logdir, load_existing_memories, merge_facts, save_memories,
)

new_facts = process_logdir(Path("~/.local/share/gptme/logs/<conversation>").expanduser())
if new_facts is not None:  # None: skipped (filtered or already processed) or API error
    merged = merge_facts(load_existing_memories(USER_MEMORIES_FILE), new_facts)
    save_memories(USER_MEMORIES_FILE, merged)
```

## Related

- [gptme plugin docs](https://gptme.org/docs/plugins.html)
- [Other gptme-contrib plugins](../README.md)
