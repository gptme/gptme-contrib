"""The suppression diff guard: a dismissal is about code, not about a claim.

`filter_suppressed` honoured every rejection forever, so a rework of the exact
lines a finding was written against still could not re-raise it. The guard is
deliberately narrow: it only fires for entries recorded at the head the
(incremental) diff starts from, and only when those old-side lines changed.
Anything it cannot localise keeps suppressing, and a caller that passes no
`base_head` — every legacy caller — sees unchanged behaviour.
"""

from gptme_review.core import added_lines, filter_suppressed, suppression_entry

BASE = "aaaa1111"
NEW = "bbbb2222"

DIFF = """\
diff --git a/counter.py b/counter.py
--- a/counter.py
+++ b/counter.py
@@ -1,6 +1,6 @@
 import sys
-x = 0
+x = 1
 for i in range(3):
-    x += 2
+    x += 1
 print(x)
 return x
diff --git a/other.py b/other.py
--- a/other.py
+++ b/other.py
@@ -10,2 +10,3 @@ def f():
 keep = 1
+added = 2
 tail = 3
"""


def _finding(line: int = 4, file: str = "counter.py") -> dict:
    return {
        "file": file,
        "line": line,
        "severity": "P1",
        "class": "bug",
        "body": "The counter is incremented twice, so callers index past the intended value.",
    }


def _entry(finding: dict, *, head: str | None = BASE, why: str = "dismissed") -> dict:
    entry = suppression_entry(finding, why=why)
    if head is not None:
        entry["head"] = head
    return entry


def test_added_lines_keeps_only_plus_side_in_new_coordinates() -> None:
    touched = added_lines(DIFF)

    # counter.py hunk starts at new line 1: context stays at 1, the rewritten
    # lines land at 2 and 4. Deleted and context lines are not "added".
    assert touched["counter.py"] == {2, 4}
    assert touched["other.py"] == {11}


def test_rewritten_lines_re_enable_the_finding() -> None:
    finding = _finding(line=4)

    kept, muted = filter_suppressed([finding], [_entry(finding)], DIFF, base_head=BASE)

    assert kept == [finding]
    assert muted == []


def test_untouched_line_keeps_suppressing() -> None:
    finding = _finding(line=99)

    kept, muted = filter_suppressed([finding], [_entry(finding)], DIFF, base_head=BASE)

    assert kept == []
    assert muted == [finding]


def test_entry_recorded_at_another_head_keeps_suppressing() -> None:
    finding = _finding(line=4)

    kept, muted = filter_suppressed(
        [finding], [_entry(finding, head="cccc3333")], DIFF, base_head=BASE
    )

    assert kept == []
    assert muted == [finding]


def test_no_base_head_is_unchanged_behaviour() -> None:
    finding = _finding(line=4)

    kept, muted = filter_suppressed([finding], [_entry(finding)], DIFF)

    assert kept == []
    assert muted == [finding]


def test_empty_diff_is_unchanged_behaviour() -> None:
    finding = _finding(line=4)

    kept, muted = filter_suppressed([finding], [_entry(finding)], "", base_head=BASE)

    assert kept == []
    assert muted == [finding]


def test_resolution_never_suppresses_regardless_of_the_diff() -> None:
    finding = _finding(line=99)

    kept, muted = filter_suppressed(
        [finding], [_entry(finding, why="resolved")], DIFF, base_head=BASE
    )

    assert kept == [finding]
    assert muted == []


def test_unanchored_entry_is_re_enabled_when_its_file_changed() -> None:
    entry = _entry(_finding())
    entry["line"] = None
    finding = _finding(line=4)

    kept, muted = filter_suppressed([finding], [entry], DIFF, base_head=BASE)

    assert kept == [finding]
    assert muted == []


def test_line_shift_alone_does_not_re_enable() -> None:
    """Matching a claim at a different line does not prove its anchor changed."""
    finding = _finding(line=4)
    entry = _entry(finding)
    entry["line"] = 3  # context, not a removed/replaced old-side line

    kept, muted = filter_suppressed([finding], [entry], DIFF, base_head=BASE)

    assert kept == []
    assert muted == [finding]


def test_insertion_before_rewrite_uses_the_dismissals_old_line() -> None:
    diff = """\
diff --git a/counter.py b/counter.py
--- a/counter.py
+++ b/counter.py
@@ -1 +1,2 @@
+inserted = 1
 first = 1
@@ -20 +21 @@
-x += 2
+x += 1
"""
    entry = _entry(_finding(line=20))
    finding = _finding(line=21)

    kept, muted = filter_suppressed([finding], [entry], diff, base_head=BASE)

    assert kept == [finding]
    assert muted == []


def test_quoted_paths_match_decoded_suppression_anchors() -> None:
    diff = r"""diff --git "a/\303\251.py" "b/\303\251.py"
--- "a/\303\251.py"
+++ "b/\303\251.py"
@@ -4 +4 @@
-old
+new
"""
    finding = _finding(file="é.py")

    assert added_lines(diff) == {"é.py": {4}}
    kept, muted = filter_suppressed([finding], [_entry(finding)], diff, base_head=BASE)
    assert kept == [finding]
    assert muted == []


def test_header_like_source_is_not_a_file_header() -> None:
    diff = """\
diff --git a/counter.py b/counter.py
--- a/counter.py
+++ b/counter.py
@@ -4,2 +4,2 @@
--- old
+++ new
-old tail
+new tail
"""
    assert added_lines(diff) == {"counter.py": {4, 5}}
    finding = _finding()
    kept, muted = filter_suppressed([finding], [_entry(finding)], diff, base_head=BASE)
    assert kept == [finding]
    assert muted == []


def test_insertion_at_old_anchor_does_not_count_as_rewrite() -> None:
    diff = """\
diff --git a/counter.py b/counter.py
--- a/counter.py
+++ b/counter.py
@@ -3,2 +3,3 @@
 keep
+inserted
 anchor
"""
    finding = _finding(line=5)
    entry = _entry(_finding(line=4))
    kept, muted = filter_suppressed([finding], [entry], diff, base_head=BASE)
    assert kept == []
    assert muted == [finding]

    entry["line"] = None
    kept, muted = filter_suppressed([finding], [entry], diff, base_head=BASE)
    assert kept == [finding]
    assert muted == []


def test_deletion_only_re_enables_the_old_anchor() -> None:
    diff = """\
diff --git a/counter.py b/counter.py
--- a/counter.py
+++ b/counter.py
@@ -4 +3,0 @@
-x += 2
"""
    finding = _finding()
    kept, muted = filter_suppressed([finding], [_entry(finding)], diff, base_head=BASE)
    assert kept == [finding]
    assert muted == []
