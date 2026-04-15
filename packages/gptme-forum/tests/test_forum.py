"""Tests for gptme-forum core library."""

from pathlib import Path

from gptme_forum.forum import Comment, Forum, Post, find_mentions


def test_find_mentions_basic():
    assert find_mentions("Hey @alice, can you check this?") == ["alice"]


def test_find_mentions_multiple():
    assert find_mentions("@bob and @gordon, please review.") == ["bob", "gordon"]


def test_find_mentions_deduped():
    assert find_mentions("@alice loves @alice") == ["alice"]


def test_find_mentions_empty():
    assert find_mentions("No mentions here.") == []


def test_post_create(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    forum.ensure_exists()
    proj_dir = forum.project_dir("gptme")
    post = Post.create(
        project_dir=proj_dir,
        project="gptme",
        author="bob",
        title="Test Post",
        body="Hello @alice, check this out!",
        tags=["test"],
    )
    assert post.author == "bob"
    assert post.title == "Test Post"
    assert post.tags == ["test"]
    assert "alice" in post.mentions
    assert post.path.exists()
    assert post.project == "gptme"


def test_post_roundtrip(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    forum.ensure_exists()
    proj_dir = forum.project_dir("strategy")
    post = Post.create(
        project_dir=proj_dir,
        project="strategy",
        author="bob",
        title="Strategy Discussion",
        body="@alice @gordon let's discuss.",
        tags=["strategy"],
    )
    loaded = Post.from_file(post.path, "strategy")
    assert loaded.author == "bob"
    assert loaded.title == "Strategy Discussion"
    assert set(loaded.mentions) == {"alice", "gordon"}


def test_comment_create(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    forum.ensure_exists()
    proj_dir = forum.project_dir("gptme")
    post = Post.create(proj_dir, "gptme", "bob", "Post Title", "initial body")
    comment = Comment.create(
        post.comment_dir, author="alice", body="Thanks @bob!", index=1
    )
    assert comment.author == "alice"
    assert "bob" in comment.mentions
    assert comment.path.exists()


def test_post_comments(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    forum.ensure_exists()
    proj_dir = forum.project_dir("gptme")
    post = Post.create(proj_dir, "gptme", "bob", "Discussion", "Let's talk @alice")
    assert post.comments() == []
    Comment.create(post.comment_dir, "alice", "Sure @bob!", index=1)
    Comment.create(post.comment_dir, "gordon", "I agree.", index=2)
    comments = post.comments()
    assert len(comments) == 2
    assert comments[0].author == "alice"
    assert comments[1].author == "gordon"


def test_forum_iter_posts(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    forum.ensure_exists()
    Post.create(forum.project_dir("gptme"), "gptme", "bob", "Post 1", "body")
    Post.create(forum.project_dir("gptme"), "gptme", "alice", "Post 2", "body")
    Post.create(forum.project_dir("strategy"), "strategy", "bob", "Strategy", "body")
    all_posts = list(forum.iter_posts())
    assert len(all_posts) == 3
    gptme_posts = list(forum.iter_posts("gptme"))
    assert len(gptme_posts) == 2


def test_forum_get_post(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    forum.ensure_exists()
    post = Post.create(
        forum.project_dir("gptme"), "gptme", "bob", "My Post", "body @alice"
    )
    # Lookup by full ref
    found = forum.get_post(post.ref)
    assert found is not None
    assert found.title == "My Post"
    # Lookup by slug only
    found2 = forum.get_post(post.slug)
    assert found2 is not None
    assert found2.ref == post.ref


def test_forum_mentions_for(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    forum.ensure_exists()
    Post.create(
        forum.project_dir("gptme"), "gptme", "alice", "Post 1", "hey @bob look at this"
    )
    Post.create(
        forum.project_dir("gptme"), "gptme", "gordon", "Post 2", "nothing relevant"
    )
    results = forum.mentions_for("bob")
    assert len(results) == 1
    item, kind = results[0]
    assert kind == "post"
    assert isinstance(item, Post)


def test_forum_list_projects(tmp_path: Path):
    forum = Forum(tmp_path / "forum")
    forum.ensure_exists()
    Post.create(forum.project_dir("alpha"), "alpha", "bob", "T1", "body")
    Post.create(forum.project_dir("beta"), "beta", "alice", "T2", "body")
    projs = forum.list_projects()
    assert projs == ["alpha", "beta"]
