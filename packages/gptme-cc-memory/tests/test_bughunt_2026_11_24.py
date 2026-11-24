"""Regression tests for the 2026-11-24 gptme-cc-memory bug hunt.

CC* = correctness / edge-case input findings, CS* = security / error-handling /
data-loss findings. Overlapping pairs: CC1=CS9, CC2=CS4, CC4=CS3, CC6=CS2,
CC7=CS1, CC10=CS5. CS6/CS8 (cross-process races) are tracked separately.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from gptme_cc_memory import extractor
from gptme_cc_memory.extractor import (
    ExtractionResult,
    detect_confirmations,
    detect_corrections,
    extract_memories,
    format_pending_updates,
    read_trajectory,
)
from gptme_cc_memory.injector import inject_memories, prune_stale_pending_updates
from gptme_cc_memory.memory_retrieval import (
    load_memory_state,
    record_memory_injections,
    select_relevant_memories,
)
from gptme_cc_memory.schema import discover_memory_files, parse_memory_file

FEEDBACK_MD = """\
---
name: never-skip-precommit
description: Never bypass pre-commit hooks with --no-verify
metadata:
  type: feedback
---

Never use --no-verify to bypass pre-commit hooks.
"""

PROMPT = "Don't bypass pre-commit hooks with --no-verify"


def _write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# --- CC1 / CS9: real Claude Code transcript format ---------------------------


def test_cc1_real_claude_code_transcript_extracts_correction(tmp_path: Path):
    traj = tmp_path / "session.jsonl"
    _write_jsonl(
        traj,
        [
            {
                "type": "user",
                "sessionId": "s1",
                "uuid": "u1",
                "message": {
                    "role": "user",
                    "content": "Don't use --no-verify, fix the pre-commit failure instead",
                },
            },
            {
                "type": "assistant",
                "sessionId": "s1",
                "uuid": "a1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Fixing the hook."},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                    ],
                },
            },
        ],
    )
    result = extract_memories(traj)
    assert len(result.corrections) >= 1, result
    assert result.session_context.get("goal"), result.session_context
    assert result.session_context["last_turn"] == "Fixing the hook."
    assert result.session_context["tool_call_count"] == 1


def test_cs9_real_claude_code_transcript_yields_nothing(tmp_path: Path):
    traj = tmp_path / "cc.jsonl"
    _write_jsonl(
        traj,
        [
            {
                "type": "user",
                "sessionId": "s",
                "message": {"role": "user", "content": "Never use --no-verify to skip the hooks"},
            },
            {
                "type": "assistant",
                "sessionId": "s",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Understood, I will fix the hook."}],
                },
            },
        ],
    )
    result = extract_memories(traj)
    assert len(result.corrections) == 1
    assert result.session_context.get("goal")


def test_cc1_tool_result_only_user_line_is_not_a_human_message(tmp_path: Path):
    traj = tmp_path / "cc.jsonl"
    _write_jsonl(
        traj,
        [
            {"type": "summary", "summary": "x"},
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": "Fix the build"}]},
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "error: never use the deprecated flag",
                        }
                    ],
                },
            },
        ],
    )
    msgs = read_trajectory(traj)
    assert msgs == [{"role": "human", "content": "Fix the build"}]
    assert extract_memories(traj).corrections == []


# --- CC2 / CS4: missing / null / non-str content -----------------------------


def test_cc2_human_message_without_content_does_not_crash(tmp_path: Path):
    traj = tmp_path / "t.jsonl"
    _write_jsonl(
        traj,
        [
            {"role": "human"},
            {"role": "human", "content": "Don't use --no-verify please"},
        ],
    )
    result = extract_memories(traj)
    assert len(result.corrections) == 1


def test_cc2_null_content_does_not_crash(tmp_path: Path):
    traj = tmp_path / "t.jsonl"
    _write_jsonl(traj, [{"role": "human", "content": None}, {"role": "assistant", "content": None}])
    extract_memories(traj)


@pytest.mark.parametrize(
    "first",
    [{"role": "human"}, {"role": "human", "content": None}, {"role": "human", "content": {"a": 1}}],
    ids=["no-content", "null-content", "dict-content"],
)
def test_cs4_one_malformed_message_aborts_whole_extraction(tmp_path: Path, first: dict):
    traj = tmp_path / "t.jsonl"
    _write_jsonl(
        traj,
        [first, {"role": "human", "content": "Never use --no-verify to skip the hooks"}],
    )
    result = extract_memories(traj)
    assert len(result.corrections) == 1


# --- CC3: negative feedback recorded as positive confirmation ----------------


def test_cc3_not_right_is_not_a_positive_confirmation():
    msgs = [{"role": "human", "content": "No, that's not right."}]
    assert detect_corrections(msgs), "sanity: it is detected as a correction"
    confirmations = detect_confirmations(msgs)
    assert confirmations == [], confirmations


# --- CC4 / CS3: stale Confirmations/Guidance survive pruning -----------------


def test_cc4_stale_block_confirmations_and_guidance_are_pruned():
    r = ExtractionResult(
        corrections=[
            {"type": "restrictive_directive", "matched": "don't use", "context": "Don't use X"}
        ],
        confirmations=[{"type": "positive_confirmation", "full_text": "OLD-CONFIRMATION"}],
        guidance_blocks=["OLD-GUIDANCE always use Y for everything"],
    )
    content = format_pending_updates(r)
    future = date.today() + timedelta(days=30)
    pruned = prune_stale_pending_updates(content, future)
    assert "Don't use X" not in pruned
    assert "OLD-CONFIRMATION" not in pruned, pruned
    assert "OLD-GUIDANCE" not in pruned, pruned


def _extractor_block(days_ago: int, *, corrections: bool = True) -> str:
    res = ExtractionResult(
        corrections=(
            [{"type": "strong_directive", "matched": "never use", "context": "never use X"}]
            if corrections
            else []
        ),
        confirmations=[{"type": "positive_confirmation", "full_text": f"STALE-CONFIRM-{days_ago}"}],
        guidance_blocks=[f"always run STALE-GUIDANCE-{days_ago} please"],
    )
    text = format_pending_updates(res)
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=days_ago)).isoformat()
    return text.replace(today, old)


@pytest.mark.parametrize("corrections", [True, False], ids=["with-corrections", "no-corrections"])
def test_cs3_stale_confirmations_and_guidance_never_pruned(corrections: bool):
    content = _extractor_block(30, corrections=corrections)
    pruned = prune_stale_pending_updates(content, date.today())
    assert "STALE-CONFIRM-30" not in pruned
    assert "STALE-GUIDANCE-30" not in pruned


def test_cs3_fresh_block_after_stale_block_is_kept():
    content = _extractor_block(30) + "\n\n" + _extractor_block(0)
    pruned = prune_stale_pending_updates(content, date.today())
    assert "STALE-CONFIRM-30" not in pruned
    assert "STALE-CONFIRM-0" in pruned
    assert "STALE-GUIDANCE-0" in pruned


def test_cs3_stale_backlog_crowds_out_durable_memory(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "feedback-precommit.md").write_text(FEEDBACK_MD, encoding="utf-8")
    pending = mem / "pending-updates.md"
    pending.write_text(
        "\n\n".join(_extractor_block(10 + i) for i in range(60)) + "\n", encoding="utf-8"
    )
    result = inject_memories(
        PROMPT,
        memory_dir=mem,
        metadata_file=tmp_path / "metadata.json",
        pending_updates_file=pending,
    )
    assert result is not None and "never-skip-precommit" in result


# --- CC5: multi-paragraph correction context leaks past pruning --------------


def test_cc5_multiline_correction_context_is_fully_pruned():
    r = ExtractionResult(
        corrections=[
            {
                "type": "restrictive_directive",
                "matched": "Don't use",
                "context": "Don't use mocks here.\n\nLEAKED-SECOND-PARAGRAPH",
            }
        ]
    )
    content = format_pending_updates(r)
    pruned = prune_stale_pending_updates(content, date.today() + timedelta(days=30))
    assert "LEAKED-SECOND-PARAGRAPH" not in pruned, pruned


# --- CC6 / CS2: truncation after side effects --------------------------------


def _big_pending(mem: Path) -> Path:
    pending = mem / "pending-updates.md"
    pending.write_text("\n".join(f"- free-form note line {i:03d} " + "x" * 40 for i in range(70)))
    return pending


def test_cc6_truncated_memory_is_not_recorded_as_injected(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "feedback-precommit.md").write_text(FEEDBACK_MD)
    meta = tmp_path / "state" / "metadata.json"
    pending = _big_pending(mem)

    result = inject_memories(
        PROMPT,
        memory_dir=mem,
        metadata_file=meta,
        pending_updates_file=pending,
    )
    assert result is not None
    assert len(result) <= 4000
    delivered = "never-skip-precommit" in result
    recorded = "never-skip-precommit" in load_memory_state(meta)
    assert delivered == recorded, (delivered, recorded)
    if delivered:
        assert result.count("<memory_relevant_entries>") == result.count(
            "</memory_relevant_entries>"
        )


def test_cc6_truncated_session_context_is_not_cleared(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    meta = tmp_path / "state" / "metadata.json"
    pending = _big_pending(mem)
    ctx = mem / "pending-session-context.md"
    ctx.write_text("## Previous Session\n**Goal**: UNIQUE-GOAL-TEXT")

    result = inject_memories(
        "hello world prompt",
        memory_dir=mem,
        metadata_file=meta,
        pending_updates_file=pending,
        pending_session_context_file=ctx,
    )
    assert result is not None
    assert len(result) <= 4000
    if "UNIQUE-GOAL-TEXT" not in result:
        assert ctx.read_text() != "", "session context cleared without being delivered"


def test_cs2_one_shot_session_context_cleared_without_delivery(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    today = date.today().isoformat()
    pending = mem / "pending-updates.md"
    pending.write_text(
        f"## Pending — {today} 10:00 (corrections)\n" + ('- **x**: "filler line"\n' * 250),
        encoding="utf-8",
    )
    ctx_file = mem / "pending-session-context.md"
    ctx_file.write_text("## Previous Session\n**Goal**: finish the DB migration", encoding="utf-8")

    result = inject_memories(
        "hello world prompt",
        memory_dir=mem,
        metadata_file=tmp_path / "metadata.json",
        pending_updates_file=pending,
        pending_session_context_file=ctx_file,
    )
    delivered = result is not None and "finish the DB migration" in result
    still_on_disk = "finish the DB migration" in ctx_file.read_text(encoding="utf-8")
    assert delivered or still_on_disk, "session context was neither injected nor kept"


def test_cs2_oversized_guidance_truncated_then_cleared(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    guidance = mem / "guidance.md"
    body = "\n".join(f"rule {i}: something to remember" for i in range(300)) + "\nLAST-RULE"
    guidance.write_text(body, encoding="utf-8")

    result = inject_memories(
        "hello world prompt",
        memory_dir=mem,
        metadata_file=tmp_path / "metadata.json",
        guidance_file=guidance,
    )
    delivered = result is not None and "LAST-RULE" in result
    still_on_disk = "LAST-RULE" in guidance.read_text(encoding="utf-8")
    assert delivered or still_on_disk, "tail of one-shot guidance silently destroyed"


# --- CC7 / CS1: one malformed memory file kills all injection ----------------


def test_cc7_null_metadata_does_not_break_discovery(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "good.md").write_text(FEEDBACK_MD)
    (mem / "bad.md").write_text("---\nname: bad\ndescription: d\nmetadata:\n---\n\nbody\n")
    entries = discover_memory_files(mem)
    assert "never-skip-precommit" in [e.name for e in entries]


def test_cc7_string_aliases_does_not_break_discovery(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "good.md").write_text(FEEDBACK_MD)
    (mem / "bad.md").write_text(
        "---\nname: bad\ndescription: d\naliases: type hints\nmetadata:\n  type: user\n---\n\nbody\n"
    )
    entries = discover_memory_files(mem)
    assert "never-skip-precommit" in [e.name for e in entries]
    bad = next(e for e in entries if e.name == "bad")
    assert "type hints" in bad.aliases


def test_cc7_non_utf8_memory_file_does_not_break_discovery(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "good.md").write_text(FEEDBACK_MD)
    (mem / "latin1.md").write_bytes(
        "---\nname: caf\xe9\ndescription: d\nmetadata:\n  type: user\n---\n\nnaïve\n".encode(
            "latin-1"
        )
    )
    entries = discover_memory_files(mem)
    assert "never-skip-precommit" in [e.name for e in entries]


def test_cc7_bad_memory_file_suppresses_guidance_injection(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "bad.md").write_text("---\nname: bad\ndescription: d\nmetadata:\n---\n\nbody\n")
    guidance = mem / "guidance.md"
    guidance.write_text("GUIDANCE-TEXT")
    result = inject_memories(
        "some prompt about things",
        memory_dir=mem,
        metadata_file=tmp_path / "meta.json",
        guidance_file=guidance,
    )
    assert result is not None and "GUIDANCE-TEXT" in result, result


@pytest.mark.parametrize(
    "bad_bytes",
    [
        b"---\nname: x\ndescription: d\nmetadata: just-a-string\n---\n\nbody\n",
        b"---\nname: x\ndescription: d\nmetadata:\n  type: user\naliases: one-alias\n---\n\nbody\n",
        b"---\nname: x\ndescription: d\nmetadata:\n  type: user\naliases: [42]\n---\n\nbody\n",
        b"---\nname: caf\xe9\ndescription: d\nmetadata:\n  type: user\n---\n\nbody\n",
    ],
    ids=["metadata-scalar", "aliases-string", "aliases-int", "latin1-bytes"],
)
def test_cs1_one_malformed_memory_file_disables_all_injection(tmp_path: Path, bad_bytes: bytes):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "feedback-precommit.md").write_text(FEEDBACK_MD, encoding="utf-8")
    (mem / "zz-bad.md").write_bytes(bad_bytes)
    guidance = mem / "guidance.md"
    guidance.write_text("Check tests first.", encoding="utf-8")

    result = inject_memories(
        PROMPT,
        memory_dir=mem,
        metadata_file=tmp_path / "metadata.json",
        guidance_file=guidance,
    )
    assert result is not None and "never-skip-precommit" in result


# --- CC8: UTF-8 BOM ----------------------------------------------------------


def test_cc8_bom_prefixed_memory_file_is_parsed(tmp_path: Path):
    p = tmp_path / "bom.md"
    p.write_bytes(b"\xef\xbb\xbf" + FEEDBACK_MD.encode("utf-8"))
    entry = parse_memory_file(p)
    assert entry is not None
    assert entry.name == "never-skip-precommit"


# --- CC9: alias substring inside other words ---------------------------------


def test_cc9_alias_substring_inside_other_words_does_not_match(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "go-test.md").write_text(
        "---\nname: go test\ndescription: Golang notes\nmetadata:\n  type: project\n---\n\n"
        "Run with -race and -count=1 for golang packages.\n"
    )
    hits = select_relevant_memories(
        "please run cargo tests for the rust crate",
        memory_dir=mem,
        state_file=tmp_path / "meta.json",
    )
    assert hits == [], hits


def test_cc9_alias_whole_phrase_still_matches(tmp_path: Path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "go-test.md").write_text(
        "---\nname: go test\ndescription: Golang notes\nmetadata:\n  type: project\n---\n\n"
        "Run with -race and -count=1 for golang packages.\n"
    )
    hits = select_relevant_memories(
        "how should I go test this package",
        memory_dir=mem,
        state_file=tmp_path / "meta.json",
    )
    assert [h["name"] for h in hits] == ["go test"]


# --- CC10 / CS5: invalid UTF-8 byte aborts extraction ------------------------


def test_cc10_invalid_utf8_line_is_skipped_not_fatal(tmp_path: Path):
    traj = tmp_path / "t.jsonl"
    good = json.dumps({"role": "human", "content": "Don't use --no-verify please"}).encode()
    traj.write_bytes(good + b"\n" + b'{"role": "human", "content": "trunc\xe2\x82' + b"\n")
    msgs = read_trajectory(traj)
    assert len(msgs) == 1


def test_cs5_single_invalid_utf8_byte_crashes_extractor(tmp_path: Path):
    traj = tmp_path / "t.jsonl"
    good = json.dumps({"role": "human", "content": "Never use --no-verify to skip the hooks"})
    traj.write_bytes(
        good.encode() + b"\n" + b'{"role": "assistant", "content": "trunc\xe2\x82' + b"\n"
    )
    result = extract_memories(traj)
    assert len(result.corrections) == 1


# --- CC11: typographic apostrophe --------------------------------------------


def test_cc11_curly_apostrophe_dont_is_detected():
    msgs = [{"role": "human", "content": "Don’t use mocks in the integration tests"}]
    assert detect_corrections(msgs), "Don’t use ... not detected"


# --- CS7: non-atomic writes / corrupt state wiped -----------------------------
#
# Adapted from the reviewer's test: the failure is injected on *any* write-mode
# open inside the target's directory (simulating ENOSPC on that filesystem), so
# it also covers a temp-file-then-rename write path.


class _FailingWriter:
    def __init__(self, f):
        self._f = f

    def write(self, _data):
        raise OSError(28, "No space left on device")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False


def _fail_writes_in_dir_of(monkeypatch, target: Path):
    real_open = pathlib.Path.open

    def fake_open(self, mode="r", *a, **kw):
        f = real_open(self, mode, *a, **kw)
        if self.parent == target.parent and "w" in mode:
            return _FailingWriter(f)
        return f

    monkeypatch.setattr(pathlib.Path, "open", fake_open)


def test_cs7_failed_state_save_truncates_metadata(tmp_path: Path, monkeypatch):
    state_file = tmp_path / "metadata.json"
    state_file.write_text(json.dumps({"old": {"confidence": 0.99, "references": 12}}))
    _fail_writes_in_dir_of(monkeypatch, state_file)
    with pytest.raises(OSError):
        record_memory_injections(["new"], state_file=state_file)
    monkeypatch.undo()
    assert load_memory_state(state_file).get("old", {}).get("references") == 12
    assert sorted(p.name for p in tmp_path.iterdir()) == ["metadata.json"], "temp file leaked"


def test_cs7_corrupt_state_silently_wiped_on_next_save(tmp_path: Path):
    state_file = tmp_path / "metadata.json"
    torn = '{"old": {"confidence": 0.99, "references": 12}'
    state_file.write_text(torn)
    record_memory_injections(["new"], state_file=state_file)
    # Unparseable state must not be silently destroyed: the old bytes are
    # moved aside before a fresh state file is started.
    backup = tmp_path / "metadata.json.corrupt"
    assert backup.read_text() == torn
    assert "new" in load_memory_state(state_file)


def test_cs7_second_corruption_keeps_the_first_backup(tmp_path: Path):
    """A second corrupt state must not replace the first backup's bytes."""
    state_file = tmp_path / "metadata.json"
    first = '{"old": {"confidence": 0.99'
    second = '{"older": {"references": 12'
    state_file.write_text(first)
    record_memory_injections(["one"], state_file=state_file)
    state_file.write_text(second)
    record_memory_injections(["two"], state_file=state_file)
    assert (tmp_path / "metadata.json.corrupt").read_text() == first
    assert (tmp_path / "metadata.json.corrupt.1").read_text() == second
    assert "two" in load_memory_state(state_file)


