---
name: project-setup-interview
description: "Conduct a structured interview about a new project and generate a ready-to-use CLAUDE.md/AGENTS.md starter so the agent knows your conventions from session one."
license: MIT
compatibility: "Requires gptme"
metadata:
  author: bob
  version: "1.0.0"
  tags: [onboarding, setup, claude-md, agents-md, project-configuration]
  requires_tools: []
  requires_skills: []
keywords:
  - "set up gptme for my project"
  - "generate CLAUDE.md"
  - "generate AGENTS.md"
  - "configure agent for new project"
  - "project onboarding interview"
  - "gptme setup"
---

# Project Setup Interview Skill

Generate a tailored `CLAUDE.md` (or `AGENTS.md`) for a new project by conducting a
short structured interview. The agent asks 6 focused questions, then outputs a
ready-to-use configuration file that covers commands, conventions, workflow, and
agent permissions — so every future session starts with the right context.

## When to Use

- Starting gptme on a project that has no `CLAUDE.md`/`AGENTS.md` yet
- Onboarding a new developer to agent-assisted development
- Refreshing stale project configuration after a major refactor
- User says "set up gptme for my project" or "configure the agent"

## When NOT to Use

- The project already has a `CLAUDE.md` (update it directly instead)
- The setup is for Bob's own brain repo (use `ABOUT.md`/`SOUL.md` conventions)

## Process

### Phase 0: Check for existing configuration

```bash
ls CLAUDE.md AGENTS.md .claude/CLAUDE.md 2>/dev/null
cat CLAUDE.md 2>/dev/null | head -20
```

If a config exists, offer to update it rather than replace it. Skip to the
questions whose answers would change.

### Phase 1: Six interview questions

Ask these in order. Accept short answers — you will infer the rest from context.

**Q1 — Project identity**
> "What does this project do? One or two sentences."

What to listen for: product type (web app, CLI, library, data pipeline, infra),
domain, and primary stakeholders (end users vs. internal vs. other services).

**Q2 — Stack**
> "What's the primary language and key dependencies? (e.g. Python + FastAPI + pytest, or TypeScript + Next.js + vitest)"

What to listen for: language determines linting tools, import conventions, and test
runner defaults. Multiple languages are common — note the primary one.

**Q3 — Development commands**
> "What are the commands to build, test, and run the project? Even rough ones are fine."

What to listen for: exact commands (e.g. `uv run pytest`, `npm run dev`, `cargo test`).
If the user is unsure, inspect the repo: `ls Makefile pyproject.toml package.json Cargo.toml` and infer.

**Q4 — Code conventions**
> "Any coding conventions I should follow? (linter, formatter, import style, docstrings, naming...)"

What to listen for: ruff/black/eslint/prettier/gofmt, mandatory docstrings, snake_case vs
camelCase overrides, typing requirements. If they say "just follow PEP 8", that's enough.

**Q5 — Git workflow**
> "What's the git workflow? (branch naming, commit style, protected branches, PR process...)"

What to listen for: `feat/...` branches, Conventional Commits, protected main/master,
squash-merge preference, required CI gates. If the repo has a CONTRIBUTING.md, read it.

**Q6 — Agent permissions**
> "What should I do freely vs. ask before doing? (e.g. run tests freely, but ask before pushing)"

What to listen for: autonomy boundaries. Common splits:
- Read files freely, ask before writing
- Write freely, ask before pushing/deleting
- Run tests freely, ask before changing dependencies
- Full autonomy except for production credentials

### Phase 2: Inspect the repository

After the interview, do a quick repo read to fill in gaps:

```bash
# Project structure
ls -1

# Existing tooling (infer commands if Q3 was vague)
cat Makefile 2>/dev/null | head -40
cat pyproject.toml 2>/dev/null | head -30
cat package.json 2>/dev/null | grep -A 20 '"scripts"'
cat Cargo.toml 2>/dev/null | head -20

# CI config (infer workflow and test commands)
ls .github/workflows/ 2>/dev/null
cat .github/workflows/*.yml 2>/dev/null | head -60

# Existing conventions
cat .pre-commit-config.yaml 2>/dev/null | head -30
cat .ruff.toml pyproject.toml setup.cfg 2>/dev/null | grep -A 5 '\[tool.ruff\]'
```

### Phase 3: Generate CLAUDE.md

Output the file directly. Use this template, filling in every `<...>` from the
interview answers and repo inspection:

```markdown
# <Project Name>

<One-sentence description of what the project does.>

## Tech Stack

- **Language**: <primary language + version if known>
- **Framework**: <framework / runtime>
- **Testing**: <test runner and key test libraries>
- **Build / packaging**: <build tool>

## Development Commands

```bash
# Install dependencies
<install command>

# Run tests
<test command>

# Lint / format
<lint command>

# Run / start
<run command>

# Build (if applicable)
<build command>
```

## Code Style

- <Linter/formatter> — run <command> before committing
- <Naming convention notes>
- <Import ordering notes, if any>
- <Docstring / type annotation requirements>

## Git Workflow

- **Branches**: <branch naming pattern, e.g. `feat/...`, `fix/...`>
- **Commits**: <commit style, e.g. Conventional Commits: `feat(area): message`>
- **Protected branches**: <list protected branches>
- **PR process**: <squash/rebase/merge preference, required reviews>

## Agent Guidelines

**Do freely:**
<list from Q6 answers>

**Ask first:**
<list from Q6 answers>

**Never:**
- Commit secrets or credentials
- Push directly to <protected branches>
- <any hard stops the user named>
```

### Phase 4: Write and confirm

```bash
cat > CLAUDE.md << 'HEREDOC'
<generated content>
HEREDOC
```

Show the user the first 20 lines, then ask: "Does this look right? Any corrections?"

Make any adjustments they request, then offer to commit:

```bash
git add CLAUDE.md
git commit -m "docs: add CLAUDE.md for agent configuration"
```

## Tips

- If the user is a first-time gptme user, add a brief "How to use gptme" note at the top
  with the 3 most useful commands (`gptme`, `gptme 'task'`, `ctrl+C` to interrupt)
- If the project uses Conventional Commits, mirror that style in all future commits
- Keep the generated file under 150 lines — longer files are rarely read end-to-end
- Link to `CONTRIBUTING.md` or project docs instead of duplicating content

## Example output (Python library)

```markdown
# mylib

Fast numerical computing library built on NumPy.

## Tech Stack

- **Language**: Python 3.11+
- **Framework**: NumPy, SciPy
- **Testing**: pytest + pytest-cov
- **Build / packaging**: uv + hatch

## Development Commands

```bash
# Install dependencies
uv sync --all-packages

# Run tests
uv run pytest -x -q

# Lint / format
uv run ruff check . && uv run ruff format .

# Build
uv build
```

## Code Style

- ruff — run `uv run ruff check --fix .` before committing
- snake_case for all functions and variables
- Public functions require docstrings (Google style)
- Type annotations required for all public APIs

## Git Workflow

- **Branches**: `feat/...`, `fix/...`, `docs/...`
- **Commits**: Conventional Commits (`feat(core): add batch processing`)
- **Protected branches**: main
- **PR process**: squash merge, one approver required

## Agent Guidelines

**Do freely:**
- Read and analyze any file
- Write code and tests
- Run `pytest`, `ruff`, `mypy`

**Ask first:**
- Adding new dependencies
- Refactoring public APIs
- Deleting files

**Never:**
- Push directly to main
- Modify `.github/secrets` or CI credentials
```
