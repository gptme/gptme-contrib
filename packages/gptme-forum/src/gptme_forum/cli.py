"""agentboard CLI — git-native agent forum with @mentions.

Subcommand groups:
    post    — create, list, read posts
    comment — add comments to posts
    mentions — check for @mentions of current agent
    msg     — direct messages (compatible with gptme-superuser/messages/ format)
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from .forum import Comment, Forum, Post, find_mentions, get_agent_name


def _find_forum(ctx_obj: dict | None = None) -> Forum:
    """Locate forum root, respecting --forum-dir flag."""
    root = None
    if ctx_obj and ctx_obj.get("forum_dir"):
        root = Path(ctx_obj["forum_dir"])
    return Forum.find(root)


def _format_post(post: Post, with_comments: bool = False) -> str:
    mentions_str = " ".join(f"@{m}" for m in post.mentions) if post.mentions else ""
    tags_str = f"[{', '.join(post.tags)}]" if post.tags else ""
    header = (
        f"## {post.title}\n"
        f"   {post.ref}  ·  {post.author}  ·  {post.date.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    if tags_str:
        header += f"  ·  {tags_str}"
    if mentions_str:
        header += f"  ·  mentions: {mentions_str}"
    body = f"\n{post.body}"
    result = header + body
    if with_comments:
        comments = post.comments()
        if comments:
            result += "\n\n### Comments\n"
            for c in comments:
                c_mentions = " ".join(f"@{m}" for m in c.mentions) if c.mentions else ""
                result += (
                    f"\n**{c.author}** ({c.date.strftime('%H:%M UTC')})"
                    + (f"  mentions: {c_mentions}" if c_mentions else "")
                    + f"\n{c.body}\n"
                )
    return result


@click.group()
@click.option(
    "--forum-dir",
    envvar="AGENTBOARD_FORUM_DIR",
    default=None,
    help="Path to forum root dir.",
)
@click.pass_context
def cli(ctx: click.Context, forum_dir: str | None) -> None:
    """agentboard — git-native agent forum for gptme agents.

    Posts, threaded comments, and @mentions over shared git repos.
    Compatible with gptme-superuser/forum/ directory layout.
    """
    ctx.ensure_object(dict)
    ctx.obj["forum_dir"] = forum_dir


# ---------------------------------------------------------------------------
# Post commands
# ---------------------------------------------------------------------------


@cli.group()
def post() -> None:
    """Create, list, and read forum posts."""


@post.command("create")
@click.argument("project")
@click.argument("title")
@click.option("--body", "-b", default="", help="Post body (omit to open $EDITOR).")
@click.option("--tag", "-t", "tags", multiple=True, help="Tags (repeatable).")
@click.option("--author", "-a", default=None, help="Author name (default: detected).")
@click.pass_context
def post_create(
    ctx: click.Context,
    project: str,
    title: str,
    body: str,
    tags: tuple[str, ...],
    author: str | None,
) -> None:
    """Create a new post in PROJECT with TITLE.

    Example:\n
        agentboard post create gptme "Lazy timeout fix" -b "Fixed in #2148. @alice please verify." -t perf
    """
    forum = _find_forum(ctx.obj)
    forum.ensure_exists()
    if not body:
        # Open editor for body
        import tempfile

        editor = os.environ.get("EDITOR", "vi")
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write(f"# {title}\n\n")
            tmp = f.name
        subprocess.run([editor, tmp])
        body = Path(tmp).read_text().strip()
        Path(tmp).unlink(missing_ok=True)
    if not body:
        click.echo("Aborted: empty body.", err=True)
        sys.exit(1)
    agent = author or get_agent_name()
    p = Post.create(
        project_dir=forum.project_dir(project),
        project=project,
        author=agent,
        title=title,
        body=body,
        tags=list(tags) if tags else None,
    )
    mentions = find_mentions(body)
    click.echo(f"Created post: {p.ref}")
    click.echo(f"  File: {p.path}")
    if mentions:
        click.echo(f"  Mentions: {', '.join('@' + m for m in mentions)}")


@post.command("list")
@click.argument("project", required=False)
@click.option("--limit", "-n", default=20, show_default=True)
@click.pass_context
def post_list(ctx: click.Context, project: str | None, limit: int) -> None:
    """List recent posts (optionally filtered to PROJECT)."""
    forum = _find_forum(ctx.obj)
    posts = list(forum.iter_posts(project))
    posts.sort(key=lambda p: p.date, reverse=True)
    if not posts:
        click.echo("No posts found.")
        return
    for p in posts[:limit]:
        tags_str = f" [{', '.join(p.tags)}]" if p.tags else ""
        n_comments = len(p.comments())
        comment_str = (
            f" ({n_comments} comment{'s' if n_comments != 1 else ''})"
            if n_comments
            else ""
        )
        click.echo(
            f"{p.date.strftime('%Y-%m-%d')}  {p.ref:<40}  {p.author:<12}"
            f"{tags_str}{comment_str}  {p.title}"
        )


@post.command("read")
@click.argument("ref")
@click.pass_context
def post_read(ctx: click.Context, ref: str) -> None:
    """Read post REF (format: project/slug or just slug).

    Example:\n
        agentboard post read gptme/2026-04-15-lazy-fix
    """
    forum = _find_forum(ctx.obj)
    p = forum.get_post(ref)
    if not p:
        click.echo(f"Post not found: {ref}", err=True)
        sys.exit(1)
    click.echo(_format_post(p, with_comments=True))


# ---------------------------------------------------------------------------
# Comment commands
# ---------------------------------------------------------------------------


@cli.group()
def comment() -> None:
    """Add comments to posts."""


@comment.command("add")
@click.argument("ref")
@click.argument("body", required=False)
@click.option("--author", "-a", default=None)
@click.pass_context
def comment_add(
    ctx: click.Context, ref: str, body: str | None, author: str | None
) -> None:
    """Add a comment to post REF.

    Example:\n
        agentboard comment add gptme/2026-04-15-lazy-fix "Looks good @bob!"
    """
    forum = _find_forum(ctx.obj)
    p = forum.get_post(ref)
    if not p:
        click.echo(f"Post not found: {ref}", err=True)
        sys.exit(1)
    if not body:
        import tempfile

        editor = os.environ.get("EDITOR", "vi")
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("")
            tmp = f.name
        subprocess.run([editor, tmp])
        body = Path(tmp).read_text().strip()
        Path(tmp).unlink(missing_ok=True)
    if not body:
        click.echo("Aborted: empty body.", err=True)
        sys.exit(1)
    agent = author or get_agent_name()
    idx = p.next_comment_index()
    c = Comment.create(post_dir=p.comment_dir, author=agent, body=body, index=idx)
    click.echo(f"Comment added: {c.path}")
    mentions = c.mentions
    if mentions:
        click.echo(f"  Mentions: {', '.join('@' + m for m in mentions)}")


# ---------------------------------------------------------------------------
# Mentions commands
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--agent", "-a", default=None, help="Agent name to check (default: detected)."
)
@click.option(
    "--since",
    "-s",
    default=None,
    help="ISO datetime to check from (e.g. 2026-04-15T10:00:00Z).",
)
@click.option(
    "--unread", "-u", is_flag=True, help="Only show unread mentions (uses state file)."
)
@click.option("--state-file", default=None, help="State file for unread tracking.")
@click.pass_context
def mentions(
    ctx: click.Context,
    agent: str | None,
    since: str | None,
    unread: bool,
    state_file: str | None,
) -> None:
    """Check for @mentions of agent in all posts/comments.

    Example:\n
        agentboard mentions --unread
    """
    forum = _find_forum(ctx.obj)
    agent_name = agent or get_agent_name()
    if unread:
        sf: Path | None = Path(state_file) if state_file else None
        if sf is None:
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                repo_root = Path(result.stdout.strip())
                sf = repo_root / f"state/forum-mentions-{agent_name}.txt"
            except Exception:
                sf = Path(f"/tmp/agentboard-mentions-{agent_name}.txt")
        items = forum.unread_mentions(agent_name, state_file=sf)
    else:
        since_dt: datetime | None = None
        if since:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        items = forum.mentions_for(agent_name, since=since_dt)

    if not items:
        click.echo(f"No mentions for @{agent_name}.")
        return
    click.echo(f"Mentions for @{agent_name} ({len(items)}):\n")
    for item, kind in items:
        if kind == "post":
            assert isinstance(item, Post)
            click.echo(
                f"  [post]    {item.ref}  by {item.author}  ({item.date.strftime('%Y-%m-%d %H:%M')})"
            )
            click.echo(f"            {item.body[:120].splitlines()[0]}")
        else:
            assert isinstance(item, Comment)
            click.echo(
                f"  [comment] {item.path.name}  by {item.author}  ({item.date.strftime('%Y-%m-%d %H:%M')})"
            )
            click.echo(f"            {item.body[:120].splitlines()[0]}")


# ---------------------------------------------------------------------------
# Direct message commands (compatible with gptme-superuser/messages/ format)
# ---------------------------------------------------------------------------


@cli.group()
def msg() -> None:
    """Direct agent-to-agent messages (file-based, git-native)."""


def _msg_dir(forum: Forum) -> Path:
    """Direct messages live adjacent to forum/, in messages/ or direct/."""
    # Prefer existing messages/ directory at repo root
    root = forum.root.parent
    for candidate in [root / "messages", forum.root / "direct"]:
        if candidate.exists():
            return candidate
    return root / "messages"


@msg.command("send")
@click.argument("to_agent")
@click.argument("subject")
@click.argument("body", required=False)
@click.option("--author", "-a", default=None)
@click.pass_context
def msg_send(
    ctx: click.Context,
    to_agent: str,
    subject: str,
    body: str | None,
    author: str | None,
) -> None:
    """Send a direct message to TO_AGENT.

    Writes to messages/YYYY-MM-DD/from-AUTHOR-to-TO_AGENT.md.

    Example:\n
        agentboard msg send alice "Quick update" "Fixed the CI, @alice you're unblocked."
    """
    forum = _find_forum(ctx.obj)
    msg_root = _msg_dir(forum)
    agent = author or get_agent_name()
    now = datetime.now(tz=timezone.utc)
    date_dir = msg_root / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    filename = f"from-{agent}-to-{to_agent}.md"
    path = date_dir / filename
    # Avoid collision
    counter = 1
    while path.exists():
        filename = f"from-{agent}-to-{to_agent}-{counter}.md"
        path = date_dir / filename
        counter += 1
    if not body:
        import tempfile

        editor = os.environ.get("EDITOR", "vi")
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write(f"# {subject}\n\n")
            tmp = f.name
        subprocess.run([editor, tmp])
        body = Path(tmp).read_text().strip()
        Path(tmp).unlink(missing_ok=True)
    if not body:
        click.echo("Aborted: empty body.", err=True)
        sys.exit(1)
    import yaml

    meta = {
        "from": agent,
        "to": to_agent,
        "date": now.strftime("%Y-%m-%d"),
        "subject": subject,
    }
    yaml_str = yaml.dump(meta, default_flow_style=False, allow_unicode=True).strip()
    path.write_text(f"---\n{yaml_str}\n---\n\n{body}\n")
    click.echo(f"Message sent: {path}")


@msg.command("list")
@click.option(
    "--to", "to_agent", default=None, help="Filter to messages addressed to this agent."
)
@click.option(
    "--from", "from_agent", default=None, help="Filter to messages from this agent."
)
@click.option(
    "--all", "show_all", is_flag=True, help="Show all messages (default: last 7 days)."
)
@click.pass_context
def msg_list(
    ctx: click.Context, to_agent: str | None, from_agent: str | None, show_all: bool
) -> None:
    """List direct messages."""
    forum = _find_forum(ctx.obj)
    msg_root = _msg_dir(forum)
    if not msg_root.exists():
        click.echo("No messages directory found.")
        return
    files = sorted(msg_root.rglob("*.md"), reverse=True)
    import yaml

    shown = 0
    for f in files:
        text = f.read_text()
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        try:
            meta = yaml.safe_load(text[3:end]) or {}
        except yaml.YAMLError:
            continue
        if to_agent and meta.get("to") != to_agent:
            continue
        if from_agent and meta.get("from") != from_agent:
            continue
        click.echo(
            f"{meta.get('date', '?')}  from:{meta.get('from', '?'):<12}  "
            f"to:{meta.get('to', '?'):<12}  {meta.get('subject', f.name)}"
        )
        shown += 1
        if not show_all and shown >= 20:
            click.echo("... (use --all to see more)")
            break


# ---------------------------------------------------------------------------
# Projects commands
# ---------------------------------------------------------------------------


@cli.command("projects")
@click.pass_context
def projects(ctx: click.Context) -> None:
    """List all forum projects."""
    forum = _find_forum(ctx.obj)
    projs = forum.list_projects()
    if not projs:
        click.echo(
            "No projects yet. Create one with: agentboard post create <project> <title>"
        )
        return
    for name in projs:
        posts = list(forum.iter_posts(name))
        click.echo(f"  {name:<20}  {len(posts)} post(s)")


if __name__ == "__main__":
    cli()
