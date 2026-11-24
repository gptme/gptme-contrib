"""LessonDiscovery feature cache must not collide on file stem (#2362 LX7)."""

from pathlib import Path

from gptme_lessons_extras.discovery import LessonDiscovery


def test_same_stem_in_different_categories_gets_its_own_features(tmp_path: Path):
    a = tmp_path / "lessons" / "git" / "commit.md"
    b = tmp_path / "lessons" / "workflow" / "commit.md"
    for path, kw in ((a, "git"), (b, "standup")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nmatch:\n  keywords: [{kw}]\n---\n# T\n")
    d = LessonDiscovery(lessons_dir=tmp_path / "lessons")
    fa = d.extract_features(a)
    fb = d.extract_features(b)
    assert (fa.category, fa.keywords) == ("git", {"git"})
    assert (fb.category, fb.keywords) == ("workflow", {"standup"})
    assert d.extract_features(a) is fa  # still cached per file
