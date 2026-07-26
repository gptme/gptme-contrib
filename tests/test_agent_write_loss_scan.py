"""Tests for agent-write-loss-scan.py."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Make the scripts directory importable
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import importlib

aws = importlib.import_module("agent-write-loss-scan")

parse_conversation_jsonl = aws.parse_conversation_jsonl
classify_write = aws.classify_write
WriteEvent = aws.WriteEvent
_git_blob_sha = aws._git_blob_sha


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conv(tmp_path: Path, messages: list[dict]) -> Path:
    """Write a fake conversation.jsonl under tmp_path/<session>/."""
    sess = tmp_path / "session-abc123"
    sess.mkdir(parents=True, exist_ok=True)
    conv = sess / "conversation.jsonl"
    conv.write_text(
        "\n".join(json.dumps(m) for m in messages) + "\n",
        encoding="utf-8",
    )
    return conv


_NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its path (hooks disabled)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, check=True, capture_output=True,
    )
    # Disable global commit hooks so test repos can commit freely
    subprocess.run(
        ["git", "config", "core.hooksPath", "/dev/null"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


def _commit_file(repo: Path, rel: str, content: str) -> None:
    """Write a file and commit it."""
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    subprocess.run(["git", *_NO_HOOKS, "add", rel], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", *_NO_HOOKS, "commit", "-m", f"add {rel}"],
        cwd=repo, check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Test 1: parse_conversation_jsonl extracts save events correctly
# ---------------------------------------------------------------------------


def test_parse_conversation_jsonl_extracts_save_events(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    content_body = "hello world"
    messages = [
        {
            "role": "user",
            "content": "write hello.txt",
            "timestamp": "2025-01-01T00:00:00Z",
        },
        {
            "role": "assistant",
            "content": f"```save hello.txt\n{content_body}\n```",
            "timestamp": "2025-01-01T00:00:01Z",
        },
    ]
    conv = _make_conv(tmp_path / "logs", messages)
    start_ts, events = parse_conversation_jsonl(conv, repo)

    assert len(events) == 1
    ev = events[0]
    assert ev.tool == "save"
    assert ev.rel_path == "hello.txt"
    assert ev.written_blob == _git_blob_sha((content_body + "\n").encode())
    assert ev.session_id == "session-abc123"


def test_parse_conversation_jsonl_skips_non_assistant_messages(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    messages = [
        {
            "role": "user",
            "content": "```save secret.txt\ndo not capture\n```",
            "timestamp": "2025-01-01T00:00:00Z",
        },
    ]
    conv = _make_conv(tmp_path / "logs", messages)
    _, events = parse_conversation_jsonl(conv, repo)
    assert events == []


def test_parse_conversation_jsonl_handles_append(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    messages = [
        {
            "role": "assistant",
            "content": "```append notes.txt\nextra line\n```",
            "timestamp": "2025-01-01T00:00:00Z",
        },
    ]
    conv = _make_conv(tmp_path / "logs", messages)
    _, events = parse_conversation_jsonl(conv, repo)
    assert len(events) == 1
    assert events[0].tool == "append"
    assert events[0].new_strings == ["extra line"]
    assert events[0].written_blob is None  # appends don't have a blob SHA


# ---------------------------------------------------------------------------
# Test 2: classify_write correctly identifies PERSISTED vs LOST
# ---------------------------------------------------------------------------


def test_classify_write_persisted(tmp_path: Path) -> None:
    """A save whose exact content was committed should be PERSISTED."""
    repo = _init_repo(tmp_path)
    content = "committed content"
    _commit_file(repo, "foo.txt", content + "\n")

    ev = WriteEvent(
        session_id="sess1",
        tool="save",
        rel_path="foo.txt",
        write_ts=0.0,  # very early, so post-write window covers the commit
        written_blob=_git_blob_sha((content + "\n").encode()),
    )
    result = classify_write(ev, repo)
    assert result.outcome == "PERSISTED"


def test_classify_write_lost(tmp_path: Path) -> None:
    """A save whose blob never appeared in git should be LOST (or SUPERSEDED)."""
    repo = _init_repo(tmp_path)
    # Commit a different version of the file
    _commit_file(repo, "bar.txt", "committed different content\n")

    ev = WriteEvent(
        session_id="sess2",
        tool="save",
        rel_path="bar.txt",
        write_ts=0.0,
        written_blob=_git_blob_sha(b"the write that was lost\n"),
    )
    result = classify_write(ev, repo)
    # The blob was never committed: SUPERSEDED (a later commit changed it)
    # or LOST (depends on ordering). Either way it should NOT be PERSISTED.
    assert result.outcome in ("LOST", "SUPERSEDED", "UNKNOWN")
    assert result.outcome != "PERSISTED"


def test_classify_write_unknown_path(tmp_path: Path) -> None:
    """Write to a path not in git at all should yield LOST or UNKNOWN."""
    repo = _init_repo(tmp_path)

    ev = WriteEvent(
        session_id="sess3",
        tool="save",
        rel_path="nonexistent/path.txt",
        write_ts=0.0,
        written_blob=_git_blob_sha(b"some content\n"),
    )
    result = classify_write(ev, repo)
    assert result.outcome in ("LOST", "UNKNOWN")


# ---------------------------------------------------------------------------
# Test 3: git_blob_sha matches git's own hash-object output
# ---------------------------------------------------------------------------


def test_git_blob_sha_matches_git(tmp_path: Path) -> None:
    """_git_blob_sha should produce the same SHA as `git hash-object`."""
    content = b"hello from the test\n"
    expected = subprocess.run(
        ["git", "hash-object", "--stdin"],
        input=content,
        capture_output=True,
        check=True,
    ).stdout.strip().decode()

    assert _git_blob_sha(content) == expected


# ---------------------------------------------------------------------------
# Test 4: CLI smoke test
# ---------------------------------------------------------------------------


def test_main_empty_logs(tmp_path: Path) -> None:
    """main() should exit 0 with an empty logs dir."""
    repo = _init_repo(tmp_path)
    logs_dir = tmp_path / "empty_logs"
    logs_dir.mkdir()

    ret = aws.main(["--repo", str(repo), "--logs-dir", str(logs_dir)])
    assert ret == 0


def test_main_json_output(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """main() with --json should emit valid JSON."""
    repo = _init_repo(tmp_path)
    logs_dir = tmp_path / "empty_logs"
    logs_dir.mkdir()

    aws.main(["--repo", str(repo), "--logs-dir", str(logs_dir), "--json"])
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "total_writes" in data
    assert "loss_rate" in data
    assert data["total_writes"] == 0
