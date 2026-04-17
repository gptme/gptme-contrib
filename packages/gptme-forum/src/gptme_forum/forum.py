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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import yaml

# Regex to find @mentions inline in body text
_MENTION_RE = re.compile(r"@(\w+)")


def find_mentions(text: str) -> list[str]:
    """Extract @mentions from body text (e.g. '@alice' → ['alice'])."""
    return list(dict.fromkeys(_MENTION_RE.findall(text)))


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
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw_yaml = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    try:
        meta = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, body


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

    @classmethod
    def from_file(cls, path: Path) -> Comment:
        text = path.read_text()
        meta, body = _parse_frontmatter(text)
        date_raw = meta.get("date", datetime.now(tz=timezone.utc))
        if isinstance(date_raw, str):
            date = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        elif isinstance(date_raw, datetime):
            date = date_raw
        else:
            date = datetime.now(tz=timezone.utc)
        # Normalize naive datetimes to UTC (defensive: treat as UTC if no tz info)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return cls(
            path=path,
            author=meta.get("author", "unknown"),
            date=date,
            body=body.strip(),
            mentions=find_mentions(body),
        )

    @classmethod
    def create(cls, post_dir: Path, author: str, body: str, index: int) -> Comment:
        post_dir.mkdir(parents=True, exist_ok=True)
        filename = f"comment-{index:02d}-{author}.md"
        path = post_dir / filename
        # Avoid collisions (e.g. concurrent comment add from same agent)
        counter = 1
        while path.exists():
            filename = f"comment-{index:02d}-{author}-{counter}.md"
            path = post_dir / filename
            counter += 1
        now = datetime.now(tz=timezone.utc)
        meta = {"author": author, "date": now.isoformat()}
        path.write_text(_render_frontmatter(meta, body))
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
        paths = sorted(self.comment_dir.glob("comment-*.md"))
        return [Comment.from_file(p) for p in paths]

    def next_comment_index(self) -> int:
        return len(self.comments()) + 1

    @classmethod
    def from_file(cls, path: Path, project: str) -> Post:
        text = path.read_text()
        meta, body = _parse_frontmatter(text)
        date_raw = meta.get("date", datetime.now(tz=timezone.utc))
        if isinstance(date_raw, str):
            date = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
        elif isinstance(date_raw, datetime):
            date = date_raw
        else:
            date = datetime.now(tz=timezone.utc)
        # Normalize naive datetimes to UTC (defensive: treat as UTC if no tz info)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return cls(
            path=path,
            project=project,
            author=meta.get("author", "unknown"),
            date=date,
            title=meta.get("title", path.stem),
            tags=meta.get("tags", []),
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
        filename = f"{date_str}-{slug}.md"
        path = project_dir / filename
        # Avoid collisions
        counter = 1
        while path.exists():
            path = project_dir / f"{date_str}-{slug}-{counter}.md"
            counter += 1
        meta: dict = {"author": author, "date": now.isoformat(), "title": title}
        if tags:
            meta["tags"] = tags
        path.write_text(_render_frontmatter(meta, body))
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
        return self.projects_dir / project

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
                yield Post.from_file(post_path, proj)

    def get_post(self, ref: str) -> Post | None:
        """Look up a post by 'project/slug' or just 'slug' (searches all projects)."""
        if "/" in ref:
            project, slug = ref.split("/", 1)
            path = self.projects_dir / project / f"{slug}.md"
            if path.exists():
                return Post.from_file(path, project)
            return None
        # Search all projects
        for post in self.iter_posts():
            if post.slug == ref:
                return post
        return None

    def mentions_for(
        self, agent: str, since: datetime | None = None
    ) -> list[tuple[Post | Comment, str]]:
        """Return (post_or_comment, type) pairs where agent is mentioned.

        type is 'post' or 'comment'.
        Optionally filter to items newer than `since`.
        """
        results: list[tuple[Post | Comment, str]] = []
        for post in self.iter_posts():
            if since and post.date <= since:
                continue
            if agent in post.mentions:
                results.append((post, "post"))
            for comment in post.comments():
                if since and comment.date <= since:
                    continue
                if agent in comment.mentions:
                    results.append((comment, "comment"))
        return results

    def unread_mentions(
        self, agent: str, state_file: Path | None = None
    ) -> list[tuple[Post | Comment, str]]:
        """Return mentions since last check, updating state_file."""
        since: datetime | None = None
        if state_file and state_file.exists():
            raw = state_file.read_text().strip()
            if raw:
                try:
                    since = datetime.fromisoformat(raw)
                except ValueError:
                    pass
        results = self.mentions_for(agent, since=since)
        # Update state
        if state_file:
            now = datetime.now(tz=timezone.utc)
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
        new_posts: list[Post] = []
        new_comments: list[tuple[Comment, Post]] = []
        mentions: list[tuple[Post | Comment, str]] = []

        for post in self.iter_posts():
            is_new_post = since is None or post.date > since
            if is_new_post:
                new_posts.append(post)
            if agent and agent in post.mentions and is_new_post:
                mentions.append((post, "post"))
            for comment in post.comments():
                is_new_comment = since is None or comment.date > since
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
            raw = state_file.read_text().strip()
            if raw:
                try:
                    since = datetime.fromisoformat(raw)
                except ValueError:
                    pass
        result = self.digest(agent=agent, since=since)
        if state_file:
            now = datetime.now(tz=timezone.utc)
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(now.isoformat())
        return result
