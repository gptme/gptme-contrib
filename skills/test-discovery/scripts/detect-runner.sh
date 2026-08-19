#!/usr/bin/env bash
# Detect the test runner for the current project and print the recommended command.
# Exit 0 on success (prints command), exit 1 if no runner detected.
set -euo pipefail

REASON=""
CMD=""

# Resolve pytest invocation: prefer uv run pytest when uv is available
if command -v uv &>/dev/null; then
    PYTEST_CMD="uv run pytest -x -q"
else
    PYTEST_CMD="pytest -x -q"
fi

# Python: pytest (priority: explicit config > pyproject > setup.cfg > tox > presence)
if [[ -f pytest.ini ]]; then
    CMD="$PYTEST_CMD"
    REASON="pytest.ini found"
elif [[ -f pyproject.toml ]] && grep -q '\[tool\.pytest' pyproject.toml 2>/dev/null; then
    CMD="$PYTEST_CMD"
    REASON="[tool.pytest.ini_options] in pyproject.toml"
elif [[ -f setup.cfg ]] && grep -q '^\[tool:pytest\]' setup.cfg 2>/dev/null; then
    CMD="$PYTEST_CMD"
    REASON="[tool:pytest] in setup.cfg"
elif [[ -f tox.ini ]]; then
    if grep -q '^\[pytest\]' tox.ini 2>/dev/null; then
        # tox.ini used as a plain pytest config — run pytest directly
        CMD="$PYTEST_CMD"
        REASON="[pytest] section in tox.ini"
    else
        # Real tox project (has [testenv] or similar) — let tox manage the env
        CMD="tox"
        REASON="tox.ini found"
    fi
elif command -v pytest &>/dev/null || command -v uv &>/dev/null; then
    # Heuristic: if there are test files, assume pytest
    if find . -name 'test_*.py' -o -name '*_test.py' 2>/dev/null | grep -q .; then
        CMD="$PYTEST_CMD"
        REASON="test_*.py files found (pytest assumed)"
    fi
fi

# JavaScript/TypeScript: vitest or jest (only if no Python runner found)
if [[ -z "$CMD" ]] && [[ -f package.json ]]; then
    if python3 -c "import json,sys; s=json.load(open('package.json')).get('scripts',{}); sys.exit(0 if any('vitest' in v for v in s.values()) else 1)" 2>/dev/null; then
        CMD="npx vitest run"
        REASON="vitest in package.json scripts"
    elif python3 -c "import json,sys; s=json.load(open('package.json')).get('scripts',{}); sys.exit(0 if 'test' in s else 1)" 2>/dev/null; then
        CMD="npm test"
        REASON="test script in package.json"
    fi
fi

# Rust: cargo test
if [[ -z "$CMD" ]] && [[ -f Cargo.toml ]]; then
    CMD="cargo test 2>&1"
    REASON="Cargo.toml found"
fi

# Makefile: test target
if [[ -z "$CMD" ]] && [[ -f Makefile ]]; then
    if grep -q '^test[[:space:]]*:' Makefile 2>/dev/null; then
        CMD="make test"
        REASON="test target in Makefile"
    fi
fi

if [[ -z "$CMD" ]]; then
    echo "No test runner detected" >&2
    exit 1
fi

echo "$CMD  # $REASON"
