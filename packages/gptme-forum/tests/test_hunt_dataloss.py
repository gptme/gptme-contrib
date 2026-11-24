"""Bug hunt: concurrency/ordering and error-handling/data-loss defects.

Every test here FAILS on current code because of a real defect. Races are made
deterministic by monkeypatching an internal step so a second writer runs in
the window between the collision check and the write.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from gptme_forum import forum as forum_mod
from gptme_forum.cli import cli
from gptme_forum.forum import Comment, Forum, Post


def _forum(tmp_path: Path) -> Forum:
    f = Forum(tmp_path / "forum")
    f.ensure_exists()
    return f


def _write_post(path: Path, author: str, date: datetime, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nauthor: {author}\ndate: '{date.isoformat()}'\ntitle: {title}\n---\n\n{body}\n"
    )


# ---------------------------------------------------------------------------
# D1: Post.create check-then-write race overwrites a concurrent post
# ---------------------------------------------------------------------------


def test_d1_concurrent_post_same_title_is_not_overwritten(tmp_path, monkeypatch):
    forum = _forum(tmp_path)
    pdir = forum.project_dir("standups")
    real_render = forum_mod._render_frontmatter
    state = {"fired": False}

    def racing_render(meta, body):
        # Runs after the outer create's exists() loop, before its write.
        if not state["fired"]:
            state["fired"] = True
            Post.create(pdir, "standups", "alice", "Daily standup", "alice's standup")
        return real_render(meta, body)

    monkeypatch.setattr(forum_mod, "_render_frontmatter", racing_render)
    Post.create(pdir, "standups", "bob", "Daily standup", "bob's standup")

    bodies = sorted(p.body for p in forum.iter_posts("standups"))
    assert bodies == [
        "alice's standup",
        "bob's standup",
    ], f"concurrent post lost; files={sorted(x.name for x in pdir.glob('*.md'))}"


# ---------------------------------------------------------------------------
# D2: Comment.create check-then-write race overwrites a concurrent comment from
#     the same agent (the code comment claims this case is handled)
# ---------------------------------------------------------------------------


def test_d2_concurrent_comment_same_agent_is_not_overwritten(tmp_path, monkeypatch):
    forum = _forum(tmp_path)
    post = Post.create(forum.project_dir("gptme"), "gptme", "alice", "Thread", "hi")
    real_render = forum_mod._render_frontmatter
    state = {"fired": False}

    def racing_render(meta, body):
        if not state["fired"]:
            state["fired"] = True
            # Second session of the same agent computed the same index.
            Comment.create(post.comment_dir, "bob", "session A reply", index=1)
        return real_render(meta, body)

    monkeypatch.setattr(forum_mod, "_render_frontmatter", racing_render)
    Comment.create(post.comment_dir, "bob", "session B reply", index=1)

    bodies = sorted(c.body for c in post.comments())
    assert bodies == ["session A reply", "session B reply"]


# ---------------------------------------------------------------------------
# D3: msg send picks the filename BEFORE opening $EDITOR; a message sent by a
#     concurrent session while the editor is open is silently overwritten
# ---------------------------------------------------------------------------


def test_d3_msg_send_editor_window_overwrites_concurrent_message(tmp_path):
    fd = tmp_path / "repo"
    fd.mkdir()
    editor = tmp_path / "editor.sh"
    editor.write_text(
        "#!/bin/sh\n"
        # A concurrent session of bob sends alice a message meanwhile.
        f'"{sys.executable}" -m gptme_forum.cli --forum-dir "{fd}" '
        'msg send alice "other" "concurrent message" -a bob >/dev/null '
        f'&& touch "{tmp_path / "inner-ok"}"\n'
        'printf "edited message" > "$1"\n'
    )
    editor.chmod(0o755)
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["--forum-dir", str(fd), "msg", "send", "alice", "subj", "-a", "bob"],
        env={"EDITOR": str(editor)},
    )
    assert res.exit_code == 0, res.output
    assert (tmp_path / "inner-ok").exists(), "concurrent send did not run"
    texts = sorted(p.read_text() for p in (fd / "messages").rglob("*.md"))
    assert len(texts) == 2, texts
    assert any("concurrent message" in t for t in texts)


# ---------------------------------------------------------------------------
# D4: writes are not atomic -- a write that fails mid-way leaves an empty file
#     that is then read back as a ghost post/comment
# ---------------------------------------------------------------------------


def test_d4_failed_post_write_leaves_no_ghost_post(tmp_path):
    forum = _forum(tmp_path)
    pdir = forum.project_dir("gptme")
    # Lone surrogate (what Python yields for non-UTF-8 argv bytes) fails to encode
    # AFTER write_text has already created/truncated the target file.
    with pytest.raises(UnicodeEncodeError):
        Post.create(pdir, "gptme", "bob", "Broken", "bad byte \udcff")
    ghosts = list(forum.iter_posts("gptme"))
    assert (
        ghosts == []
    ), f"ghost post left behind: {[(g.path.name, g.body) for g in ghosts]}"


def test_d4_failed_comment_write_leaves_no_ghost_comment(tmp_path):
    forum = _forum(tmp_path)
    post = Post.create(forum.project_dir("gptme"), "gptme", "alice", "Thread", "hi")
    with pytest.raises(UnicodeEncodeError):
        Comment.create(post.comment_dir, "bob", "bad byte \udcff", index=1)
    assert post.comments() == []


# ---------------------------------------------------------------------------
# D5: one malformed file crashes every listing (post list, mentions, digest,
#     bare-slug read) and blocks commenting on its thread
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_content",
    [
        "---\nauthor: x\ndate: not-a-date\ntitle: broken\n---\n\nbody\n",
        b"---\nauthor: x\ntitle: latin1 \xe9\n---\n\nbody\n",
    ],
    ids=["bad-date", "non-utf8"],
)
def test_d5_malformed_post_does_not_crash_listing(tmp_path, bad_content):
    forum = _forum(tmp_path)
    Post.create(forum.project_dir("good"), "good", "bob", "Good post", "hello @alice")
    bad = forum.project_dir("aaa") / "2026-01-01-broken.md"
    bad.parent.mkdir(parents=True)
    if isinstance(bad_content, bytes):
        bad.write_bytes(bad_content)
    else:
        bad.write_text(bad_content)

    runner = CliRunner()
    res = runner.invoke(cli, ["--forum-dir", str(tmp_path), "post", "list"])
    assert res.exit_code == 0, repr(res.exception)
    assert "Good post" in res.output
    res = runner.invoke(cli, ["--forum-dir", str(tmp_path), "mentions", "-a", "alice"])
    assert res.exit_code == 0, repr(res.exception)


def test_d5_malformed_comment_blocks_comment_add(tmp_path):
    forum = _forum(tmp_path)
    post = Post.create(forum.project_dir("gptme"), "gptme", "alice", "Thread", "hi")
    post.comment_dir.mkdir(parents=True)
    (post.comment_dir / "comment-01-x.md").write_text(
        "---\nauthor: x\ndate: yesterday\n---\n\nhm\n"
    )
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            "--forum-dir",
            str(tmp_path),
            "comment",
            "add",
            post.ref,
            "reply",
            "-a",
            "bob",
        ],
    )
    assert res.exit_code == 0, repr(res.exception)


# ---------------------------------------------------------------------------
# D6: mentions_for(since=...) `continue`s past an old post, skipping its comments,
#     so --unread never reports a mention in a reply to an older thread
# ---------------------------------------------------------------------------


def test_d6_unread_mentions_reports_comment_on_old_post(tmp_path):
    forum = _forum(tmp_path)
    state = tmp_path / "state.txt"
    post = Post.create(forum.project_dir("gptme"), "gptme", "alice", "Thread", "hi")
    assert forum.unread_mentions("bob", state_file=state) == []
    Comment.create(post.comment_dir, "alice", "@bob can you check?", index=1)
    items = forum.unread_mentions("bob", state_file=state)
    assert [k for _, k in items] == ["comment"]


# ---------------------------------------------------------------------------
# D7: unread state is stamped with the time AFTER the scan, so anything written
#     while the scan runs is marked read without ever being shown
# ---------------------------------------------------------------------------


def test_d7_unread_mentions_item_written_during_scan_is_not_lost(tmp_path, monkeypatch):
    forum = _forum(tmp_path)
    state = tmp_path / "state.txt"
    real = Forum.mentions_for

    def scan_then_concurrent_post(self, agent, since=None):
        res = real(self, agent, since=since)
        # Another agent posts after the scan finished but before state is written.
        Post.create(self.project_dir("gptme"), "gptme", "alice", "Ping", "@bob ping")
        return res

    monkeypatch.setattr(Forum, "mentions_for", scan_then_concurrent_post)
    assert forum.unread_mentions("bob", state_file=state) == []
    monkeypatch.setattr(Forum, "mentions_for", real)
    items = forum.unread_mentions("bob", state_file=state)
    assert len(items) == 1, "mention written during the previous scan was never shown"


def test_d7_unread_digest_item_written_during_scan_is_not_lost(tmp_path, monkeypatch):
    forum = _forum(tmp_path)
    state = tmp_path / "state.txt"
    real = Forum.digest

    def scan_then_concurrent_post(self, agent=None, since=None):
        res = real(self, agent=agent, since=since)
        Post.create(self.project_dir("gptme"), "gptme", "alice", "News", "body")
        return res

    monkeypatch.setattr(Forum, "digest", scan_then_concurrent_post)
    forum.unread_digest(agent="bob", state_file=state)
    monkeypatch.setattr(Forum, "digest", real)
    data = forum.unread_digest(agent="bob", state_file=state)
    assert len(data["new_posts"]) == 1


# ---------------------------------------------------------------------------
# D8: unread filter compares the AUTHOR's timestamp to the reader's last-check
#     time, so a post that arrives later via `git pull` is permanently missed
# ---------------------------------------------------------------------------


def test_d8_unread_mentions_sees_post_arriving_via_git_pull(tmp_path):
    forum = _forum(tmp_path)
    state = tmp_path / "state.txt"
    # alice wrote this in her clone 10 minutes ago, but bob has not pulled yet.
    authored = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    assert forum.unread_mentions("bob", state_file=state) == []
    # bob pulls: the file now appears in his checkout.
    pulled = forum.project_dir("gptme") / "2026-11-24-review.md"
    _write_post(pulled, "alice", authored, "Review", "@bob please review")
    # The pull happens after the check; filesystem timestamps are coarser than
    # datetime.now(), so make "after" unambiguous instead of sub-millisecond.
    later = datetime.now(tz=timezone.utc).timestamp() + 1
    import os

    os.utime(pulled, (later, later))
    items = forum.unread_mentions("bob", state_file=state)
    assert len(items) == 1, "mention that arrived via git pull was never reported"


# ---------------------------------------------------------------------------
# D9: `digest --unread` advances the state file before rendering; a render
#     crash (empty-body comment -> splitlines()[0]) marks everything read
# ---------------------------------------------------------------------------


def test_d9_digest_unread_crash_does_not_mark_activity_read(tmp_path):
    forum_root = tmp_path / "forum"
    Forum(forum_root).ensure_exists()
    state = tmp_path / "digest-state.txt"
    runner = CliRunner()
    base = ["--forum-dir", str(tmp_path)]
    runner.invoke(
        cli,
        base + ["post", "create", "gptme", "Important", "-b", "news", "-a", "alice"],
    )
    post = next(Forum(forum_root).iter_posts())
    # A whitespace body used to pass the CLI's `if not body` guard; it is now
    # rejected, so create the empty-body comment the way a conflict/touch would.
    runner.invoke(cli, base + ["comment", "add", post.ref, "   ", "-a", "carol"])
    post.comment_dir.mkdir(parents=True, exist_ok=True)
    (post.comment_dir / "comment-01-carol.md").write_text(
        "---\nauthor: carol\n---\n\n   \n"
    )

    first = runner.invoke(
        cli, base + ["digest", "--unread", "--state-file", str(state), "-a", "bob"]
    )
    second = runner.invoke(
        cli, base + ["digest", "--unread", "--state-file", str(state), "-a", "bob"]
    )
    shown = (first.output if first.exit_code == 0 else "") + second.output
    assert (
        "Important" in shown
    ), f"first run crashed ({first.exception!r}) and second run shows: {second.output!r}"


# ---------------------------------------------------------------------------
# D10: unread state file is keyed by agent only (cwd git root), not by forum, so
#      checking one forum marks every other forum's mentions as read
# ---------------------------------------------------------------------------


def test_d10_unread_state_is_not_shared_across_forums(tmp_path, monkeypatch):
    cwd = tmp_path / "home-repo"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q", str(cwd)], check=True)
    monkeypatch.chdir(cwd)
    a, b = tmp_path / "team-a", tmp_path / "team-b"
    for d, title in ((a, "From A"), (b, "From B")):
        f = Forum(d / "forum")
        f.ensure_exists()
        Post.create(f.project_dir("p"), "p", "alice", title, "@bob look")
    runner = CliRunner()
    ra = runner.invoke(
        cli, ["--forum-dir", str(a), "mentions", "--unread", "-a", "bob"]
    )
    assert "Mentions for @bob (1)" in ra.output
    rb = runner.invoke(
        cli, ["--forum-dir", str(b), "mentions", "--unread", "-a", "bob"]
    )
    assert "Mentions for @bob (1)" in rb.output, rb.output


# ---------------------------------------------------------------------------
# D11: bare-slug refs are ambiguous across projects; `comment add SLUG` silently
#      writes to whichever project sorts first
# ---------------------------------------------------------------------------


def test_d11_ambiguous_bare_slug_does_not_silently_pick_a_project(tmp_path):
    forum = _forum(tmp_path)
    pa = Post.create(forum.project_dir("alpha"), "alpha", "alice", "Standup", "a")
    pb = Post.create(forum.project_dir("beta"), "beta", "bob", "Standup", "b")
    assert pa.slug == pb.slug
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            "--forum-dir",
            str(tmp_path),
            "comment",
            "add",
            pb.slug,
            "for beta",
            "-a",
            "carol",
        ],
    )
    # Either refuse as ambiguous, or at least do not write to a post the user
    # did not identify. Current code exits 0 and writes to alpha.
    assert res.exit_code != 0 or not pa.comments(), (
        "comment silently attached to alpha/" + pa.slug
    )


# ---------------------------------------------------------------------------
# D12: comments are ordered by filename, so comment #100+ sorts before #11
# ---------------------------------------------------------------------------


def test_d12_comments_keep_order_past_99(tmp_path):
    forum = _forum(tmp_path)
    post = Post.create(forum.project_dir("gptme"), "gptme", "alice", "Long", "hi")
    for _ in range(100):
        Comment.create(
            post.comment_dir,
            "bob",
            f"reply {post.next_comment_index()}",
            index=post.next_comment_index(),
        )
    bodies = [c.body for c in post.comments()]
    assert bodies[-1] == "reply 100", f"newest comment not last; tail={bodies[-3:]}"
    assert bodies == [f"reply {i}" for i in range(1, 101)]


# ---------------------------------------------------------------------------
# D13: `post create` / `msg send` ignore the editor's exit status; an aborted
#      editor (non-zero exit, e.g. vim :cq) still publishes the template
# ---------------------------------------------------------------------------


def test_d13_post_create_aborted_editor_does_not_publish(tmp_path):
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["--forum-dir", str(tmp_path), "post", "create", "gptme", "Draft", "-a", "bob"],
        env={"EDITOR": "false"},
    )
    posts = list((tmp_path / "forum" / "projects").rglob("*.md"))
    assert (
        res.exit_code != 0 and posts == []
    ), f"exit={res.exit_code} published={[p.read_text() for p in posts]}"


def test_d13_msg_send_aborted_editor_does_not_send(tmp_path):
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["--forum-dir", str(tmp_path), "msg", "send", "alice", "Draft", "-a", "bob"],
        env={"EDITOR": "false"},
    )
    sent = list(tmp_path.rglob("from-bob-to-alice*.md"))
    assert res.exit_code != 0 and sent == [], f"exit={res.exit_code} sent={sent}"
