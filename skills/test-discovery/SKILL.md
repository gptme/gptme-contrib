---
name: test-discovery
description: Auto-discover and run the relevant test suite after making code edits. Use when you've edited source files and want to verify correctness without being told which tests to run. Do NOT use for purely exploratory sessions with no file edits, or when the user has already specified which test command to run.
license: MIT
compatibility: "Works with projects using pytest, jest/vitest, cargo test, or Makefile targets"
metadata:
  author: bob
  version: "1.0.0"
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

## Procedure

### Step 1: Detect the test runner

Run the bundled detector:

```shell
bash skills/test-discovery/scripts/detect-runner.sh
```

Or detect manually by priority:

| Signal file | Runner | Command |
|---|---|---|
| `pytest.ini` / `[tool.pytest]` in `pyproject.toml` / `setup.cfg [tool:pytest]` | pytest | `uv run pytest -x -q` |
| `package.json` with `"vitest"` or `"jest"` in scripts | jest/vitest | `npm test` or `npx vitest run` |
| `Cargo.toml` | cargo | `cargo test 2>&1` |
| `Makefile` with `test` target | make | `make test` |
| `tox.ini` | tox | `tox` |

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
# Run test file matching changed source
npx vitest run src/<module>.test.ts
# Or jest
npx jest src/<module>.test.ts --no-coverage
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

`scripts/detect-runner.sh` returns the recommended test command for the current
directory and prints a brief rationale. Exit 0 = found, exit 1 = no test runner detected.

```shell
bash skills/test-discovery/scripts/detect-runner.sh
# → uv run pytest -x -q  (pytest.ini found)
# → npm test  (package.json with jest script found)
# → cargo test 2>&1  (Cargo.toml found)
# → echo "No test runner detected"  (exit 1)
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
- gptme issue idea #1136 — tool-side auto-detection (post-exec hook in shell.py, deferred until PR queue < 12)
