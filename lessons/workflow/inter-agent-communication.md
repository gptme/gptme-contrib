---
match:
  keywords: ["inter-agent communication", "agent-to-agent messaging", "cross-agent coordination", "communicate with other agent", "message another agent"]
status: active
---

# Inter-Agent Communication

## Rule
Use a channel that the other agent can actually read. GitHub issues work for any agent.
If your team uses a shared coordination repo, that's also valid. What doesn't work is
writing messages in your own workspace — they never leave your repo.

## Context
When you want to communicate with other agents or respond to messages from them.

## Detection
Observable signals that you need this rule:
- About to write a message to your own `messages/` or `sent/` directory
- Received a GitHub issue from another agent but thinking of responding locally
- Want to coordinate with another agent on a task
- Unsure which channel to use

## Pattern

### Option A: GitHub issues (works for any agent setup)
```text
# ✅ Comment on an existing issue to respond
gh issue comment <issue-number> --repo owner/agent-repo --body "Response..."

# ✅ Create new issue to initiate communication
gh issue create --repo owner/other-agent-repo --title "Topic" --body "..."
```

**When to use**: Any inter-agent communication. Default when you don't have a shared repo.

**Why it works**:
- Both agents have GitHub context in their autonomous runs
- Issues are visible to both agents and maintainers
- Comments create a clear thread with notification

### Option B: Shared coordination repo (if your team has one)
Some multi-agent setups use a shared git repository (e.g., `gptme-superuser`) that all
agents on the team have read/write access to. If your setup includes this:
```bash
# Write to a shared messages directory in the coordination repo
cat > shared-repo/messages/YYYY-MM-DD/from-alice-to-bob.md << 'EOF'
---
from: alice
to: bob
subject: "..."
---
Message content here.
EOF
cd shared-repo && git add messages/ && git commit -m "msg(bob): ..." && git push
```

**When to use**: Agent-to-agent messages that don't need human visibility, or when the
team uses the shared repo as the coordination hub.

**Why it works**: All agents pull the shared repo at session start, so messages reach them.

### ❌ Wrong: Messages in your own workspace
```text
# ❌ Wrong: Only you can see this
messages/sent/message-to-bob.md   # Stays in YOUR repo — Bob never sees it
```

The anti-pattern is messages written to your own workspace, not to a shared location.
If you're writing to a repo only you have access to, use GitHub issues instead.

## Responding to External Mentions

When your GitHub context shows a **Mentions** notification from another repo:
1. Read the issue/PR to understand what's being asked of you
2. Comment on that issue/PR to respond
3. This completes the communication loop

Example:
- See in GitHub context: **Mentions** (1): owner/agent-repo: Getting inter-agent communication working...
- Action: `gh issue comment 193 --repo owner/agent-repo --body "Your response"`

**Important**: Mentions on external repos are direct requests for your input. Always respond promptly.

## Outcome
Following this pattern enables:
- **Actual communication**: Messages reach the other agent
- **Visibility**: Both agents and maintainers can see the conversation
- **Threading**: Comments create natural conversation flow
- **Notifications**: GitHub context includes relevant issues
- **External collaboration**: Respond to mentions on other repos

## Related
- [Read Full GitHub Context](./read-full-github-context.md) - Reading GitHub issues/PRs completely
