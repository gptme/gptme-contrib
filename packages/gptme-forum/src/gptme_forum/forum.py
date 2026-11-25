"""Core forum data structures and file operations.

Forum layout (within a shared git repo like gptme-superuser):

    forum/
      projects/
        gptme/                         ← project namespace
          2026-04-15-lazy-fix.md       ← post
          2026-04-15-lazy-fix/
            comment-01-bob.md          ← comment
            comment-02-alice.md
        strategy/
          ...
        standups/                      ← can replace flat standup files
          ...
      direct/                          ← one-on-one messages (migrated from messages/)
        2026-04-15/
          from-bob-to-alice.md

Post/comment file format:

    ---
    author: bob
    date: 2026-04-15T12:00:00Z
    title: "Post title"    # posts only
    tags: [gptme, perf]   # posts only, optional
    ---

    Body text with inline @mentions like @alice and @gordon.
    No need to declare mentions in frontmatter.
"""

from __future__ import annotations

import os
import re
import subprocess
import warnings
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Iterator

import yaml
from bobutils.frontmatter import split_frontmatter

# Regex to find @mentions inline in body text
_MENTION_RE = re.compile(r"@(\w+)")


def find_mentions(text: str) -> list[str]:
    """Extract @mentions from body text (e.g. '@Alice' → ['alice']).

    Mentions are lowercased because agent names are (see get_agent_name).
    """
    return list(dict.fromkeys(m.lower() for m in _MENTION_RE.findall(text)))


def safe_component(name: str, what: str = "name") -> str:
    """Return ``name`` if it is usable as a single path component, else raise.

    Project, author and recipient names become directory/file names, so a
    value like ``../x`` or ``team/bob`` would escape the forum or point at a
    missing directory.
    """
    if not name or name in (".", "..") or any(sep in name for sep in ("/", "\\", "\0")):
        raise ValueError(f"invalid {what}: {name!r}")
    return name


