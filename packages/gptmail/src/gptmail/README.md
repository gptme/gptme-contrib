# gptmail source

User documentation lives in the [package README](../../README.md). This file maps the
source modules.

| Module | Role |
|---|---|
| `cli.py` | The `gptmail` click CLI (email commands). Registers the `agent` subgroup. |
| `agent_cli.py` | `gptmail agent …`: inter-agent messaging over SSH/SCP. Never imports the email stack. |
| `lib.py` | `AgentEmail`: Markdown mail storage, compose/send (via msmtp), threading, Maildir import/export, reply tracking. |
| `transport/` | `Transport` protocol plus `EmailTransport` (wraps `AgentEmail`) and `AgentTransport` (filesystem inbox/outbox). |
| `watcher.py` | Experimental mbsync → import → `gptme` auto-reply loop (`python -m gptmail.watcher [once\|one]`). |
| `complexity.py` | Heuristic email complexity scoring. |
| `migrate_lock_format.py` | One-off migration script for old reply-tracking state in `email/locks/email.json`. |
| `communication_utils/` | Shared rate limiting, locks, conversation tracking, auth, retry and logging, also used by the Discord, Telegram and Twitter scripts. See [its README](communication_utils/README.md). |
| `DESIGN.md` | Historical design notes. |

The watcher resolves the agent workspace from `GPTME_WORKSPACE`. Without it, the
watcher walks four directories up from this source directory. That only works when the
package sits in a source tree directly under the workspace, so set `GPTME_WORKSPACE`
for installed or submodule layouts.
