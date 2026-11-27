# Agent changelog

Written for agents that pin this repo as a submodule. A pin bump moves code;
it does not tell an agent which new hooks, env vars, config keys, or migrations
to adopt. This file does.

**Code-only changes do not need an entry.** No row means action class `none`.
Add a row when an existing agent must or should *do* something after pulling
the new pin.

After a bump, print the new action items (`OLD` and `NEW` are the previous and
current gitlink SHAs):

```txt
python3 scripts/print-agent-changelog.py OLD NEW
```

The printer emits `recommended` and `required` rows by default (`--all`
includes `optional`). File those as tasks in the agent workspace.

## Action classes

| Class | Meaning |
|-------|---------|
| `optional` | New capability. Adopt if useful. |
| `recommended` | Safety or hardening. Adopt unless there is a reason not to. |
| `required` | Breaking change or migration. Must act. |

## Entry format

Newest first, below [Entries](#entries). The heading date is UTC.

````markdown
## YYYY-MM-DD - short title

- **Action:** optional | recommended | required
- **Do:** one or two lines of what the agent should do
````

## Entries

## 2026-11-27 - Agent changelog convention

- **Action:** recommended
- **Do:** After each gptme-contrib pin bump, run `python3 scripts/print-agent-changelog.py OLD NEW` and file any printed rows as tasks in the agent workspace.
