---
name: test-discovery
description: Auto-discover and run the relevant test suite after making code edits. Use when you've edited source files and want to verify correctness without being told which tests to run. Do NOT use for purely exploratory sessions with no file edits, or when the user has already specified which test command to run.
license: MIT
compatibility: "Works with projects using pytest, tox, an npm test script, cargo test, or Makefile targets"
metadata:
  author: bob
  version: "1.1.0"
  tags: [testing, pytest, jest, cargo, test-discovery, tdd, verification]
  requires_tools: [shell]
  requires_skills: []
---

# Test Discovery Skill

Auto-detect and run the right test suite after code edits. Agents using this skill
self-correct from test failures without a manual "run tests" prompt.

## When to Use

- After editing source files (`*.py`, `*.ts`, `*.js`, `*.rs`)
- After fixing a bug — run the suite to catch regressions
- When you suspect a change might have broken something
- Before committing, to confirm nothing regressed

## When NOT to Use

- User explicitly says which test command to run — just run that
- Exploratory or documentation-only session with no source edits
- The test suite is known to be very slow and the change is clearly non-breaking

## Automatic Completion Verification

gptme can run this discovery itself when the agent calls `complete`. Set
`GPTME_VERIFY_COMPLETION_AUTO=1` (off by default, gptme with
[gptme/gptme#3755](https://github.com/gptme/gptme/pull/3755)). After an
episode that changed source, test or manifest files, the `complete` hook
runs the discovered command, feeds a failure back to the agent as untrusted
output, and closes once it passes or the retries run out. Documentation-only
edits and test runs alone do not trigger it. Precedence: the
`GPTME_VERIFY_COMPLETION` command, then `.gptme/verify-completion.sh`, then
the discovered runner. The discovered command goes through normal shell
confirmation.

With that enabled, use this skill for the faster mid-task loop (Step 2's
narrowed runs); the full-suite check at completion is automatic.

## Procedure

### Step 1: Detect the test runner

Run the bundled detector:

```shell
bash skills/test-discovery/scripts/detect-runner.sh
```

It mirrors gptme's canonical detector
(`gptme.util.completion_verification.discover_verification_command`), first
match wins:

| Signal | Runner | Command |
|---|---|---|
| `pytest.ini`, `[tool.pytest]` in `pyproject.toml`, `[pytest]`/`[tool:pytest]` in `setup.cfg`, `[pytest]` in `tox.ini` | pytest | `uv run pytest -x -q` |
| `[tox]` in `tox.ini`, `[tool.tox]` in `pyproject.toml`, `[tox]`/`[tox:tox]` in `setup.cfg` | tox | `tox` |
| `package.json` with an exact `"test"` script | npm | `npm test` |
| `Cargo.toml` | cargo | `cargo test` |
| `Makefile`/`makefile`/`GNUmakefile` with a `test:` or `test::` target | make | `make test` |

There is no `npx` fallback: `npx` can download and execute a package that is
not in the project. `npm test` also runs `pretest`/`posttest`; the detector
prints those scripts on stderr so you can read them first.

### Step 2: Narrow the scope

Run only the tests relevant to changed files. This keeps feedback fast:

```shell
# Python: run tests for the module you edited
uv run pytest tests/test_<module>.py -x -q

# Broader: all tests in the package
uv run pytest packages/<pkg>/tests/ -x -q

# Last resort: full suite
uv run pytest -x -q
```

For JavaScript/TypeScript:

```shell
# Pass the changed test file through to the project's own test script
npm test -- src/<module>.test.ts
```

For Rust:

```shell
cargo test <module_name> 2>&1 | tail -20
```

### Step 3: Interpret results

**Pass**: Commit with confidence. State "tests pass" in the response.

**Fail**: Read the failure message. One of three paths:
1. **Fix is obvious** → fix the code, re-run (one cycle max before escalating)
2. **Test is outdated** → update the test to reflect intentional behavior change
3. **Unclear root cause** → surface the exact failing assertion to the user; don't guess

### Step 4: Keep output concise

Test output can be verbose. Summarize for the user:

```
Tests: 47 passed, 0 failed (3.2s)
```

Or on failure:

```
FAILED tests/test_shell.py::test_execute_timeout – AssertionError: expected exit 1, got 0
```

Never dump the full pytest traceback into the response unless the user asks for it.

## Detection Script

`scripts/detect-runner.sh [DIR]` prints the recommended test command for `DIR`
(default: current directory) with a brief rationale. Exit 0 = found, exit 1 = no
test runner detected.

```shell
bash skills/test-discovery/scripts/detect-runner.sh
# → uv run pytest -x -q  # pytest.ini contains explicit pytest configuration
# → npm test  # package.json defines an exact test script
# → cargo test  # Cargo.toml defines a Cargo project
# → "No test runner detected" on stderr  (exit 1)
```

## Anti-Patterns

| Anti-Pattern | Fix |
|---|---|
| Running the full suite for a one-line fix | Scope to the test file for the changed module |
| Dumping the full traceback into the response | Summarize: `FAILED test_foo::test_bar – AssertionError: X != Y` |
| Skipping tests because "it's obviously right" | Run at least the module-level tests; correctness is not obvious |
| Re-running tests 3+ times hoping they pass | Two strikes and escalate — report the failing assertion |
| Guessing the test command from memory | Use the detection script or check for signal files |

## Verification

The skill worked if:
- You can state a concrete pass/fail count after edits
- Failures led to a fix or a clear user-facing error message
- You did not dump raw test output into the conversation

## Related

- `skills/test-driven-development/SKILL.md` — write tests *before* code
- `lessons/workflow/verifiable-tasks-principle.md` — prefer tasks with objective verification
- [gptme/gptme#3755](https://github.com/gptme/gptme/pull/3755) — opt-in completion-time discovery (`GPTME_VERIFY_COMPLETION_AUTO=1`), the canonical detector this skill mirrors
