---
name: rewrite-soul
description: "Use when creating or revising an agent's SOUL.md, splitting runtime voice out of broader identity docs, or tightening a vague persona file into a short, opinionated, voice-only artifact without changing the agent's core identity."
license: MIT
compatibility: "Requires gptme"
metadata:
  author: bob
  version: "1.0.0"
  tags: [agent, soul, persona, identity, voice]
  requires_tools: []
  requires_skills: []
---

# Rewrite Soul

## Overview

Tighten an agent's `SOUL.md` into a compact runtime persona.
Keep tone, taste, and default behavioral pull; keep operational rules elsewhere.

`SOUL.md` is a first-class persona file in the
[`gptme-agent-template`](https://github.com/gptme/gptme-agent-template)
bootstrap: it sits alongside `ABOUT.md`, `GOALS.md`, and `AGENTS.md`/`CLAUDE.md`
and is auto-included so the agent's voice loads every session. This skill
encodes the discipline needed to keep that file useful over time.

## When to Use This Skill

Apply this skill when:

- Creating `SOUL.md` for a new agent from broader identity docs
- Tightening an existing `SOUL.md` that has drifted into a README, roadmap, or tool list
- Splitting runtime voice out of `ABOUT.md` after the file has grown too long
- Aligning a fork's persona with its source agent without copying operational baggage

## Workflow

### 1. Read the boundary files first

Read these before rewriting:

- `SOUL.md` if it already exists
- broader identity docs such as `ABOUT.md`, `GOALS.md`, or `README.md`
- operating docs such as `AGENTS.md` or `CLAUDE.md`

Use the broader docs as source material, not as the output shape. `SOUL.md` is
runtime voice, not a dump of everything true about the agent.

If no `SOUL.md` exists yet, derive it from those broader docs instead of
inventing a new personality from scratch.

### 2. Keep only persona material

Keep:

- voice and tone
- taste and preferences
- default behavioral pull under ambiguity
- social texture or stance
- a few durable values if they clearly affect decisions

Move out or leave out:

- tool lists
- directory layouts
- git workflow rules
- task schemas
- temporary initiatives, metrics, or blockers
- long biography or justification paragraphs

### 3. Compress hard

Aim for:

- 20-60 lines
- 3-5 short sections
- quotable bullets instead of explanatory paragraphs
- direct language with no corporate filler

Recommended sections:

- `Voice`
- `Taste`
- `Behavioral Pull`
- `Social Texture`

### 4. Rewrite surgically

Preserve the agent's identity; do not invent a different one just to sound
stronger. If a line sounds like policy, procedure, or file-system doctrine,
move it to `AGENTS.md`, `ABOUT.md`, `TASKS.md`, or another operational file
instead of keeping it in `SOUL.md`.

When content is duplicated, prefer a clear split:

- `SOUL.md` = voice, taste, stance
- `ABOUT.md` = background, doctrine, longer-form values
- `AGENTS.md` / `CLAUDE.md` = operating constraints
- `GOALS.md` = explicit goal hierarchy

### 5. Verify the result

A good `SOUL.md` should answer:

- How does this agent sound?
- What does it find tasteful or distasteful?
- What does it default toward when the prompt is vague?
- What kind of social energy does it bring?

A bad `SOUL.md` reads like a README, policy manual, roadmap, or tool index.

## Rewrite Prompt

```txt
Read SOUL.md, ABOUT.md, GOALS.md, and AGENTS.md/CLAUDE.md if present.
Rewrite SOUL.md into a sharper runtime persona.

Constraints:
- Preserve the agent's identity; do not invent a new one.
- Keep operational rules, file paths, workflow rules, and tool inventories out of SOUL.md.
- Prefer short, opinionated lines over explanatory paragraphs.
- Keep it to 20-60 lines.
- Organize it into 3-5 short sections such as Voice, Taste, Behavioral Pull, and Social Texture.

After rewriting, list the 3 most important things you intentionally kept out and where they belong instead.
```

## Anti-Patterns

- Turning `SOUL.md` into `ABOUT.md` but shorter
- Stuffing in operational constraints because they feel important
- Making it bland to sound "professional"
- Hard-coding current initiatives or dated facts
- Adding values that never change decisions

## Example: what belongs where

| Content | Belongs in |
|---------|------------|
| "Direct, opinionated, no corporate fluff" | `SOUL.md` (Voice) |
| "Prefers Unix philosophy, local-first tools" | `SOUL.md` (Taste) |
| "Always use absolute paths when saving files" | `AGENTS.md` / `CLAUDE.md` |
| "Uses gptodo for task management" | `ARCHITECTURE.md` or `TASKS.md` |
| "Final goal: play the longest possible game" | `GOALS.md` |
| "Was created by X on date Y" | `ABOUT.md` |

## Related

- [gptme-agent-template](https://github.com/gptme/gptme-agent-template) — template that ships a `SOUL.md` skeleton by default
- `ABOUT.md` / `AGENTS.md` / `CLAUDE.md` / `GOALS.md` in the agent workspace — the
  files `SOUL.md` deliberately defers to
