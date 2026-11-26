"""Regression tests for lesson adoption ledger safety (#2362 E5)."""

import builtins
import json
import os
import sys
import tempfile
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


def _network_lesson(tmp_path: Path) -> tuple[dict, Path]:
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
    return meta, net


def test_failed_lesson_write_keeps_existing_lesson(tmp_path, monkeypatch):
    """A failed lesson write must not truncate the lesson under --force."""
    meta, net = _network_lesson(tmp_path)
    lessons = tmp_path / "ws" / "lessons"
    local = lessons / "tools" / "git.md"
    local.parent.mkdir(parents=True)
    local.write_text("ORIGINAL LESSON\n")

    real_open, real_fdopen = builtins.open, os.fdopen

    class _FailingWrite:
        """Handle whose body write raises ENOSPC (after any truncation)."""

        def __init__(self, f):
            self._f = f

        def write(self, _s):
            raise OSError(28, "No space left on device")

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self._f.close()
            return False

    def failing_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if "w" in mode and "git.md" in str(file):
            return _FailingWrite(f)
        return f

    real_mkstemp = tempfile.mkstemp
    lesson_fds: set[int] = set()

    def recording_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        if "git.md" in str(name):
            lesson_fds.add(fd)
        return fd, name

    def failing_fdopen(fd, mode="r", *args, **kwargs):
        f = real_fdopen(fd, mode, *args, **kwargs)
        if "w" in mode and fd in lesson_fds:
            return _FailingWrite(f)
        return f

    monkeypatch.setattr(adopt.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(builtins, "open", failing_open)
    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    ok, msg = adopt.adopt_lesson(meta, net, lessons, force=True)
    monkeypatch.undo()

    assert not ok, msg
    assert local.read_text() == "ORIGINAL LESSON\n"
    assert not list(local.parent.glob("*.tmp"))
    assert not _ledger(lessons).exists()


def test_symlinked_lesson_is_written_through(tmp_path):
    """A symlinked lesson must stay a symlink, as the in-place write did."""
    meta, net = _network_lesson(tmp_path)
    lessons = tmp_path / "ws" / "lessons"
    (lessons / "tools").mkdir(parents=True)
    # Target stays inside lessons_dir: adopt_lesson rejects symlinks that
    # escape it, so only an in-tree link can reach this path.
    shared = lessons / "shared-git.md"
    shared.write_text("SHARED LESSON\n")
    local = lessons / "tools" / "git.md"
    local.symlink_to(shared)

    ok, msg = adopt.adopt_lesson(meta, net, lessons, force=True)

    assert ok, msg
    assert local.is_symlink()
    assert shared.read_text() != "SHARED LESSON\n"
    assert not list(shared.parent.glob("*.tmp"))


def test_ledger_keeps_its_mode(tmp_path):
    """Rewriting the ledger must not narrow a shared ledger to owner-only."""
    lessons = tmp_path / "ws" / "lessons"
    lessons.mkdir(parents=True)
    adopt.record_adoption("a-tools-l0", "a", "2026-01-01T00:00:00", lessons)
    ledger = _ledger(lessons)
    ledger.chmod(0o644)

    adopt.record_adoption("a-tools-l1", "a", "2026-01-02T00:00:00", lessons)

    assert ledger.stat().st_mode & 0o777 == 0o644
    assert len(json.loads(ledger.read_text())["adoptions"]) == 2


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
