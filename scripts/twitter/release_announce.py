#!/usr/bin/env python3
"""Announce a new stable release on X, then quote-tweet it from Bob.

Flow (idempotent, state-ledgered):
  1. Read the latest release of --repo via `gh release view`.
  2. Skip prereleases and already-announced tags.
  3. Compose a short announcement from the release notes (feat-line
     extraction, no LLM) and post it from the org account
     (``twitter.py --account gptmeorg post``), with the release URL in a
     reply tweet (links depress reach in the main tweet).
  4. Quote-tweet the announcement from the default account (Bob).

Designed to run from a timer: exits 0 quickly when there is nothing new.

Usage:
    release_announce.py                       # announce latest stable, if new
    release_announce.py --tag v0.33.0         # announce a specific tag
    release_announce.py --dry-run             # print tweets, post nothing
    release_announce.py --skip-quote          # org tweet only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_USER_CONTEXT_VARS = (
    "TWITTER_OAUTH2_ACCESS_TOKEN",
    "TWITTER_OAUTH2_REFRESH_TOKEN",
    "TWITTER_OAUTH2_EXPIRES_AT",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_SECRET",
    "TWITTER_EXPECTED_USERNAME",
)
_AUTOMATION_UNSAFE_OAUTH_VARS = (
    "TWITTER_OAUTH_CALLBACK_FILE",
    "TWITTER_OAUTH_CALLBACK_TIMEOUT",
)

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - non-Windows
    _msvcrt = None  # type: ignore[assignment]

TWITTER_CLI = Path(__file__).resolve().parent / "twitter.py"
STATE_FILE = (
    Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    / "gptwitter"
    / "release-announcements.json"
)

STABLE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
# Release notes carry lines like:
#   * feat(cli): add `gptme explain` for offline concept answers by @Bob in #3542
#   - feat(tools): read-only audit preset (#3543)
_FEAT_RE = re.compile(r"^[*-]\s*feat(?:\(([^)]*)\))?!?:\s*(.+)$")
_AUTHOR_LIST = r"@[\w-]+(?:\s+(?:and|&)\s+@[\w-]+)*"
_AUTHOR_TRAIL_RE = re.compile(rf"\s+by {_AUTHOR_LIST}[.,;:!?]*\s*$")
_PR_NUMBER_TRAIL_RE = re.compile(r"\s+(?:in\s+)?\(?#\d+\)?[.,;:!?]*\s*$")
_RELEASE_URL_TRAIL_RE = re.compile(
    r"\s+(?:in\s+)?\(?https?://(?:www\.)?github\.com/[^/\s]+/[^/\s]+/"
    r"(?:pull|issues)/\d+/?\)?[.,;:!?]*\s*$"
)

MAX_TWEET = 270  # headroom under 280


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{STATE_FILE.name}.", dir=STATE_FILE.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(STATE_FILE)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def state_lock() -> Iterator[None]:
    """Serialize the full post-and-record transaction across timer runs."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_file = STATE_FILE.with_name(f"{STATE_FILE.name}.lock")
    with lock_file.open("ab") as lock:
        if _fcntl is not None:
            _fcntl.flock(lock, _fcntl.LOCK_EX)
        elif _msvcrt is not None:  # pragma: no cover - Windows only
            if lock.tell() == 0:
                lock.write(b"\x00")
                lock.flush()
            lock.seek(0)
            _msvcrt.locking(lock.fileno(), _msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(lock, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover - Windows only
                lock.seek(0)
                _msvcrt.locking(lock.fileno(), _msvcrt.LK_UNLCK, 1)


def latest_release(repo: str, tag: str | None) -> dict | None:
    cmd = ["gh", "release", "view"]
    if tag:
        cmd.append(tag)
    cmd += ["--repo", repo, "--json", "tagName,isPrerelease,body,url,publishedAt"]
    r = _run(cmd)
    if r.returncode != 0:
        print(f"gh release view failed: {r.stderr.strip()}", file=sys.stderr)
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        print(f"gh release view returned invalid JSON: {exc}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def extract_features(body: str, limit: int = 5) -> list[str]:
    """Pull the most user-facing feat lines out of auto-generated notes."""
    feats: list[str] = []
    for line in body.splitlines():
        m = _FEAT_RE.match(line.strip())
        if not m:
            continue
        desc = m.group(2)
        while True:
            stripped = _AUTHOR_TRAIL_RE.sub("", desc)
            stripped = _PR_NUMBER_TRAIL_RE.sub("", stripped)
            stripped = _RELEASE_URL_TRAIL_RE.sub("", stripped).strip()
            if stripped == desc:
                break
            desc = stripped
        desc = desc.rstrip(".")
        # Strip markdown backticks/bold but keep the text
        desc = desc.replace("**", "")
        if desc:
            feats.append(desc)
    return feats[:limit]


def compose_announcement(tag: str, body: str, repo: str) -> str:
    version = tag.lstrip("v")
    feats = extract_features(body)
    header = f"gptme v{version} is out 🎉"
    if repo.split("/")[-1] != "gptme":
        header = f"{repo.split('/')[-1]} v{version} is out 🎉"
    if not feats:
        return f"{header}\n\nRelease notes in the reply."
    lines = [header, ""]
    for f in feats:
        candidate = lines + [f"— {f}"]
        if len("\n".join(candidate)) > MAX_TWEET:
            break
        lines = candidate
    return "\n".join(lines)


def compose_quote(tag: str, repo: str) -> str:
    repo_name = repo.split("/")[-1]
    return (
        f"{repo_name} {tag} is out — release notes below. "
        "Built with gptme, reviewed by agents, shipped by CI."
    )


def _post(args: list[str], account: str | None = None) -> tuple[bool, str | None]:
    """Run twitter.py post ... and return success plus the created tweet id."""
    cmd = [sys.executable, str(TWITTER_CLI)]
    env = os.environ.copy()
    for var in _AUTOMATION_UNSAFE_OAUTH_VARS:
        env.pop(var, None)
    if account:
        cmd += ["--account", account]
    else:
        env["TWITTER_ACCOUNT"] = ""
        for var in _USER_CONTEXT_VARS:
            env.pop(var, None)
    if args and args[0] == "post" and len(args) > 1:
        # Protect the tweet text from being parsed as a CLI option when it starts
        # with '-'. Restructure to: post [options] --headless -- TEXT.
        # CALLER CONTRACT: args[1] must be the tweet text; all trailing options
        # (--reply-to, --quote …) must come at args[2:]. Placing an option at
        # args[1] silently corrupts the command line, so _post callers must not
        # start with an option at position 1.
        text = args[1]
        rest = args[2:]  # any trailing options like --reply-to, --quote
        args = ["post", *rest, "--headless", "--", text]
    elif args and args[0] == "post":
        args = [*args, "--headless"]
    cmd += args
    r = _run(cmd, env=env)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        print(f"twitter.py failed ({r.returncode}):\n{out}", file=sys.stderr)
        return False, None
    # Strip ANSI so a Rich-colored "Tweet ID: N" still matches. Line-anchored
    # last match wins: tweet text containing "Tweet ID: 123" must not steal
    # the created id (the CLI prints the id on its own line after the body).
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    ids = re.findall(r"(?m)^\s*Tweet ID: (\d+)\s*$", clean)
    if not ids:
        print(
            "twitter.py succeeded but did not report a tweet ID; "
            "recording the step as posted and refusing to retry",
            file=sys.stderr,
        )
        return True, None
    return True, ids[-1]


def _pending_key(step: str) -> str:
    return f"{step.removesuffix('_id')}_pending_at"


def _begin_post(state: dict, key: str, record: dict, step: str) -> bool:
    """Persist post intent, refusing to repeat an ambiguous remote side effect.

    The pending marker is cleared only on confirmed success (``_finish_post``).
    If the post step failed (subprocess non-zero) or the process was killed
    after ``_begin_post`` but before the result was recorded, the marker remains
    so a later timer run refuses to retry blindly.

    Recovery: if you are certain the tweet was NOT posted (e.g. the crash
    happened before the subprocess started), run with ``--force`` to reset all
    state for this tag and retry from scratch.  If the tweet WAS posted but the
    ID was never recorded, reconcile the state file manually (set the tweet ID
    and remove the pending key) before running without ``--force``.
    """
    pending_key = _pending_key(step)
    if pending_key in record:
        print(
            f"{key}: {step} may already have been posted at "
            f"{record[pending_key]}; refusing to retry without reconciliation. "
            "Run with --force to reset all state for this tag (risk: duplicate "
            "tweet if the post succeeded), or edit the state file manually.",
            file=sys.stderr,
        )
        return False
    record[pending_key] = datetime.now(timezone.utc).isoformat()
    state[key] = record
    save_state(state)
    return True


def _finish_post(state: dict, record: dict, step: str, tweet_id: str | None) -> None:
    record[step] = tweet_id
    record.pop(_pending_key(step), None)
    save_state(state)


def _main(args: argparse.Namespace, rel: dict) -> int:
    tag = rel["tagName"]
    state = load_state()
    key = f"{args.repo}#{tag}"
    record = state.get(key, {}) if not args.force else {}
    # "org_tweet_id" in record with value None = posted but ID unknown; quote is
    # not recoverable without the ID, so treat this as a complete (degraded) state.
    _org_id_unknown = "org_tweet_id" in record and record["org_tweet_id"] is None
    if record.get("announced_at") and (
        args.skip_quote or "bob_quote_id" in record or _org_id_unknown
    ):
        print(f"{key}: already announced ({record.get('org_tweet_id')})")
        return 0

    announcement = compose_announcement(tag, rel.get("body", ""), args.repo)
    link_reply = rel["url"]
    quote_text = compose_quote(tag, args.repo)

    print(f"--- announcement (@{args.org_account}) ---\n{announcement}\n")
    print(f"--- link reply ---\n{link_reply}\n")
    if not args.skip_quote:
        print(f"--- Bob quote ---\n{quote_text}\n")
    if args.dry_run:
        print("dry-run: nothing posted")
        return 0

    org_id = record.get("org_tweet_id")
    if "org_tweet_id" not in record:
        if not _begin_post(state, key, record, "org_tweet_id"):
            return 1
        posted, org_id = _post(["post", announcement], account=args.org_account)
        if not posted:
            # Keep the pending marker so a later run refuses blind retry.
            # Use --force to reset if certain no tweet was posted.
            return 1
        _finish_post(state, record, "org_tweet_id", org_id)
    if org_id is None:
        # The org tweet was posted but its ID was not captured.  The reply and
        # quote steps both need the ID, so they cannot be posted automatically.
        # Mark as announced (degraded: no reply or quote) so that subsequent
        # timer runs do not permanently fail.
        # To add the missing reply and quote: edit the state file to set
        # org_tweet_id to the actual tweet ID and remove announced_at, then re-run.
        print(
            f"{key}: org tweet was posted but its ID is unknown; "
            "reply and quote skipped — marking as announced (degraded). "
            f"Recovery: set org_tweet_id in {STATE_FILE}, remove announced_at, then re-run.",
            file=sys.stderr,
        )
        record["announced_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return 0

    link_reply_id = record.get("link_reply_id")
    if "link_reply_id" not in record:
        if not _begin_post(state, key, record, "link_reply_id"):
            return 1
        posted, link_reply_id = _post(
            ["post", link_reply, "--reply-to", org_id], account=args.org_account
        )
        if not posted:
            # Keep the pending marker so a later run refuses blind retry.
            return 1
        _finish_post(state, record, "link_reply_id", link_reply_id)

    quote_id = record.get("bob_quote_id")
    if not args.skip_quote and "bob_quote_id" not in record:
        if not _begin_post(state, key, record, "bob_quote_id"):
            return 1
        posted, quote_id = _post(["post", quote_text, "--quote", org_id])
        if not posted:
            # Keep the pending marker so a later run refuses blind retry.
            return 1
        _finish_post(state, record, "bob_quote_id", quote_id)

    # When --skip-quote is in effect, clear any stale quote-pending marker so
    # a later run without --skip-quote can post the missing quote without --force.
    if args.skip_quote:
        record.pop(_pending_key("bob_quote_id"), None)
    record["announced_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print(f"announced {key}: org={org_id} quote={quote_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="gptme/gptme")
    ap.add_argument("--tag", default=None, help="announce this tag (default: latest)")
    ap.add_argument("--org-account", default="gptmeorg")
    ap.add_argument("--skip-quote", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--force",
        action="store_true",
        help=(
            "re-announce a done tag or clear stale pending markers left by a "
            "crashed run; resets all per-tag state, so a partially-completed "
            "announcement may re-post already-sent tweets"
        ),
    )
    args = ap.parse_args()

    rel = latest_release(args.repo, args.tag)
    if rel is None:
        return 1
    tag = rel["tagName"]
    if rel.get("isPrerelease"):
        print(f"{tag}: prerelease — skipping")
        return 0
    if not STABLE_TAG_RE.match(tag):
        print(f"{tag}: not a stable vX.Y.Z tag — skipping")
        return 0
    if args.dry_run:
        return _main(args, rel)
    with state_lock():
        return _main(args, rel)


if __name__ == "__main__":
    sys.exit(main())