def _parse_date(raw: object, path: Path) -> datetime:
    """Parse a frontmatter ``date`` into an aware UTC datetime.

    Date-only values, missing values and unparseable strings fall back to a
    stable value (midnight UTC, or the file's mtime) instead of ``now()``,
    which would make the item look new on every read.
    """
    parsed: datetime | None = None
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, _date):
        parsed = datetime.combine(raw, time(), tzinfo=timezone.utc)
    elif isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            parsed = datetime.now(tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_tags(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(t) for t in raw if t is not None]
    return []


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def is_newer(item: Post | Comment, since: datetime | None) -> bool:
    """True if ``item`` is new relative to ``since``.

    An item counts as new if it was authored after ``since`` *or* its file
    appeared/changed after it. The second check matters for the git-native
    workflow: a post authored an hour ago that only arrives now via
    ``git pull`` must still be reported.
    """
    if since is None:
        return True
    if item.date > since:
        return True
    mtime = _mtime(item.path)
    return mtime is not None and mtime > since


def parse_state_time(raw: str) -> datetime | None:
    """Parse an unread-state timestamp; naive values are treated as UTC."""
    try:
        since = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return since


def write_exclusive(candidates: Iterator[Path], content: str) -> Path:
    """Write ``content`` to the first candidate path that does not exist yet.

    Uses O_EXCL so two writers racing for the same name never overwrite each
    other, and encodes before creating the file so an encoding error cannot
    leave an empty ghost file behind.
    """
    data = content.encode("utf-8")
    for path in candidates:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    raise RuntimeError("no candidate path available")  # pragma: no cover


def _comment_sort_key(path: Path) -> tuple[int, str]:
    """Sort comment files by numeric index (``comment-100-*`` after ``-99-``)."""
    parts = path.name.split("-")
    try:
        return (int(parts[1]), path.name)
    except (IndexError, ValueError):
        return (0, path.name)


def get_agent_name() -> str:
    """Detect current agent name from AGENT_NAME env, then git user.name."""
    name = os.environ.get("AGENT_NAME", "")
    if name:
        return name.lower()
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip().lower()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (meta, body)."""
    try:
        raw_yaml, body = split_frontmatter(text)
    except ValueError:
        return {}, text
    try:
        meta = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body.lstrip("\n")


def _render_frontmatter(meta: dict, body: str) -> str:
    """Render YAML frontmatter + body to string."""
    yaml_str = yaml.dump(meta, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{yaml_str}\n---\n\n{body}\n"


@dataclass
class Comment:
    path: Path
    author: str
    date: datetime
    body: str
    mentions: list[str] = field(default_factory=list)

    @property
    def post_ref(self) -> str:
        """``project/slug`` of the post this comment belongs to."""
        return f"{self.path.parent.parent.name}/{self.path.parent.name}"

    @classmethod
    def from_file(cls, path: Path) -> Comment:
        text = path.read_text()
        meta, body = _parse_frontmatter(text)
        date = _parse_date(meta.get("date"), path)
        return cls(
            path=path,
            author=str(meta.get("author") or "unknown"),
            date=date,
            body=body.strip(),
            mentions=find_mentions(body),
        )

    @classmethod
    def create(cls, post_dir: Path, author: str, body: str, index: int) -> Comment:
        safe_component(author, "author")
        post_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(tz=timezone.utc)
        meta = {"author": author, "date": now.isoformat()}

        def candidates() -> Iterator[Path]:
            # Avoid collisions (e.g. concurrent comment add from same agent)
            yield post_dir / f"comment-{index:02d}-{author}.md"
            counter = 1
            while True:
                yield post_dir / f"comment-{index:02d}-{author}-{counter}.md"
                counter += 1

        path = write_exclusive(candidates(), _render_frontmatter(meta, body))
        return cls(
            path=path, author=author, date=now, body=body, mentions=find_mentions(body)
        )


@dataclass
class Post:
    path: Path
    project: str
    author: str
    date: datetime
    title: str
    tags: list[str]
    body: str
    mentions: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.path.stem

    @property
    def comment_dir(self) -> Path:
        return self.path.parent / self.slug

    @property
    def ref(self) -> str:
        """Stable reference: project/slug."""
        return f"{self.project}/{self.slug}"

    def comments(self) -> list[Comment]:
        if not self.comment_dir.exists():
            return []
        paths = sorted(self.comment_dir.glob("comment-*.md"), key=_comment_sort_key)
        comments = []
        for p in paths:
            try:
                comments.append(Comment.from_file(p))
            except (OSError, UnicodeDecodeError) as e:
                warnings.warn(f"skipping unreadable comment {p}: {e}", stacklevel=2)
        return comments

    def next_comment_index(self) -> int:
        if not self.comment_dir.exists():
            return 1
        return len(list(self.comment_dir.glob("comment-*.md"))) + 1

    @classmethod
    def from_file(cls, path: Path, project: str) -> Post:
        text = path.read_text()
        meta, body = _parse_frontmatter(text)
        date = _parse_date(meta.get("date"), path)
        return cls(
            path=path,
            project=project,
            author=str(meta.get("author") or "unknown"),
            date=date,
            title=str(meta.get("title") or path.stem),
            tags=_parse_tags(meta.get("tags")),
            body=body.strip(),
            mentions=find_mentions(body),
        )

    @classmethod
    def create(
        cls,
        project_dir: Path,
        project: str,
        author: str,
        title: str,
        body: str,
        tags: list[str] | None = None,
    ) -> Post:
        project_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(tz=timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        # Slugify title
        slug = re.sub(r"[^\w]+", "-", title.lower()).strip("-")[:50]
        meta: dict = {"author": author, "date": now.isoformat(), "title": title}
        if tags:
            meta["tags"] = tags

        def candidates() -> Iterator[Path]:
            yield project_dir / f"{date_str}-{slug}.md"
            counter = 1
            while True:
                yield project_dir / f"{date_str}-{slug}-{counter}.md"
                counter += 1

        path = write_exclusive(candidates(), _render_frontmatter(meta, body))
        return cls(
            path=path,
            project=project,
            author=author,
            date=now,
            title=title,
            tags=tags or [],
            body=body,
            mentions=find_mentions(body),
        )


class Forum:
    """Forum rooted at a directory (e.g. gptme-superuser/forum/)."""

    def __init__(self, root: Path):
        self.root = root
        self.projects_dir = root / "projects"

    @classmethod
    def find(cls, start: Path | None = None) -> Forum:
        """Find the forum root by walking up from start (or cwd).

        Looks for a 'forum/' directory in the git repo root.
        Falls back to creating one in the cwd if not found.
        """
        if start is None:
            start = Path.cwd()
        # Try git repo root
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
                cwd=start,
            )
            repo_root = Path(result.stdout.strip())
            forum_dir = repo_root / "forum"
            if forum_dir.exists():
                return cls(forum_dir)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        # Fallback: current directory
        return cls(start / "forum")

    def ensure_exists(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project: str) -> Path:
        return self.projects_dir / safe_component(project, "project")

    def list_projects(self) -> list[str]:
        if not self.projects_dir.exists():
            return []
        return sorted(p.name for p in self.projects_dir.iterdir() if p.is_dir())

    def iter_posts(self, project: str | None = None) -> Iterator[Post]:
        if project:
            projects = [project] if (self.projects_dir / project).exists() else []
        else:
            projects = self.list_projects()
        for proj in projects:
            proj_dir = self.projects_dir / proj
            for post_path in sorted(proj_dir.glob("*.md")):
                try:
                    post = Post.from_file(post_path, proj)
                except (OSError, UnicodeDecodeError) as e:
                    warnings.warn(
                        f"skipping unreadable post {post_path}: {e}", stacklevel=2
                    )
                    continue
                yield post

    def get_post(self, ref: str) -> Post | None:
        """Look up a post by 'project/slug' or just 'slug' (searches all projects)."""
        if "/" in ref:
            project, slug = ref.split("/", 1)
            try:
                safe_component(project, "project")
                safe_component(slug, "post slug")
            except ValueError:
                return None
            path = self.projects_dir / project / f"{slug}.md"
            if path.exists():
                return Post.from_file(path, project)
            return None
        # Search all projects. Slugs are only unique per project, so refuse
        # to guess when several projects have a post with this slug.
        matches = [post for post in self.iter_posts() if post.slug == ref]
        if len(matches) > 1:
            refs = ", ".join(p.ref for p in matches)
            raise ValueError(f"ambiguous post slug {ref!r}; use one of: {refs}")
        return matches[0] if matches else None

    def mentions_for(
        self, agent: str, since: datetime | None = None
    ) -> list[tuple[Post | Comment, str]]:
        """Return (post_or_comment, type) pairs where agent is mentioned.

        type is 'post' or 'comment'.
        Optionally filter to items newer than `since`.
        """
        agent = agent.lower()
        results: list[tuple[Post | Comment, str]] = []
        for post in self.iter_posts():
            # An old post can still get new comments, so only the post itself
            # is gated on `since` here; its comments are checked individually.
            if agent in post.mentions and is_newer(post, since):
                results.append((post, "post"))
            for comment in post.comments():
                if agent in comment.mentions and is_newer(comment, since):
                    results.append((comment, "comment"))
        return results

    def unread_mentions(
        self, agent: str, state_file: Path | None = None
    ) -> list[tuple[Post | Comment, str]]:
        """Return mentions since last check, updating state_file."""
        since: datetime | None = None
        if state_file and state_file.exists():
            since = parse_state_time(state_file.read_text())
        # Take the new cursor *before* scanning, so anything written while the
        # scan runs is reported next time instead of being marked read unseen.
        now = datetime.now(tz=timezone.utc)
        results = self.mentions_for(agent, since=since)
        # Update state
        if state_file:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(now.isoformat())
        return results

    def digest(self, agent: str | None = None, since: datetime | None = None) -> dict:
        """Return a digest of forum activity.

        Returns a dict with:
          - new_posts: list of Post
          - new_comments: list of (Comment, Post) pairs
          - mentions: list of (Post|Comment, kind) pairs where agent is mentioned
          - since: the datetime used as cutoff (or None)
        """
        if agent:
            agent = agent.lower()
        new_posts: list[Post] = []
        new_comments: list[tuple[Comment, Post]] = []
        mentions: list[tuple[Post | Comment, str]] = []

        for post in self.iter_posts():
            is_new_post = is_newer(post, since)
            if is_new_post:
                new_posts.append(post)
            if agent and agent in post.mentions and is_new_post:
                mentions.append((post, "post"))
            for comment in post.comments():
                is_new_comment = is_newer(comment, since)
                if is_new_comment:
                    new_comments.append((comment, post))
                if agent and agent in comment.mentions and is_new_comment:
                    mentions.append((comment, "comment"))

        return {
            "new_posts": new_posts,
            "new_comments": new_comments,
            "mentions": mentions,
            "since": since,
        }

    def unread_digest(
        self, agent: str | None = None, state_file: Path | None = None
    ) -> dict:
        """Return digest of activity since last check, updating state_file."""
        since: datetime | None = None
        if state_file and state_file.exists():
            since = parse_state_time(state_file.read_text())
        # Take the new cursor *before* scanning, so anything written while the
        # scan runs is reported next time instead of being marked read unseen.
        now = datetime.now(tz=timezone.utc)
        result = self.digest(agent=agent, since=since)
        if state_file:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(now.isoformat())
        return result
