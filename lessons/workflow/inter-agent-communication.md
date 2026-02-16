---
match:
  keywords: ["inter-agent communication", "agent-to-agent messaging", "cross-agent coordination", "communicate with other agent", "message another agent"]
status: active
---

# Inter-Agent Communication

## Rule
Use GitHub issues for inter-agent communication, not local file-based message systems.

## Context
When you want to communicate with other agents or respond to messages from them.

## Detection
Observable signals that you need this rule:
- About to write a message to messages/ directory for another agent
- Received a GitHub issue from another agent but thinking of responding locally
- Want to coordinate with another agent on a task
- Building a "message system" for agent communication

## Pattern
Use GitHub issues for cross-agent communication:
```text
# ❌ Wrong: Local file-based messages (don't cross repos)
messages/sent/message-to-agent.md  # Other agent can't see this!

# ✅ Correct: GitHub issues (both agents can see)
gh issue comment <issue-number> --repo owner/agent-repo --body "Response..."

# ✅ Correct: Create new issue to initiate communication
gh issue create --repo owner/other-agent-repo --title "Question for Agent" --body "..."
```

**Why GitHub issues work**:
- Both agents have GitHub context in their autonomous runs
- Issues are visible to both agents
- Comments create a clear thread
- Maintainers can also see and participate

**Why local messages don't work**:
- Messages in your workspace stay in YOUR workspace
- Other agent would have to manually check your repo
- No notification mechanism
- No way for other agent to "receive" the message

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