def test_cs7_failed_append_truncates_pending_updates(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    pending = workspace / "memory" / "pending-updates.md"
    pending.parent.mkdir(parents=True)
    pending.write_text("WEEKS-OF-ACCUMULATED-PENDING\n", encoding="utf-8")
    traj = tmp_path / "t.jsonl"
    _write_jsonl(traj, [{"role": "human", "content": "never use the force push flag"}])
    monkeypatch.setenv("GPTME_CC_MEMORY_DIR", str(workspace))
    monkeypatch.setattr(sys, "argv", ["x", str(traj)])
    _fail_writes_in_dir_of(monkeypatch, pending)
    with pytest.raises(OSError):
        extractor.main()
    monkeypatch.undo()
    assert "WEEKS-OF-ACCUMULATED-PENDING" in pending.read_text(encoding="utf-8")


@pytest.mark.parametrize("existing_mode", [None, 0o600, 0o640])
def test_atomic_write_does_not_expose_private_contents(tmp_path: Path, monkeypatch, existing_mode):
    import os
    import stat

    target = tmp_path / "pending.md"
    if existing_mode is not None:
        target.write_text("old")
        target.chmod(existing_mode)
    real_replace = os.replace

    def check_temp_mode(src, dst):
        assert stat.S_IMODE(Path(src).stat().st_mode) == (existing_mode or 0o600)
        real_replace(src, dst)

    real_open = os.open

    def check_create_mode(path, flags, mode=0o777):
        assert flags & os.O_EXCL
        assert mode == 0o600
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", check_create_mode)
    monkeypatch.setattr(os, "replace", check_temp_mode)
    old_umask = os.umask(0)
    try:
        extractor.atomic_write_text(target, "private session context")
    finally:
        os.umask(old_umask)
    assert target.read_text() == "private session context"
    assert stat.S_IMODE(target.stat().st_mode) == (existing_mode or 0o600)
