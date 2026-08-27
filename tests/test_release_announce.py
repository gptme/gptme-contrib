"""Tests for scripts/twitter/release_announce.py (compose + state logic)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "release_announce", REPO_ROOT / "scripts" / "twitter" / "release_announce.py"
)
assert _spec and _spec.loader
ra = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("release_announce", ra)
_spec.loader.exec_module(ra)


def _fresh_published_at() -> str:
    from datetime import datetime, timedelta, timezone

    return (
        (datetime.now(timezone.utc) - timedelta(days=1))
        .isoformat()
        .replace("+00:00", "Z")
    )


NOTES = """## What's Changed
* feat(cli): add `gptme explain` for offline concept answers by @Bob in https://github.com/gptme/gptme/pull/3542
* feat(tools): add read-only audit preset by @Bob in #3543
* fix(grok-subscription): enable native tools API and add grok-4.6 by @TimeToBuildBob in #3571
* feat(autocompact): add keep_head to protect task context from compaction (#3567)
* chore: bump version to 0.33.0
* feat(prompts): end responses with a concrete next step by @x in #3559
* feat(webui): add trajectory copy commands by @y in #3491
* feat(review): artifact mode + structured handoff for review-watch (#3442) by @z in #3449

**Full Changelog**: https://github.com/gptme/gptme/compare/v0.32.1...v0.33.0
"""


def test_extract_features_strips_attribution_and_caps():
    feats = ra.extract_features(NOTES)
    assert len(feats) == 5
    assert feats[0] == "add `gptme explain` for offline concept answers"
    assert feats[1] == "add read-only audit preset"
    # fixes and chores are not features
    assert not any("grok-subscription" in f or "bump version" in f for f in feats)
    # no PR refs / authors leak through
    assert not any("#" in f or "@" in f or "http" in f for f in feats)


def test_extract_features_strips_mid_description_pr_ref():
    notes = "* feat(review): structured handoff (#3442) by @z in #3449"
    assert ra.extract_features(notes) == ["structured handoff"]


def test_extract_features_strips_standalone_trailing_author():
    notes = "* feat(cache): improve caching by @alice"
    assert ra.extract_features(notes) == ["improve caching"]


def test_extract_features_strips_multiple_trailing_authors():
    notes = "* feat(cache): improve caching by @alice and @bob (#100)"
    assert ra.extract_features(notes) == ["improve caching"]


def test_extract_features_preserves_description_url():
    notes = "* feat(docs): add guide at https://example.com/guide"
    assert ra.extract_features(notes) == ["add guide at https://example.com/guide"]


def test_extract_features_strips_github_pr_url():
    notes = "* feat(docs): add guide in https://github.com/gptme/gptme/pull/123"
    assert ra.extract_features(notes) == ["add guide"]


def test_extract_features_strips_author_with_trailing_period():
    # Trailing punctuation after author handle must not block stripping.
    notes = "* feat(cache): improve caching by @alice."
    assert ra.extract_features(notes) == ["improve caching"]


def test_extract_features_strips_author_with_trailing_comma():
    notes = "* feat(cache): improve caching by @alice,"
    assert ra.extract_features(notes) == ["improve caching"]


def test_extract_features_strips_github_pr_url_in_parens():
    # URL wrapped in parentheses must still be stripped.
    notes = "* feat(docs): add guide (https://github.com/gptme/gptme/pull/123)"
    assert ra.extract_features(notes) == ["add guide"]


def test_extract_features_strips_github_pr_url_with_trailing_period():
    # A trailing period after the URL must not prevent stripping.
    notes = "* feat(docs): add guide in https://github.com/gptme/gptme/pull/123."
    assert ra.extract_features(notes) == ["add guide"]


def test_post_places_double_dash_before_tweet_text(monkeypatch):
    # Tweet text starting with '-' must not be parsed as a CLI option.
    # _post must insert '--' before the text so Click treats it as positional.
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "Tweet ID: 42", "")

    monkeypatch.setattr(ra, "_run", fake_run)
    ok, tid, _ = ra._post(["post", "-f, --force option-like text"])
    assert ok
    assert tid == "42"
    cmd = captured[0]
    assert "--" in cmd
    dash_idx = cmd.index("--")
    text_idx = cmd.index("-f, --force option-like text")
    assert dash_idx < text_idx, "'--' must precede the tweet text"


def test_compose_fits_and_headlines():
    text = ra.compose_announcement("v0.33.0", NOTES, "gptme/gptme")
    assert text.startswith("gptme v0.33.0 is out")
    assert len(text) <= ra.MAX_TWEET
    assert "— add `gptme explain`" in text


def test_compose_without_feats_falls_back():
    text = ra.compose_announcement("v0.33.0", "* fix: only fixes\n", "gptme/gptme")
    assert "Release notes in the reply" in text


def test_quote_text_uses_repository_name():
    assert ra.compose_quote("v1.2.3", "owner/widget").startswith("widget v1.2.3 is out")


def test_stable_tag_gate():
    assert ra.STABLE_TAG_RE.match("v0.33.0")
    assert not ra.STABLE_TAG_RE.match("v0.32.2.dev20260817")


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "state" / "ra.json")
    assert ra.load_state() == {}
    ra.save_state({"gptme/gptme#v0.33.0": {"org_tweet_id": "1"}})
    assert ra.load_state()["gptme/gptme#v0.33.0"]["org_tweet_id"] == "1"
    # corrupt file -> empty dict, not crash
    ra.STATE_FILE.write_text("{broken")
    assert ra.load_state() == {}


def test_latest_release_handles_invalid_json(monkeypatch, capsys):
    monkeypatch.setattr(
        ra,
        "_run",
        lambda cmd: subprocess.CompletedProcess(cmd, 0, "not json", ""),
    )

    assert ra.latest_release("gptme/gptme", None) is None
    assert "returned invalid JSON" in capsys.readouterr().err


def test_post_ignores_tweet_id_substring_in_body(monkeypatch):
    """Tweet text containing 'Tweet ID: N' must not steal the created id."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Posted tweet: see Tweet ID: 999 in the notes\nTweet ID: 42\n",
            stderr="",
        )

    monkeypatch.setattr(ra, "_run", fake_run)
    assert ra._post(["post", "hello"])[:2] == (True, "42")


def test_post_distinguishes_failure_from_success_without_id(monkeypatch):
    monkeypatch.setattr(
        ra,
        "_run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "failed"),
    )
    assert ra._post(["post", "hello"])[:2] == (False, None)

    monkeypatch.setattr(
        ra,
        "_run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "posted", ""),
    )
    assert ra._post(["post", "hello"])[:2] == (True, None)


def test_main_skips_prerelease(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.1.dev20260820",
            "isPrerelease": True,
            "body": "",
            "url": "u",
            "publishedAt": _fresh_published_at(),
        },
    )
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 0
    assert "prerelease" in capsys.readouterr().out


