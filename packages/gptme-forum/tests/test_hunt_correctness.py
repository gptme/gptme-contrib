"""Bug-hunt: correctness / edge-case input defects in gptme-forum.

Every test here is expected to FAIL on current master; each documents one defect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner
from gptme_forum.cli import cli
from gptme_forum.forum import Comment, Forum, Post


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _old_post(
    forum: Forum, project: str = "gptme", slug: str = "2026-01-01-old"
) -> Post:
    _write(
        forum.projects_dir / project / f"{slug}.md",
        "---\nauthor: alice\ndate: '2026-01-01T00:00:00+00:00'\ntitle: Old\n---\n\nOld post.\n",
    )
    p = forum.get_post(f"{project}/{slug}")
    assert p is not None
    return p


# C1 -------------------------------------------------------------------------
def test_mentions_since_includes_new_comment_on_old_post(tmp_path: Path):
    """A new comment mentioning @bob on an *old* post must be reported."""
    forum = Forum(tmp_path / "forum")
    post = _old_post(forum)
    Comment.create(post.comment_dir, author="alice", body="@bob please look", index=1)
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    results = forum.mentions_for("bob", since=since)
    assert [kind for _, kind in results] == ["comment"]


def test_unread_mentions_includes_new_comment_on_old_post(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    post = _old_post(forum)
    state = tmp_path / "state.txt"
    forum.unread_mentions("bob", state_file=state)  # first check sets the cursor
    Comment.create(post.comment_dir, author="alice", body="@bob ping", index=1)
    results = forum.unread_mentions("bob", state_file=state)
    assert len(results) == 1


# C2 -------------------------------------------------------------------------
def test_mentions_since_date_only_does_not_crash(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    _old_post(forum)
    r = CliRunner().invoke(
        cli, ["--forum-dir", str(tmp_path), "mentions", "-a", "bob", "-s", "2026-04-15"]
    )
    assert r.exception is None or isinstance(r.exception, SystemExit), r.exception
    assert r.exit_code == 0, r.output


def test_digest_since_naive_does_not_crash(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    _old_post(forum)
    r = CliRunner().invoke(
        cli,
        [
            "--forum-dir",
            str(tmp_path),
            "digest",
            "-a",
            "bob",
            "-s",
            "2026-04-15T10:00:00",
        ],
    )
    assert r.exception is None or isinstance(r.exception, SystemExit), r.exception
    assert r.exit_code == 0, r.output


def test_mentions_invalid_since_is_usage_error_not_traceback(tmp_path: Path):
    r = CliRunner().invoke(
        cli, ["--forum-dir", str(tmp_path), "mentions", "-a", "bob", "-s", "yesterday"]
    )
    # click usage errors exit with 2 and raise SystemExit, not ValueError
    assert isinstance(r.exception, SystemExit), repr(r.exception)


# C3 -------------------------------------------------------------------------
def test_date_only_frontmatter_is_parsed_not_replaced_with_now(tmp_path: Path):
    path = _write(
        tmp_path / "p.md", "---\nauthor: bob\ndate: 2026-04-15\ntitle: T\n---\n\nHi\n"
    )
    post = Post.from_file(path, "gptme")
    assert post.date.date().isoformat() == "2026-04-15"


def test_post_without_date_is_not_perpetually_unread(tmp_path: Path):
    """A hand-written post lacking `date` gets now() on *every* read, so it is
    reported as a new unread mention forever."""
    forum = Forum(tmp_path / "forum")
    _write(
        forum.projects_dir / "gptme" / "2026-01-01-nodate.md",
        "---\nauthor: alice\ntitle: T\n---\n\n@bob hi\n",
    )
    state = tmp_path / "state.txt"
    assert len(forum.unread_mentions("bob", state_file=state)) == 1
    assert forum.unread_mentions("bob", state_file=state) == []


# C4 -------------------------------------------------------------------------
def test_one_bad_date_string_does_not_break_whole_forum(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    _old_post(forum)
    _write(
        forum.projects_dir / "gptme" / "2026-02-02-bad.md",
        "---\nauthor: alice\ndate: last tuesday\ntitle: Bad\n---\n\nbody\n",
    )
    r = CliRunner().invoke(cli, ["--forum-dir", str(tmp_path), "post", "list"])
    assert r.exit_code == 0, repr(r.exception)
    assert "Old" in r.output


# C5 -------------------------------------------------------------------------
def test_scalar_tags_not_split_into_characters(tmp_path: Path):
    path = _write(
        tmp_path / "p.md", "---\nauthor: bob\ntitle: T\ntags: perf\n---\n\nx\n"
    )
    post = Post.from_file(path, "gptme")
    assert post.tags == ["perf"]


def test_numeric_tag_does_not_crash_post_list(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    _write(
        forum.projects_dir / "gptme" / "2026-01-01-x.md",
        "---\nauthor: bob\ndate: '2026-01-01T00:00:00+00:00'\ntitle: T\ntags: [v2, 2026]\n---\n\nx\n",
    )
    r = CliRunner().invoke(cli, ["--forum-dir", str(tmp_path), "post", "list"])
    assert r.exit_code == 0, repr(r.exception)


def test_null_author_does_not_crash_post_list(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    _write(
        forum.projects_dir / "gptme" / "2026-01-01-x.md",
        "---\nauthor:\ndate: '2026-01-01T00:00:00+00:00'\ntitle: T\n---\n\nx\n",
    )
    r = CliRunner().invoke(cli, ["--forum-dir", str(tmp_path), "post", "list"])
    assert r.exit_code == 0, repr(r.exception)


# C6 -------------------------------------------------------------------------
def test_capitalised_mention_matches_lowercased_agent(tmp_path: Path):
    """get_agent_name() lowercases identities, so '@Bob' must reach agent 'bob'."""
    forum = Forum(tmp_path / "forum")
    Post.create(forum.project_dir("gptme"), "gptme", "alice", "Hi", "Thanks @Bob!")
    assert len(forum.mentions_for("bob")) == 1


# C7 -------------------------------------------------------------------------
def test_project_name_cannot_escape_forum(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    CliRunner().invoke(
        cli,
        [
            "--forum-dir",
            str(repo),
            "post",
            "create",
            "../../escaped",
            "Title",
            "-b",
            "body",
            "-a",
            "bob",
        ],
    )
    assert not (repo / "escaped").exists(), "post written outside forum/projects/"


def test_author_with_slash_is_rejected_not_a_crash(tmp_path: Path):
    """An author like 'team/bob' must be refused cleanly, not FileNotFoundError."""
    import pytest

    forum = Forum(tmp_path / "forum")
    post = _old_post(forum)
    with pytest.raises(ValueError, match="invalid author"):
        Comment.create(post.comment_dir, author="team/bob", body="hi", index=1)
    r = CliRunner().invoke(
        cli,
        [
            "--forum-dir",
            str(tmp_path),
            "comment",
            "add",
            post.ref,
            "hi",
            "-a",
            "team/bob",
        ],
    )
    assert r.exit_code == 2 and "invalid author" in r.output, r.output


# C8 -------------------------------------------------------------------------
def test_editor_left_untouched_aborts_post_create(tmp_path: Path, monkeypatch):
    """Quitting $EDITOR without writing must abort, not publish '# Title'."""
    monkeypatch.setenv("EDITOR", "true")
    r = CliRunner().invoke(
        cli,
        ["--forum-dir", str(tmp_path), "post", "create", "gptme", "Draft", "-a", "bob"],
    )
    assert r.exit_code == 1, r.output
    assert not list((tmp_path / "forum" / "projects").rglob("*.md"))


def test_editor_left_untouched_aborts_msg_send(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EDITOR", "true")
    r = CliRunner().invoke(
        cli, ["--forum-dir", str(tmp_path), "msg", "send", "alice", "Subj", "-a", "bob"]
    )
    assert r.exit_code == 1, r.output
    assert not list(tmp_path.rglob("from-bob-to-alice*.md"))


# C9 -------------------------------------------------------------------------
def test_comment_order_beyond_99(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    post = _old_post(forum)
    for i in range(1, 102):
        Comment.create(post.comment_dir, author="bob", body=f"n{i}", index=i)
    bodies = [c.body for c in post.comments()]
    assert bodies == [f"n{i}" for i in range(1, 102)]


# C10 ------------------------------------------------------------------------
def test_post_read_converts_offset_dates_to_utc(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    _write(
        forum.projects_dir / "gptme" / "2026-04-15-tz.md",
        "---\nauthor: bob\ndate: '2026-04-15T12:00:00+02:00'\ntitle: T\n---\n\nx\n",
    )
    r = CliRunner().invoke(
        cli, ["--forum-dir", str(tmp_path), "post", "read", "gptme/2026-04-15-tz"]
    )
    assert r.exit_code == 0
    assert "2026-04-15 10:00 UTC" in r.output, r.output


# C11 ------------------------------------------------------------------------
def test_mentions_output_identifies_thread_of_comment(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    post = _old_post(forum)
    Comment.create(post.comment_dir, author="alice", body="@bob see", index=1)
    r = CliRunner().invoke(cli, ["--forum-dir", str(tmp_path), "mentions", "-a", "bob"])
    assert r.exit_code == 0
    assert post.ref in r.output, r.output


# C12 ------------------------------------------------------------------------
def test_empty_comment_file_does_not_crash_digest(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    post = _old_post(forum)
    _write(post.comment_dir / "comment-01-alice.md", "")
    r = CliRunner().invoke(cli, ["--forum-dir", str(tmp_path), "digest", "-a", "bob"])
    assert r.exit_code == 0, repr(r.exception)


# C13 ------------------------------------------------------------------------
def test_unread_mentions_sees_items_pulled_after_last_check(tmp_path: Path):
    """Git-native flow: alice writes at T, commits at session end; bob checks
    at T+1h (before the pull) and then pulls. Alice's mention is never reported
    because unread tracking compares the *authored* timestamp to bob's wall clock."""
    forum = Forum(tmp_path / "forum")
    state = tmp_path / "state.txt"
    forum.unread_mentions("bob", state_file=state)  # bob's check
    authored = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    pulled = _write(
        forum.projects_dir / "gptme" / "2026-01-01-pulled.md",
        f"---\nauthor: alice\ndate: '{authored}'\ntitle: T\n---\n\n@bob review pls\n",
    )  # arrives via git pull
    # The pull happens after the check; filesystem timestamps are coarser than
    # datetime.now(), so make "after" unambiguous instead of sub-millisecond.
    later = datetime.now(tz=timezone.utc).timestamp() + 1
    import os

    os.utime(pulled, (later, later))
    assert len(forum.unread_mentions("bob", state_file=state)) == 1
