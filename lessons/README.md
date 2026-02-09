# Lessons

Shared lessons for gptme agents. These lessons provide behavioral guidance, workflow patterns, and tool-specific knowledge that can be loaded dynamically based on context keywords.

## Categories

| Category | Description |
|----------|-------------|
| [autonomous](./autonomous/) | Autonomous operation patterns and session management |
| [communication](./communication/) | GitHub collaboration and professional communication patterns |
| [concepts](./concepts/) | Core concepts and mental models |
| [patterns](./patterns/) | Reusable behavioral patterns |
| [social](./social/) | Social interaction and communication patterns |
| [tools](./tools/) | Tool-specific lessons (shell, git, etc.) |
| [workflow](./workflow/) | Workflow and process lessons |

## Usage

Lessons are automatically loaded by gptme when conversation context matches the lesson's keywords. Configure lesson directories in your `gptme.toml`:

```toml
[lessons]
dirs = ["gptme-contrib/lessons"]
```

## Lesson Format

Each lesson follows a standard format with YAML frontmatter:

```yaml
---
match:
  keywords:
    - "trigger phrase"
    - "another trigger"
---

# Lesson Title

## Rule
One-sentence imperative.

## Context
When this applies.

## Pattern
Minimal correct example.

## Outcome
Benefits when followed.
```

See the gptme documentation for more details on creating and using lessons.
