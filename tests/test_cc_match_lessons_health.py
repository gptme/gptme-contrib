"""CLI health diagnostics must distinguish a broken matcher from no matches."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parents[1] / "scripts/claude-code-hooks/match-lessons.py"


@pytest.fixture
def cli(tmp_path):
    # Copy outside any real workspace so discovery cannot use the developer's brain.
    script = tmp_path / "match-lessons.py"
    shutil.copyfile(HOOK, script)
    (tmp_path / "gptme.toml").write_text('[lessons]\ndirs = ["lessons"]\n')
    (tmp_path / "lessons").mkdir()

    def run(*args, missing=False, event="UserPromptSubmit", prompt="unrelated words"):
        code = "import builtins, runpy, sys\n"
        if missing:
            code += (
                "original = builtins.__import__\n"
                "def guarded(name, *a, **kw):\n"
                "    if name == 'gptme_rag.lesson_matcher':\n"
                "        raise ModuleNotFoundError(\"No module named 'gptme_rag'\")\n"
                "    return original(name, *a, **kw)\n"
                "builtins.__import__ = guarded\n"
            )
        code += "sys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n"
        env = {
            **os.environ,
            "TMPDIR": str(tmp_path),
            "MATCH_LESSONS_VENV_REEXEC": "1",
            "LESSON_DROPOUT_EPSILON": "0",
            "HOLDOUT_LESSONS": "",
            "MATCH_LESSONS_SELECTOR_CMD": "",
        }
        return subprocess.run(
            [sys.executable, "-c", code, str(script), *args],
            input=json.dumps(
                {
                    "hook_event_name": event,
                    "session_id": "health",
                    "prompt": prompt,
                    "tool_name": "Bash",
                    "tool_input": {"command": "true"},
                }
            ),
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=env,
            timeout=10,
        )

    return run


@pytest.mark.parametrize(
    "event,prompt",
    [
        ("UserPromptSubmit", "unrelated words"),
        ("PreToolUse", ""),
        ("UserPromptSubmit", "hello"),
    ],
)
def test_missing_dependency_is_not_no_match(cli, tmp_path, event, prompt):
    result = cli(missing=True, event=event, prompt=prompt)
    assert result.returncode == 0  # Hook stays fail-open.
    receipts = [
        json.loads(line)
        for line in (tmp_path / "cc-session-health-lessons-receipts.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(receipts) == 1
    assert receipts[0]["reason"] == "dependency_missing"
    assert receipts[0]["bytes"] == 0
    assert result.stderr.count("Warning:") == 1
    assert "gptme_rag.lesson_matcher" in result.stderr


def test_self_check_missing_dependency(cli):
    result = cli("--self-check", missing=True)
    assert result.returncode != 0
    assert "dependency_missing" in result.stderr


def test_self_check_installed(cli):
    result = cli("--self-check")
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout.lower()


def test_self_check_missing_directories(cli, tmp_path):
    (tmp_path / "lessons").rmdir()
    result = cli("--self-check")
    assert result.returncode != 0
    assert "lesson" in result.stderr.lower()


def test_installed_no_match_unchanged(cli, tmp_path):
    result = cli()
    assert result.returncode == 0, result.stderr
    receipt = json.loads(
        (tmp_path / "cc-session-health-lessons-receipts.jsonl").read_text()
    )
    assert receipt["reason"] == "no_match"
    assert not result.stderr


def test_dependency_actually_absent_without_site_packages(tmp_path):
    script = tmp_path / "match-lessons.py"
    shutil.copyfile(HOOK, script)
    env = {**os.environ, "TMPDIR": str(tmp_path), "MATCH_LESSONS_VENV_REEXEC": "1"}
    result = subprocess.run(
        [sys.executable, "-S", str(script)],
        input=json.dumps({"session_id": "absent", "prompt": "merge conflict"}),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    receipt = json.loads(
        (tmp_path / "cc-session-absent-lessons-receipts.jsonl").read_text()
    )
    assert receipt["reason"] == "dependency_missing"
    assert "dependency_missing" in result.stderr
    check = subprocess.run(
        [sys.executable, "-S", str(script), "--self-check"],
        stdin=subprocess.DEVNULL,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert check.returncode == 1


def test_self_check_never_reads_stdin(tmp_path):
    script = tmp_path / "match-lessons.py"
    shutil.copyfile(HOOK, script)
    (tmp_path / "gptme.toml").write_text('[lessons]\ndirs = ["lessons"]\n')
    (tmp_path / "lessons").mkdir()
    with subprocess.Popen(
        [sys.executable, str(script), "--self-check"],
        cwd=tmp_path,
        env={**os.environ, "MATCH_LESSONS_VENV_REEXEC": "1"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            # Keep stdin open: a self-check that calls read() would hang here.
            assert process.wait(timeout=5) == 0
        finally:
            process.kill()
            process.communicate()


def test_installed_matching_still_injects(cli, tmp_path):
    (tmp_path / "lessons/sample.md").write_text(
        '---\nmatch:\n  keywords: ["merge conflict"]\nstatus: active\n---\n'
        "# Merge lesson\n\n## Rule\nPreserve both changes.\n"
    )
    result = cli(prompt="merge conflict")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "Preserve both changes" in payload["hookSpecificOutput"]["additionalContext"]
    receipt = json.loads(
        (tmp_path / "cc-session-health-lessons-receipts.jsonl").read_text()
    )
    assert receipt["outcome"] == "emitted"
    assert receipt["bytes"] > 0


def test_missing_lesson_dependency_preserves_memory_injection(cli, tmp_path):
    # Provide the independent memory API without depending on a particular gptme
    # installation. The lesson import still genuinely takes its fallback path.
    memory_package = tmp_path / "gptme" / "memory"
    memory_package.mkdir(parents=True)
    (memory_package.parent / "__init__.py").write_text("")
    (memory_package / "__init__.py").write_text(
        "class MemoryStore:\n"
        "    @classmethod\n"
        "    def from_workspace(cls, workspace, *, env):\n"
        "        return workspace\n"
    )
    (memory_package / "match.py").write_text(
        "from types import SimpleNamespace\n"
        "def match_memories(store, text, *, limit):\n"
        "    if 'memory signal' not in text:\n"
        "        return []\n"
        "    entry = SimpleNamespace(path=store / 'memory.md',\n"
        "        index_title='Independent memory', description='memory signal',\n"
        "        body='Preserve the independent memory source.')\n"
        "    return [SimpleNamespace(entry=entry, score=10, matched_by=['keyword'])]\n"
    )
    result = cli(missing=True, prompt="memory signal")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "Preserve the independent memory source" in payload[
        "hookSpecificOutput"
    ].get("additionalContext", "")
    receipt = json.loads(
        (tmp_path / "cc-session-health-lessons-receipts.jsonl").read_text()
    )
    assert receipt["outcome"] == "emitted"
    assert receipt["bytes"] > 0
    assert receipt["reason"] == "dependency_missing"
    assert result.stderr.count("Warning:") == 1
