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


def test_symlink_in_another_category_keeps_its_own_features(tmp_path: Path):
    source = tmp_path / "git" / "commit.md"
    alias = tmp_path / "workflow" / "commit.md"
    source.parent.mkdir()
    alias.parent.mkdir()
    source.write_text("---\nmatch:\n  keywords: [git]\n---\n# T\n")
    alias.symlink_to(source)
    discovery = LessonDiscovery(lessons_dir=tmp_path)

    original = discovery.extract_features(source)
    linked = discovery.extract_features(alias)

    assert original.category == "git"
    assert linked.category == "workflow"
    assert linked.keywords == original.keywords == {"git"}
    assert discovery.extract_features(source) is original
    assert discovery.extract_features(alias) is linked


def test_duplicates_compares_every_file_pair_despite_shared_stems(tmp_path: Path):
    for relative in ("git/commit.md", "workflow/commit.md", "other/rebase.md"):
        path = tmp_path / relative
        path.parent.mkdir()
        path.write_text("---\nmatch:\n  keywords: [git]\n---\n# Identical lesson\n")
    discovery = LessonDiscovery(lessons_dir=tmp_path)

    duplicates = discovery.find_all_duplicates(threshold=0.95)

    # Three distinct files give three pairs, including both commit/rebase pairs.
    assert len(duplicates) == 3
    pairs = [tuple(sorted((a, b))) for a, b, _ in duplicates]
    assert pairs.count(("commit", "rebase")) == 2
    assert pairs.count(("commit", "commit")) == 1
