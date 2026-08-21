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


def test_post_distinguishes_failure_from_success_without_id(monkeypatch):
    monkeypatch.setattr(
        ra,
        "_run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "failed"),
    )
    assert ra._post(["post", "hello"]) == (False, None)

    monkeypatch.setattr(
        ra,
        "_run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "posted", ""),
    )
    assert ra._post(["post", "hello"]) == (True, None)


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
        },
    )
    calls: list[tuple] = []

    def fake_post(args, account=None):
        calls.append((account, args))
        return True, str(100 + len(calls))

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
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
        },
    )
    calls: list[tuple] = []
    results = iter([(True, "101"), (False, None), (True, "102"), (True, "103")])

    def fake_post(args, account=None):
        calls.append((account, args))
        return next(results)

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 1
    partial = json.loads(ra.STATE_FILE.read_text())["gptme/gptme#v0.33.0"]
    assert partial == {"org_tweet_id": "101"}

    assert ra.main() == 0
    assert len(calls) == 4
    # Resume at the missing reply instead of duplicating the org announcement.
    assert calls[2][1][2:] == ["--reply-to", "101"]
    completed = json.loads(ra.STATE_FILE.read_text())["gptme/gptme#v0.33.0"]
    assert completed["link_reply_id"] == "102"
    assert completed["bob_quote_id"] == "103"
    assert "announced_at" in completed


def test_main_resumes_after_quote_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
        },
    )
    calls: list[tuple] = []
    results = iter([(True, "101"), (True, "102"), (False, None), (True, "103")])

    def fake_post(args, account=None):
        calls.append((account, args))
        return next(results)

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 1
    assert ra.main() == 0
    assert len(calls) == 4
    assert calls[-1][1][2:] == ["--quote", "101"]


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
        },
    )
    calls: list[tuple] = []

    def fake_post(args, account=None):
        calls.append((account, args))
        return True, str(100 + len(calls))

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


def test_post_default_account_clears_environment_profile(monkeypatch):
    monkeypatch.setenv("TWITTER_ACCOUNT", "gptmeorg")
    for var in ra._USER_CONTEXT_VARS:
        monkeypatch.setenv(var, f"org-{var.lower()}")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="Tweet ID: 123\n", stderr="")

    monkeypatch.setattr(ra, "_run", fake_run)

    assert ra._post(["post", "hello"]) == (True, "123")
    assert "TWITTER_ACCOUNT" not in seen["env"]
    assert not set(ra._USER_CONTEXT_VARS) & seen["env"].keys()


def test_post_named_account_keeps_inherited_environment(monkeypatch):
    monkeypatch.setenv("TWITTER_ACCOUNT", "default")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="Tweet ID: 123\n", stderr="")

    monkeypatch.setattr(ra, "_run", fake_run)

    assert ra._post(["post", "hello"], account="gptmeorg") == (True, "123")
    assert seen["cmd"][2:4] == ["--account", "gptmeorg"]
    assert seen["env"] is None


def test_main_does_not_retry_success_without_tweet_id(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
        },
    )
    calls = 0

    def fake_post(args, account=None):
        nonlocal calls
        calls += 1
        return True, None

    monkeypatch.setattr(ra, "_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 1
    assert ra.main() == 1
    assert calls == 1
    assert ra.load_state()["gptme/gptme#v0.33.0"]["org_tweet_id"] is None


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
        },
    )
    calls: list[tuple] = []
    results = iter([(True, "101"), (True, None), (True, "103")])

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
        return True, str(len(events))

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
        return True, str(100 + len(record))

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


def test_main_clears_intent_after_definite_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ra, "STATE_FILE", tmp_path / "ra.json")
    monkeypatch.setattr(
        ra,
        "latest_release",
        lambda repo, tag: {
            "tagName": "v0.33.0",
            "isPrerelease": False,
            "body": NOTES,
            "url": "https://github.com/gptme/gptme/releases/tag/v0.33.0",
        },
    )
    results = iter([(False, None), (True, "101"), (True, "102"), (True, "103")])
    monkeypatch.setattr(ra, "_post", lambda args, account=None: next(results))
    monkeypatch.setattr(sys, "argv", ["release_announce.py"])

    assert ra.main() == 1
    assert ra.load_state()["gptme/gptme#v0.33.0"] == {}
    assert ra.main() == 0
