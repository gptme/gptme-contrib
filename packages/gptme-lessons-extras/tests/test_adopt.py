"""Regression tests for lesson adoption ledger safety (#2362 E5)."""

import json
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "gptme_lessons_extras"
sys.path.insert(0, str(PKG))  # adopt.py falls back to `import review`

from gptme_lessons_extras import adopt  # noqa: E402


def _ledger(lessons: Path) -> Path:
    return lessons.parent / "knowledge" / "meta" / "lesson-adoptions.json"


def _dump_then_enospc(obj, fp, **kw):
    fp.write(json.dumps(obj, **kw)[:30])  # partial write, then disk full
    raise OSError(28, "No space left on device")


def test_failed_ledger_write_keeps_previous_adoptions(tmp_path, monkeypatch):
    lessons = tmp_path / "ws" / "lessons"
    lessons.mkdir(parents=True)
    for i in range(3):
        adopt.record_adoption(f"a-tools-l{i}", "a", "2026-01-01T00:00:00", lessons)

    monkeypatch.setattr(adopt.json, "dump", _dump_then_enospc)
    with pytest.raises(OSError):
        adopt.record_adoption("a-tools-l3", "a", "2026-01-02T00:00:00", lessons)
    monkeypatch.undo()

    data = json.loads(_ledger(lessons).read_text())
    assert len(data["adoptions"]) == 3
    assert not list(_ledger(lessons).parent.glob("*.tmp"))


def test_adoption_failing_on_ledger_is_retryable(tmp_path, monkeypatch):
    net = tmp_path / "network"
    (net / "alice" / "tools").mkdir(parents=True)
    (net / "alice" / "tools" / "git.md").write_text(
        "---\nnetwork:\n  lesson_id: alice-tools-git\n  agent_origin: alice\n"
        "  adoption_count: 0\nmatch:\n  keywords: [git]\n---\n# Git\nbody\n"
    )
    meta = {
        "network": {"lesson_id": "alice-tools-git", "agent_origin": "alice"},
        "_file": "alice/tools/git.md",
    }
    lessons = tmp_path / "ws" / "lessons"
    lessons.mkdir(parents=True)

    monkeypatch.setattr(adopt.json, "dump", _dump_then_enospc)
    ok, msg = adopt.adopt_lesson(meta, net, lessons)
    monkeypatch.undo()
    assert not ok, msg
    assert not (lessons / "tools" / "git.md").exists()

    ok, msg = adopt.adopt_lesson(meta, net, lessons)
    assert ok, msg
    assert (lessons / "tools" / "git.md").exists()
    assert len(json.loads(_ledger(lessons).read_text())["adoptions"]) == 1


def test_corrupt_ledger_is_set_aside_not_blocking(tmp_path, capsys):
    lessons = tmp_path / "ws" / "lessons"
    lessons.mkdir(parents=True)
    ledger = _ledger(lessons)
    ledger.parent.mkdir(parents=True)
    truncated = '{"adoptions": [{"lesson_id": "a-to'
    ledger.write_text(truncated)

    adopt.record_adoption("a-tools-new", "a", "2026-01-02T00:00:00", lessons)

    data = json.loads(ledger.read_text())
    assert [a["lesson_id"] for a in data["adoptions"]] == ["a-tools-new"]
    (backup,) = ledger.parent.glob(f"{ledger.name}.corrupt-*")
    assert backup.read_text() == truncated
    assert str(backup) in capsys.readouterr().err
