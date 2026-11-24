"""Peer-host contract for writer.review_main and ``python -m gptme_review.review`` (writer-port 3c)."""

from __future__ import annotations

import argparse
import subprocess

import pytest
from gptme_review import review as review_cli
from gptme_review import writer
from gptme_review.core import ReviewError
from gptme_review.sweep import sweep_identity

DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def f():
-    return 1
+    return 2
+# done
"""


def _peer(calls: list, *, pr_state: str = "OPEN"):
    io = writer.default_io(sweep_identity("alice-agent", "alice-ai-review"))

    def gh_json(cmd, *a, **kw):
        calls.append(("gh_json", tuple(cmd)))
        if cmd[:2] == ["pr", "view"]:
            return {
                "headRefOid": "a" * 40,
                "state": pr_state,
                "title": "fix: return 2",
                "body": "",
            }
        if cmd[:1] == ["api"] and cmd[1].startswith("repos/") and "/" in cmd[1][6:]:
            return {"private": False, "visibility": "public"}
        return None

    def run(cmd, **kw):
        calls.append(("run", tuple(cmd)))
        out = DIFF if cmd[:3] == ["gh", "pr", "diff"] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    io.gh_json = gh_json
    io.gh_paginated_list = lambda cmd, *a, **kw: []
    io.run = run
    return writer.bind_io(io)


def test_peer_plan_only_runs_without_brain_names(capsys):
    calls: list = []
    w = _peer(calls)
    rc = w.review_main(["alice/repo", "7", "--plan-only"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "single prompt" in out or "shard" in out
    assert any(c[0] == "run" and c[1][:3] == ("gh", "pr", "diff") for c in calls)


def test_peer_skips_closed_pr(capsys):
    w = _peer([], pr_state="CLOSED")
    assert w.review_main(["alice/repo", "7"]) == 0
    assert "is CLOSED" in capsys.readouterr().out


def test_peer_refuses_the_subscription_engine(capsys):
    w = _peer([])
    rc = w.review_main(["alice/repo", "7", "--engine", "subscription", "--plan-only"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_peer_agent_engine_fails_closed_without_posting(capsys):
    calls: list = []
    w = _peer(calls)
    with pytest.raises(ReviewError, match="host that implements it"):
        w.review_main(["alice/repo", "7", "--engine", "agent", "--passes", "1"])
    assert not any(c[0] == "run" and "POST" in c[1] for c in calls)


def test_peer_defaults_refuse_instead_of_guessing():
    with pytest.raises(ReviewError):
        writer.pick_model(None, "agent")
    assert writer.pick_model("x/y", "agent") == ("x/y", "explicit --model")
    assert writer.pick_model(None, "llm")[0] == writer.DEFAULT_MODEL
    assert writer.shard_review_key_remaining() is None
    writer.maybe_start_shadow(1, a=2)  # no-op without a host hook
    with pytest.raises(ReviewError):
        writer.run_agent_engine()
    with pytest.raises(ReviewError):
        writer.configure_subscription_plan(argparse.Namespace())


def test_omitted_host_resolves_the_bound_writer_host():
    seen = []

    class Host(writer.DefaultWriterHost):
        def extra_static_checks(self, diff, touched_files):
            seen.append("host")
            return []

    io = writer.default_io(sweep_identity("alice-agent", "alice-ai-review"), Host())
    io.gh_json = lambda *a, **kw: None
    io.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")
    w = writer.bind_io(io)
    w.static_preflight_findings(
        repo="alice/repo",
        number=7,
        pr_title="feat: x",
        pr_body="",
        diff=DIFF,
        touched_files={"app.py"},
    )
    assert seen == ["host"]


def test_host_namespace_wins_over_peer_defaults(capsys):
    """A host's names (Bob's routing, engines, ledgers) beat the peer defaults."""
    calls: list = []
    w = _peer(calls)
    io = w.review_main.__globals__["_io"]._ns
    routed = []

    def resolve_route(repo, **kwargs):
        routed.append((repo, kwargs["engine"]))
        raise ReviewError("host route refused")

    io.routing = argparse.Namespace(resolve_route=resolve_route)
    assert w.review_main(["alice/repo", "7", "--plan-only"]) == 2
    assert routed == [("alice/repo", "auto")]
    assert "host route refused" in capsys.readouterr().err


def test_cli_requires_an_explicit_identity(monkeypatch, capsys):
    monkeypatch.delenv(review_cli.LOGIN_ENV, raising=False)
    monkeypatch.delenv(review_cli.NAMESPACE_ENV, raising=False)
    assert review_cli.main(["alice/repo", "7"]) == 2
    assert "no default reviewer identity" in capsys.readouterr().err


def test_cli_rejects_a_malformed_identity(monkeypatch, capsys):
    monkeypatch.setenv(review_cli.LOGIN_ENV, "alice-agent")
    monkeypatch.setenv(review_cli.NAMESPACE_ENV, "Not A Slug")
    assert review_cli.main(["alice/repo", "7"]) == 2
    assert "marker_prefix" in capsys.readouterr().err
