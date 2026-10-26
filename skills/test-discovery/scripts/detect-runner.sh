#!/usr/bin/env bash
# Detect the test runner for a project and print the recommended command.
# Usage: detect-runner.sh [DIR]   (default: current directory)
# Exit 0 on success (prints "CMD  # REASON"), exit 1 if no runner detected.
#
# Mirrors gptme's canonical detector, gptme.util.completion_verification.
# discover_verification_command, used by GPTME_VERIFY_COMPLETION_AUTO=1:
# pytest config > tox config > exact npm "test" script > Cargo > Make "test"
# target. There is deliberately no npx fallback: npx can fetch and execute
# packages that are not in the project.
set -euo pipefail

# Keep non-Python projects usable on images without Python. Structured Python
# and npm configuration detection below requires python3; do not guess at JSON.
if ! command -v python3 >/dev/null 2>&1; then
    workspace=${1:-.}
    if [[ -f "$workspace/Cargo.toml" ]]; then
        echo "cargo test  # Cargo.toml defines a Cargo project"
        exit 0
    fi
    # GNU Make reads only the first existing default makefile.
    for name in GNUmakefile makefile Makefile; do
        if [[ -f "$workspace/$name" ]]; then
            if grep -qE '^test[[:space:]]*:' "$workspace/$name"; then
                echo "make test  # $name defines a test target"
                exit 0
            fi
            break
        fi
    done
    echo "No test runner detected (python3 unavailable for Python/npm configuration)" >&2
    exit 1
fi

exec python3 - "${1:-.}" <<'PY'
import configparser
import json
import re
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

ws = Path(sys.argv[1])
pytest_cmd = "uv run pytest -x -q" if shutil.which("uv") else "pytest -x -q"


def read(name):
    path = ws / name
    if not path.is_file():
        return None
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def toml_has_table(text, *keys):
    if tomllib is None:
        return re.search(r"(?m)^\[" + re.escape(".".join(keys)) + r"[\].]", text) is not None
    try:
        cur = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return False
        cur = cur[key]
    return True


def ini_has_section(text, *names):
    parser = configparser.RawConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return False
    return bool({s.lower() for s in parser.sections()} & {n.lower() for n in names})


def found(cmd, reason):
    print(f"{cmd}  # {reason}")
    sys.exit(0)


# 1. Explicit pytest configuration
for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"):
    text = read(name)
    if text is None:
        continue
    if (
        name == "pytest.ini"
        or (name == "pyproject.toml" and toml_has_table(text, "tool", "pytest"))
        or (name == "setup.cfg" and ini_has_section(text, "pytest", "tool:pytest"))
        or (name == "tox.ini" and ini_has_section(text, "pytest"))
    ):
        found(pytest_cmd, f"{name} contains explicit pytest configuration")

# 2. tox configuration
for name in ("tox.ini", "pyproject.toml", "setup.cfg"):
    text = read(name)
    if text is None:
        continue
    if (
        (name == "tox.ini" and ini_has_section(text, "tox"))
        or (name == "pyproject.toml" and toml_has_table(text, "tool", "tox"))
        or (name == "setup.cfg" and ini_has_section(text, "tox", "tox:tox"))
    ):
        found("tox", f"{name} contains tox configuration")

# 3. Exact npm "test" script (pre/post lifecycle scripts run too: shown on stderr)
text = read("package.json")
if text is not None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
        for hook in ("pretest", "test", "posttest"):
            if isinstance(scripts.get(hook), str):
                print(f"npm lifecycle {hook}: {scripts[hook]}", file=sys.stderr)
        found("npm test", "package.json defines an exact test script")

# 4. Cargo
if (ws / "Cargo.toml").is_file():
    found("cargo test", "Cargo.toml defines a Cargo project")

# 5. GNU Make reads only the first existing default makefile.
for name in ("GNUmakefile", "makefile", "Makefile"):
    if not (ws / name).is_file():
        continue
    text = read(name)
    if text is not None and re.search(r"(?m)^test\s*(?::|::)", text):
        found("make test", f"{name} defines a test target")
    break

print("No test runner detected", file=sys.stderr)
sys.exit(1)
PY
