"""Tests for the action-receipts pre-tool hook."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from gptme_action_receipts.hooks.receipt_hook import _make_receipt, _receipt_pre


@dataclass
class _MockToolUse:
    tool: str
    content: str
    args: list[str] = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)


@dataclass
class _MockPreData:
    tool_use: _MockToolUse | None = None
    workspace: Path | None = None
    log: object = None


class TestMakeReceipt:
    def test_fields_present(self):
        r = _make_receipt("shell", "ls -la", Path("/workspace"), "ses-001")
        assert r["action_type"] == "shell"
        assert r["target"] == "ls -la"
        assert r["workspace"] == "/workspace"
        assert r["session_id"] == "ses-001"
        assert r["timestamp"]
        assert r["receipt_hash"].startswith("sha256:")

    def test_hash_is_deterministic(self):
        timestamp = "2026-07-04T18:00:00+00:00"
        r1 = _make_receipt("shell", "ls", None, "s1", timestamp=timestamp)
        r2 = _make_receipt("shell", "ls", None, "s1", timestamp=timestamp)
        # Hashes must be identical for the same inputs.
        assert r1["receipt_hash"] == r2["receipt_hash"]

    def test_hash_changes_on_different_target(self):
        r1 = _make_receipt("shell", "ls", None, "s1")
        r2 = _make_receipt("shell", "rm -rf /", None, "s1")
        assert r1["receipt_hash"] != r2["receipt_hash"]

    def test_long_target_truncated_by_caller(self):
        long_cmd = "x" * 600
        r = _make_receipt("shell", long_cmd[:512], None, "s1")
        assert len(r["target"]) == 512

    def test_no_workspace(self):
        r = _make_receipt("save", "file.py", None, "s1")
        assert r["workspace"] is None

    def test_prefers_gptme_model_env(self, monkeypatch):
        monkeypatch.setenv("GPTME_MODEL", "gptme-model")
        monkeypatch.setenv("CC_MODEL", "claude-code-model")

        r = _make_receipt("shell", "ls", None, "s1")

        assert r["model"] == "gptme-model"


class TestReceiptPreHook:
    def test_writes_receipt_to_ledger(self, tmp_path, monkeypatch):
        ledger = tmp_path / "receipts.jsonl"
        monkeypatch.setenv("GPTME_RECEIPTS_LEDGER", str(ledger))

        data = _MockPreData(
            tool_use=_MockToolUse(tool="shell", content="echo hello"),
            workspace=tmp_path,
        )
        list(_receipt_pre(data))  # exhaust generator

        assert ledger.exists()
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 1
        receipt = json.loads(lines[0])
        assert receipt["action_type"] == "shell"
        assert receipt["target"] == "echo hello"

    def test_no_crash_on_unwritable_ledger(self, tmp_path, monkeypatch):
        unwritable = tmp_path / "no_perms" / "receipts.jsonl"
        # Parent dir doesn't exist and we make it unwritable.
        (tmp_path / "no_perms").mkdir()
        (tmp_path / "no_perms").chmod(0o444)
        monkeypatch.setenv("GPTME_RECEIPTS_LEDGER", str(unwritable))

        data = _MockPreData(
            tool_use=_MockToolUse(tool="shell", content="ls"),
        )
        # Must not raise — the hook falls back to a warning.
        list(_receipt_pre(data))

    def test_noop_when_tool_use_is_none(self, tmp_path, monkeypatch):
        ledger = tmp_path / "receipts.jsonl"
        monkeypatch.setenv("GPTME_RECEIPTS_LEDGER", str(ledger))

        data = _MockPreData(tool_use=None)
        list(_receipt_pre(data))

        assert not ledger.exists()

    def test_multiple_tool_calls_append(self, tmp_path, monkeypatch):
        ledger = tmp_path / "receipts.jsonl"
        monkeypatch.setenv("GPTME_RECEIPTS_LEDGER", str(ledger))

        for cmd in ("ls", "pwd", "echo hi"):
            data = _MockPreData(
                tool_use=_MockToolUse(tool="shell", content=cmd),
            )
            list(_receipt_pre(data))

        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 3
        targets = [json.loads(line)["target"] for line in lines]
        assert targets == ["ls", "pwd", "echo hi"]

    def test_content_truncated_to_512_chars(self, tmp_path, monkeypatch):
        ledger = tmp_path / "receipts.jsonl"
        monkeypatch.setenv("GPTME_RECEIPTS_LEDGER", str(ledger))

        long_cmd = "a" * 1000
        data = _MockPreData(
            tool_use=_MockToolUse(tool="shell", content=long_cmd),
        )
        list(_receipt_pre(data))

        receipt = json.loads(ledger.read_text().strip())
        assert len(receipt["target"]) == 512
