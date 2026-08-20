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
from pathlib import Path

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
_TRAIL_RE = re.compile(
    r"\s*(?:by @[\w-]+)?\s*(?:in\s+)?(?:https?://\S+|\(?#\d+\)?)\s*$"
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
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_FILE)


def latest_release(repo: str, tag: str | None) -> dict | None:
    cmd = ["gh", "release", "view"]
    if tag:
        cmd.append(tag)
    cmd += ["--repo", repo, "--json", "tagName,isPrerelease,body,url,publishedAt"]
    r = _run(cmd)
    if r.returncode != 0:
        print(f"gh release view failed: {r.stderr.strip()}", file=sys.stderr)
        return None
    data = json.loads(r.stdout)
    return data if isinstance(data, dict) else None


def extract_features(body: str, limit: int = 5) -> list[str]:
    """Pull the most user-facing feat lines out of auto-generated notes."""
    feats: list[str] = []
    for line in body.splitlines():
        m = _FEAT_RE.match(line.strip())
        if not m:
            continue
        desc = _TRAIL_RE.sub("", m.group(2)).strip().rstrip(".")
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


def _post(args: list[str], account: str | None = None) -> str | None:
    """Run twitter.py post ... and return the created tweet id."""
    cmd = [sys.executable, str(TWITTER_CLI)]
    if account:
        cmd += ["--account", account]
    cmd += args
    r = _run(cmd)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        print(f"twitter.py failed ({r.returncode}):\n{out}", file=sys.stderr)
        return None
    m = re.search(r"Tweet ID: (\d+)", out)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="gptme/gptme")
    ap.add_argument("--tag", default=None, help="announce this tag (default: latest)")
    ap.add_argument("--org-account", default="gptmeorg")
    ap.add_argument("--handle", default=None, help="org handle for the quote text")
    ap.add_argument("--skip-quote", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-announce a done tag")
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

    state = load_state()
    key = f"{args.repo}#{tag}"
    if key in state and not args.force:
        print(f"{key}: already announced ({state[key].get('org_tweet_id')})")
        return 0

    announcement = compose_announcement(tag, rel.get("body", ""), args.repo)
    link_reply = rel["url"]
    quote_text = f"gptme {tag} is out — release notes below. Built with gptme, reviewed by agents, shipped by CI."

    print(f"--- announcement (@{args.org_account}) ---\n{announcement}\n")
    print(f"--- link reply ---\n{link_reply}\n")
    if not args.skip_quote:
        print(f"--- Bob quote ---\n{quote_text}\n")
    if args.dry_run:
        print("dry-run: nothing posted")
        return 0

    org_id = _post(["post", announcement], account=args.org_account)
    if not org_id:
        return 1
    _post(["post", link_reply, "--reply-to", org_id], account=args.org_account)

    quote_id = None
    if not args.skip_quote:
        quote_id = _post(["post", quote_text, "--quote", org_id])

    from datetime import datetime, timezone

    state[key] = {
        "org_tweet_id": org_id,
        "bob_quote_id": quote_id,
        "announced_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)
    print(f"announced {key}: org={org_id} quote={quote_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
