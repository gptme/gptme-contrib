---
description: Record a durable friction signal when stuck, blocked, or repeatedly failing
when:
  - stuck on a task or tool failure
  - hitting the same blocker repeatedly
  - needing to attribute a blocker to self, tooling, operator, upstream, or architecture
action:
  kind: shell
  run: python3 scripts/vent.py "message describing friction" --resolution-owner <owner>
owner_paths:
  - scripts/vent.py
  - packages/metaproductivity/src/metaproductivity/friction.py
  - knowledge/analysis/
entrypoints:
  - cli
  - agent
aliases:
  - vent
  - friction-signal
  - register-friction
parameters:
  - name: message
    required: true
    description: Brief description of the blocker or repeated failure.
  - name: resolution-owner
    required: false
    description: Who or what unblocks it (`self`, `tooling`, `operator`, `upstream`, `architectural`).
verification:
  - Confirm the command prints a recorded timestamp and ledger path.
  - Use friction analysis to verify the signal surfaces in summaries and alerts.
---

# Vent

Routes to `scripts/vent.py`.

Use this when you're stuck, frustrated, or burning budget on a blocker. The
command appends a real-time friction event to the shared ledger instead of
burying the signal in journal prose.

Default form:

```bash
python3 scripts/vent.py "brief description of the blocker"
```

Recommended form when the unblock owner is clear:

```bash
python3 scripts/vent.py "missing API key prevents smoke test" --resolution-owner tooling
```

Resolution-owner values:

- `self` — better prompting, context, or reasoning should solve it now
- `tooling` — needs a tool, permission, config, or environment change
- `operator` — needs a human decision, approval, credential, or account action
- `upstream` — blocked on a dependency Bob doesn't control
- `architectural` — not solvable in the current stack without redesign

The ledger is shared across harnesses (`gptme`, Claude Code, Codex, web), so a
vent recorded here will also show up in metaproductivity analysis.

Useful follow-up:

```bash
uv run python3 -m metaproductivity.friction --journal-dir journal --last-n-sessions 20 --format summary --with-alerts
```

That turns raw vent events into aggregate friction summaries, blocker patterns,
and actionable alerts for future sessions.