def test_main_posts_org_reply_and_quote(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    calls: list[tuple] = []

    def fake_post(args, account=None):
        calls.append((account, args))
        return True, str(100 + len(calls)), ""

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 0

    assert calls[0][0] == "gptmeorg" and calls[0][1][0] == "post"
    assert calls[1][0] == "gptmeorg" and calls[1][1][1].endswith("v0.33.0")
    assert calls[1][1][2] == "--reply-to" and calls[1][1][3] == "101"
    # Bob's quote from the default account
    assert calls[2][0] is None and calls[2][1][2] == "--quote"
    assert calls[2][1][3] == "101"

    state = json.loads((tmp_path / "ra.json").read_text())
    rec = state["gptme/gptme#v0.33.0"]
    assert rec["org_tweet_id"] == "101" and rec["bob_quote_id"] == "103"

    # Second run: dedupe, no more posts
    n = len(calls)
    assert ra.main() == 0
    assert len(calls) == n


def test_main_resumes_after_link_reply_failure(monkeypatch, tmp_path):
    """A failed link_reply leaves the pending marker; the next run refuses retry."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    calls: list[tuple] = []
    results = iter([(True, "101", ""), (False, None, "boom")])

    def fake_post(args, account=None):
        calls.append((account, args))
        return next(results)

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    # First run: org tweet succeeds, link reply fails.
    assert ra.main() == 1
    partial = json.loads(ra.STATE_FILE.read_text())["gptme/gptme#v0.33.0"]
    assert partial["org_tweet_id"] == "101"
    # Pending marker must survive so the next run refuses blind retry.
    assert "link_reply_pending_at" in partial
    assert len(calls) == 2

    # Second run: link_reply_pending_at blocks retry to avoid duplicate tweet.
    assert ra.main() == 1
    assert len(calls) == 2  # no new _post calls


def test_main_resumes_after_quote_failure(monkeypatch, tmp_path):
    """A failed quote-tweet leaves the pending marker; the next run refuses retry."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    calls: list[tuple] = []
    results = iter([(True, "101", ""), (True, "102", ""), (False, None, "boom")])

    def fake_post(args, account=None):
        calls.append((account, args))
        return next(results)

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    # First run: org tweet and link reply succeed, quote fails.
    assert ra.main() == 1
    partial = json.loads(ra.STATE_FILE.read_text())["gptme/gptme#v0.33.0"]
    assert partial["org_tweet_id"] == "101"
    assert partial["link_reply_id"] == "102"
    assert "bob_quote_pending_at" in partial
    assert len(calls) == 3

    # Second run: bob_quote_pending_at blocks retry.
    assert ra.main() == 1
    assert len(calls) == 3  # no new _post calls


def test_main_adds_quote_after_skip_quote_run(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    calls: list[tuple] = []

    def fake_post(args, account=None):
        calls.append((account, args))
        return True, str(100 + len(calls)), ""

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py", "--skip-quote"])

    assert ra.main() == 0
    assert len(calls) == 2
    assert "announced_at" in ra.load_state()["gptme/gptme#v0.33.0"]

    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 0
    assert len(calls) == 3
    assert calls[-1][0] is None
    assert calls[-1][1][2:] == ["--quote", "101"]
    assert ra.load_state()["gptme/gptme#v0.33.0"]["bob_quote_id"] == "103"


def test_skip_quote_clears_stale_pending_marker(monkeypatch, tmp_path):
    """--skip-quote must clear a stale bob_quote_pending_at so a later normal
    run can post the missing quote without needing --force."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    calls: list[tuple] = []
    # Run 1: org + reply succeed, quote fails → leaves bob_quote_pending_at
    results1 = iter([(True, "101", ""), (True, "102", ""), (False, None, "boom")])

    def fake_post1(args, account=None):
        calls.append((account, args))
        return next(results1)

    monkeypatch.setattr(ra, "_post", fake_post1)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 1
    assert "bob_quote_pending_at" in ra.load_state()["gptme/gptme#v0.33.0"]

    # Run 2: --skip-quote → must clear bob_quote_pending_at and set announced_at
    monkeypatch.setattr(ra, "_post", lambda *a, **kw: (True, "999", ""))
    monkeypatch.setattr(sys, "argv", ["release_announce.py", "--skip-quote"])
    assert ra.main() == 0
    state = ra.load_state()["gptme/gptme#v0.33.0"]
    assert "announced_at" in state
    assert "bob_quote_pending_at" not in state

    # Run 3: normal run → must be able to post the quote (not stuck at 1)
    def fake_post3(args, account=None):
        calls.append((account, args))
        return True, "103", ""

    monkeypatch.setattr(ra, "_post", fake_post3)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 0
    assert ra.load_state()["gptme/gptme#v0.33.0"]["bob_quote_id"] == "103"


def test_skip_quote_early_exit_clears_stale_pending_marker(monkeypatch, tmp_path):
    """--skip-quote early-exit path must also clear stale bob_quote_pending_at.

    Scenario: first run with --skip-quote succeeds (sets announced_at), then a
    normal run fails the quote and leaves bob_quote_pending_at, then another
    --skip-quote run hits the early-exit branch.  Without the fix, the marker
    persists and every subsequent normal run is stuck (refuses blind retry).
    """
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )

    # Run 1: --skip-quote succeeds → sets announced_at
    monkeypatch.setattr(ra, "_post", lambda *a, **kw: (True, "101", ""))
    monkeypatch.setattr(sys, "argv", ["release_announce.py", "--skip-quote"])
    assert ra.main() == 0
    state = ra.load_state()["gptme/gptme#v0.33.0"]
    assert "announced_at" in state
    assert "bob_quote_id" not in state

    # Run 2: normal run, quote fails → leaves bob_quote_pending_at
    results2 = iter([(True, "102", "")])  # org reply recorded; only quote attempted

    def fake_post2(args, account=None):
        return next(results2) if "--quote" not in args else (False, None, "boom")

    monkeypatch.setattr(ra, "_post", fake_post2)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 1
    assert "bob_quote_pending_at" in ra.load_state()["gptme/gptme#v0.33.0"]

    # Run 3: --skip-quote → early-exit must clear bob_quote_pending_at
    monkeypatch.setattr(
        ra,
        "_post",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no post expected")),
    )
    monkeypatch.setattr(sys, "argv", ["release_announce.py", "--skip-quote"])
    assert ra.main() == 0
    state = ra.load_state()["gptme/gptme#v0.33.0"]
    assert "bob_quote_pending_at" not in state

    # Run 4: normal run → must succeed (not stuck refusing retry)
    monkeypatch.setattr(ra, "_post", lambda *a, **kw: (True, "103", ""))
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 0
    assert ra.load_state()["gptme/gptme#v0.33.0"]["bob_quote_id"] == "103"


def test_post_default_account_clears_environment_profile(monkeypatch):
    monkeypatch.setenv("TWITTER_ACCOUNT", "gptmeorg")
    for var in (*ra._USER_CONTEXT_VARS, *ra._AUTOMATION_UNSAFE_OAUTH_VARS):
        monkeypatch.setenv(var, f"org-{var.lower()}")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="Tweet ID: 123\n", stderr="")

    monkeypatch.setattr(ra, "_run", fake_run)

    assert ra._post(["post", "hello"])[:2] == (True, "123")
    assert "--headless" in seen["cmd"]
    assert seen["env"]["TWITTER_ACCOUNT"] == ""
    stripped = {*ra._USER_CONTEXT_VARS, *ra._AUTOMATION_UNSAFE_OAUTH_VARS}
    assert not stripped & seen["env"].keys()


def test_post_named_account_keeps_inherited_environment(monkeypatch):
    monkeypatch.setenv("TWITTER_ACCOUNT", "default")
    for var in ra._AUTOMATION_UNSAFE_OAUTH_VARS:
        monkeypatch.setenv(var, f"unsafe-{var.lower()}")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="Tweet ID: 123\n", stderr="")

    monkeypatch.setattr(ra, "_run", fake_run)

    assert ra._post(["post", "hello"], account="gptmeorg")[:2] == (True, "123")
    assert seen["cmd"][2:4] == ["--account", "gptmeorg"]
    assert "--headless" in seen["cmd"]
    assert seen["env"]["TWITTER_ACCOUNT"] == "default"
    assert not set(ra._AUTOMATION_UNSAFE_OAUTH_VARS) & seen["env"].keys()


def test_main_does_not_retry_success_without_tweet_id(monkeypatch, tmp_path):
    """When the org tweet posts but its ID is not captured, mark as announced
    (degraded: no reply or quote) and do not retry on subsequent runs."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    calls = 0

    def fake_post(args, account=None):
        nonlocal calls
        calls += 1
        return True, None, ""

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    # First run: org tweet posted (no ID) → degraded completion, exit 0.
    assert ra.main() == 0
    state = ra.load_state()["gptme/gptme#v0.33.0"]
    assert state["org_tweet_id"] is None
    assert "announced_at" in state
    # Second run: already announced (degraded) → early exit, no extra posts.
    assert ra.main() == 0
    assert calls == 1


def test_main_does_not_retry_reply_success_without_tweet_id(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    calls: list[tuple] = []
    results = iter([(True, "101", ""), (True, None, ""), (True, "103", "")])

    def fake_post(args, account=None):
        calls.append((account, args))
        return next(results)

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 0
    assert ra.main() == 0
    assert len(calls) == 3
    assert ra.load_state()["gptme/gptme#v0.33.0"]["link_reply_id"] is None


def test_main_holds_state_lock_while_posting(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    events: list[str] = []

    class FakeLock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, *exc):
            events.append("unlock")

    monkeypatch.setattr(ra, "state_lock", FakeLock)

    def fake_post(args, account=None):
        events.append("post")
        return True, str(len(events)), ""

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 0
    assert events == ["lock", "post", "post", "post", "unlock"]


def test_main_writes_intent_before_each_post(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    expected_markers = iter(
        [
            "org_tweet_pending_at",
            "link_reply_pending_at",
            "bob_quote_pending_at",
        ]
    )

    def fake_post(args, account=None):
        marker = next(expected_markers)
        record = ra.load_state()["gptme/gptme#v0.33.0"]
        assert marker in record
        return True, str(100 + len(record)), ""

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 0
    record = ra.load_state()["gptme/gptme#v0.33.0"]
    assert not any(key.endswith("_pending_at") for key in record)


def test_main_refuses_to_retry_ambiguous_post(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    ra.save_state(
        {"gptme/gptme#v0.33.0": {"org_tweet_pending_at": "2026-08-20T21:00:00+00:00"}}
    )

    def unexpected_post(args, account=None):
        raise AssertionError("an ambiguous post must not be retried")

    monkeypatch.setattr(ra, "_post", unexpected_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 1
    assert "refusing to retry" in capsys.readouterr().err


def test_main_keeps_intent_after_post_failure_prevents_retry(
    monkeypatch, tmp_path, capsys
):
    """A failed org-tweet post keeps the pending marker so blind retry is refused."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    calls = 0

    def fake_post(args, account=None):
        nonlocal calls
        calls += 1
        return False, None, ""  # every attempt fails

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    # First run: org-tweet fails → pending marker stays.
    assert ra.main() == 1
    record = ra.load_state()["gptme/gptme#v0.33.0"]
    assert "org_tweet_pending_at" in record
    assert calls == 1

    # Second run: pending marker blocks retry without --force.
    assert ra.main() == 1
    assert "refusing to retry" in capsys.readouterr().err
    assert calls == 1  # _post was NOT called again


def test_main_force_clears_pending_markers(monkeypatch, tmp_path):
    """--force resets state including stale pending markers from a crashed run."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    # Seed a stale pending marker from a hypothetical crashed run.
    ra.save_state(
        {
            "gptme/gptme#v0.33.0": {
                "org_tweet_pending_at": "2026-08-20T10:00:00+00:00",
            }
        }
    )
    calls: list[tuple] = []

    def fake_post(args, account=None):
        calls.append((account, args))
        return True, str(100 + len(calls)), ""

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py", "--force"])

    # --force ignores all pending markers and re-announces from scratch.
    assert ra.main() == 0
    assert len(calls) == 3
    record = ra.load_state()["gptme/gptme#v0.33.0"]
    assert record["org_tweet_id"] == "101"
    assert "announced_at" in record


UNQUOTABLE_ERR = (
    "twitter.py failed (1):\n"
    "Traceback (most recent call last):\n"
    "    raise Forbidden(response)\n"
    "tweepy.errors.Forbidden: 403 Forbidden\n"
    "You can only reply to or quote posts where you are mentioned or are the author.\n"
)


def test_is_unquotable_matches_the_api_rejection_only():
    assert ra._is_unquotable(UNQUOTABLE_ERR)
    assert ra._is_unquotable(UNQUOTABLE_ERR.upper())
    assert not ra._is_unquotable("429 Too Many Requests")
    assert not ra._is_unquotable("tweepy.errors.Forbidden: 403 Forbidden\nDuplicate.")


def _release_stub(monkeypatch):
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.13.2",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/ActivityWatch/activitywatch/releases/tag/v0.13.2",
            "publishedAt": _fresh_published_at(),
        },
    )


def test_main_skips_cross_account_quote_and_exits_zero(monkeypatch, tmp_path, capsys):
    """Bob cannot quote an org tweet he is not mentioned in: skip, don't loop."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    _release_stub(monkeypatch)
    calls: list[tuple] = []

    def fake_post(args, account=None):
        calls.append((account, args))
        if "--quote" in args:
            return False, None, UNQUOTABLE_ERR
        return True, str(100 + len(calls)), ""

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(
        sys, "argv", ["release_announce.py", "--repo", "ActivityWatch/activitywatch"]
    )

    # First run: org tweet + link reply post, quote is rejected but the run succeeds.
    assert ra.main() == 0
    record = json.loads(ra.STATE_FILE.read_text())[
        "ActivityWatch/activitywatch#v0.13.2"
    ]
    assert record["org_tweet_id"] == "101"
    assert record["link_reply_id"] == "102"
    assert record["bob_quote_id"] is None
    assert "gptmeorg" in record["bob_quote_skip_reason"]
    assert "bob_quote_pending_at" not in record
    assert record["announced_at"]
    assert len(calls) == 3
    assert "quote=skipped" in capsys.readouterr().out

    # Second run: already announced, no pending-marker loop, no new posts.
    assert ra.main() == 0
    assert len(calls) == 3


def test_main_still_refuses_retry_on_transient_quote_failure(monkeypatch, tmp_path):
    """A non-API-rule quote failure keeps the pending marker (no blind retry)."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    _release_stub(monkeypatch)
    calls: list[tuple] = []

    def fake_post(args, account=None):
        calls.append((account, args))
        if "--quote" in args:
            return False, None, "twitter.py failed (1):\n429 Too Many Requests\n"
        return True, str(100 + len(calls)), ""

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(
        sys, "argv", ["release_announce.py", "--repo", "ActivityWatch/activitywatch"]
    )

    assert ra.main() == 1
    record = json.loads(ra.STATE_FILE.read_text())[
        "ActivityWatch/activitywatch#v0.13.2"
    ]
    assert "bob_quote_pending_at" in record
    assert "bob_quote_id" not in record
    assert "bob_quote_skip_reason" not in record
    assert ra.main() == 1
    assert len(calls) == 3  # no blind retry


def test_main_quote_still_succeeds_when_bob_is_mentioned(monkeypatch, tmp_path):
    """No regression on the gptme lane, where the org tweet mentions Bob."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
            "publishedAt": _fresh_published_at(),
        },
    )
    monkeypatch.setattr(ra, "_post", lambda *a, **kw: (True, "777", ""))
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 0
    record = json.loads(ra.STATE_FILE.read_text())["gptme/gptme#v0.33.0"]
    assert record["bob_quote_id"] == "777"
    assert "bob_quote_skip_reason" not in record


def test_stale_latest_release_skipped(monkeypatch, capsys, tmp_path):
    """gh 'latest' can be a years-old stable when recent releases are betas
    (2026-08-24 incident: announced AW v0.13.2 from 2024 as new)."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.13.2",
            "isPrerelease": False,
            "body": "* feat: old thing",
            "url": "u",
            "publishedAt": "2024-06-16T12:00:00Z",
        },
    )
    posted = []

    def _stub_post(a, account=None):
        posted.append(a)
        return True, "1", ""

    monkeypatch.setattr(ra, "_post", _stub_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 0
    assert posted == []
    assert "exceeds --max-age-days" in capsys.readouterr().out


def test_explicit_tag_bypasses_freshness(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.13.2",
            "isPrerelease": False,
            "body": "",
            "url": "u",
            "publishedAt": "2024-06-16T12:00:00Z",
        },
    )
    calls = []

    def _stub_post(a, account=None):
        calls.append(a)
        return True, "9", ""

    monkeypatch.setattr(ra, "_post", _stub_post)
    monkeypatch.setattr(
        sys, "argv", ["release_announce.py", "--tag", "v0.13.2", "--skip-quote"]
    )
    assert ra.main() == 0
    assert calls  # posted despite age


def test_force_bypasses_freshness_gate(monkeypatch, capsys, tmp_path):
    """--force without --tag must reach _main even when the latest release is stale.
    This is the documented recovery path for clearing pending markers from crashed runs."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.13.2",
            "isPrerelease": False,
            "body": "* feat: old thing",
            "url": "u",
            "publishedAt": "2024-06-16T12:00:00Z",  # stale: > 14 days old
        },
    )
    calls: list = []

    def _stub_post(a, account=None):
        calls.append(a)
        return True, "1", ""

    monkeypatch.setattr(ra, "_post", _stub_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py", "--force", "--skip-quote"])
    result = ra.main()
    # --force must bypass the freshness gate and attempt to post
    assert "exceeds --max-age-days" not in capsys.readouterr().out
    assert calls, "--force should have reached _main and attempted to post"
    assert result == 0


def test_naive_published_at_skips_safely(monkeypatch, capsys, tmp_path):
    """A timezone-less publishedAt must fail closed instead of crashing."""
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v1.0.0",
            "isPrerelease": False,
            "body": "",
            "url": "u",
            "publishedAt": "2024-06-16T12:00:00",  # no Z / no offset → naive datetime
        },
    )
    posted: list = []

    def _stub_post(a, account=None):
        posted.append(a)
        return True, "1", ""

    monkeypatch.setattr(ra, "_post", _stub_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])
    assert ra.main() == 0
    assert posted == [], "naive publishedAt should skip, not crash or announce"
