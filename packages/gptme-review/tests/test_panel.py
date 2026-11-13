"""Portable panel behavior, characterized from the production host adapter."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from gptme_review import panel
from gptme_review.core import ReviewError


@pytest.fixture(autouse=True)
def _no_real_grok_key(monkeypatch, tmp_path):
    monkeypatch.setattr(panel, "GROK_AUTH_FILE", tmp_path / "no-auth.json")


CODEX_SOL = panel.PanelReviewer(
    key="codex:gpt-6.1-sol",
    family="openai",
    transport="codex",
    model="gpt-6.1-sol",
    price_backend="codex",
    price_model="gpt-6.1-sol",
    pool="chatgpt-sub",
    effort="medium",
)

CLAUDE_SONNET = panel.PanelReviewer(
    key="claude-code:sonnet",
    family="anthropic",
    transport="claude",
    model="sonnet",
    price_backend="claude-code",
    price_model="sonnet-5-5",
    pool="claude-sub",
)

GROK_46 = panel.PanelReviewer(
    key="grok-build:grok-4.6",
    family="xai",
    transport="grok",
    model="grok-4.6",
    price_backend="grok-build",
    price_model="grok-4.6",
    pool="supergrok-heavy",
    # The CLI default is ``high``: on the real contrib#1969 prompt it reasoned
    # past the 420s cap with no output; ``low`` answered 5/5 (CLI and direct
    # endpoint, 38-268s) with parseable findings (ErikBjare/bob#1423).
    effort="low",
)

COPILOT_GEMINI = panel.PanelReviewer(
    key="copilot-cli:gemini-3.8-flash",
    family="google",
    transport="copilot",
    model="gemini-3.8-flash",
    price_backend="copilot-cli",
    price_model="gemini-3.8-flash",
    pool="copilot",
    effort="low",
)


def _review_text(*findings: dict) -> str:
    return (
        "```json\n"
        + json.dumps(
            {"summary": "Changes things.", "files": [], "findings": list(findings)}
        )
        + "\n```"
    )


def _finding(
    body="`foo()` returns None when the cache is cold, so `bar` crashes", **kw
):
    return {
        "body": body,
        "file": "src/a.py",
        "line": 10,
        "security": False,
        "quote": "x = foo()",
        "verification": "",
        "class": "bug",
        "severity": "P1",
        "suggestion": "",
        **kw,
    }


def _fake_transports(monkeypatch, behaviour: dict):
    """behaviour: reviewer.key -> text | Exception."""

    def make(transport):
        def run(reviewer, prompt, timeout):
            out = behaviour[reviewer.key]
            if isinstance(out, BaseException):
                raise out
            return out, {"input_tokens": 1000, "output_tokens": 100}, None

        return run

    monkeypatch.setattr(
        panel, "TRANSPORTS", {t: make(t) for t in ("codex", "claude", "grok")}
    )


PRICES = {
    "codex": {"gpt-6.1-sol": (2.0, 10.0)},
    "claude-code": {"sonnet-5-5": (3.0, 15.0)},
    "grok-build": {"grok-4.6": (3.0, 15.0)},
}


class TestRunReviewer:
    def test_unparseable_output_is_a_failed_result_not_a_crash(self, monkeypatch):
        _fake_transports(monkeypatch, {"codex:gpt-6.1-sol": "I could not review this."})
        res = panel.run_reviewer(CODEX_SOL, "p", prices=PRICES)
        assert not res.ok and "unparseable" in res.error

    def test_transport_exception_is_a_failed_result(self, monkeypatch):
        _fake_transports(monkeypatch, {"codex:gpt-6.1-sol": ReviewError("codex 504")})
        res = panel.run_reviewer(CODEX_SOL, "p", prices=PRICES)
        assert not res.ok and "504" in res.error

    def test_success_records_cost_and_findings(self, monkeypatch):
        _fake_transports(monkeypatch, {"codex:gpt-6.1-sol": _review_text(_finding())})
        res = panel.run_reviewer(CODEX_SOL, "p", prices=PRICES)
        assert res.ok and len(res.findings) == 1
        assert res.list_price_usd == pytest.approx((1000 * 2 + 100 * 10) / 1e6)
        row = res.row()
        assert row["route"] == "subscription" and row["engine"] == "one-shot/codex"


class TestPanelReview:
    two = [CODEX_SOL, CLAUDE_SONNET]

    def test_union_with_model_agreement(self, monkeypatch):
        shared = _finding()
        only_claude = _finding(
            "`parse()` swallows the ValueError, hiding malformed config forever",
            file="src/b.py",
            line=40,
            severity="P2",
        )
        _fake_transports(
            monkeypatch,
            {
                "codex:gpt-6.1-sol": _review_text(shared),
                "claude-code:sonnet": _review_text(shared, only_claude),
            },
        )
        findings, usage = panel.panel_review("p", self.two, prices=PRICES)
        assert len(findings) == 2
        top = findings[0]
        assert top["severity"] == "P1" and top["agreement"] == 2
        assert (
            top["panel_models"] == ["claude-code", "codex"]
            or len(top["panel_models"]) == 2
        )
        assert findings[1]["agreement"] == 1
        assert usage["consensus_requested_passes"] == usage["consensus_passes"] == 2
        assert usage["consensus_min_agreement"] == 1
        assert usage["panel"]["families"] == ["anthropic", "openai"]
        assert "pass_failures" not in usage

    def test_requested_agreement_filters_single_reviewer_findings(self, monkeypatch):
        shared = _finding()
        only_claude = _finding(
            "`parse()` swallows malformed config forever",
            file="src/b.py",
            line=40,
            severity="P2",
        )
        _fake_transports(
            monkeypatch,
            {
                "codex:gpt-6.1-sol": _review_text(shared),
                "claude-code:sonnet": _review_text(shared, only_claude),
            },
        )
        findings, usage = panel.panel_review(
            "p", self.two, min_agreement=2, prices=PRICES
        )
        assert [f["body"] for f in findings] == [shared["body"]]
        assert usage["consensus_min_agreement"] == 2

    def test_agreement_cannot_exceed_panel(self):
        with pytest.raises(ReviewError, match="cannot apply 2-of-1"):
            panel.panel_review("p", [CODEX_SOL], min_agreement=2)

    def test_partial_panel_survives_and_records_who_answered(self, monkeypatch):
        _fake_transports(
            monkeypatch,
            {
                "codex:gpt-6.1-sol": _review_text(_finding()),
                "claude-code:sonnet": ReviewError("claude -p timed out after 420s"),
            },
        )
        findings, usage = panel.panel_review(
            "p", self.two, skipped={"grok-build:grok-4.6": "402"}, prices=PRICES
        )
        assert len(findings) == 1
        assert usage["panel"]["answered"] == ["codex:gpt-6.1-sol"]
        assert usage["panel"]["requested"] == [
            "codex:gpt-6.1-sol",
            "claude-code:sonnet",
        ]
        assert usage["panel"]["skipped"] == {"grok-build:grok-4.6": "402"}
        assert (
            usage["consensus_requested_passes"] == 2 and usage["consensus_passes"] == 1
        )
        assert any("timed out" in f for f in usage["pass_failures"])

    def test_nobody_answers_is_an_error(self, monkeypatch):
        _fake_transports(
            monkeypatch,
            {
                "codex:gpt-6.1-sol": ReviewError("down"),
                "claude-code:sonnet": ReviewError("down"),
            },
        )
        with pytest.raises(
            ReviewError, match="all subscription panel reviewers failed"
        ):
            panel.panel_review("p", self.two, prices=PRICES)

    def test_degraded_and_empty_is_not_a_clean_review(self, monkeypatch):
        _fake_transports(
            monkeypatch,
            {
                "codex:gpt-6.1-sol": _review_text(),
                "claude-code:sonnet": ReviewError("down"),
            },
        )
        with pytest.raises(panel.PanelReviewError, match="degraded panel") as exc:
            panel.panel_review("p", self.two, prices=PRICES)
        # The ledger must blame only the reviewer that failed, with its error:
        # blanket "failed" rows hid grok's timeout from the breaker.
        rows = {r["key"]: r for r in exc.value.reviewer_rows}
        assert rows["codex:gpt-6.1-sol"]["outcome"] == "ok"
        assert rows["claude-code:sonnet"]["outcome"] == "failed"
        assert "down" in rows["claude-code:sonnet"]["error"]

    def test_full_panel_with_no_findings_is_clean(self, monkeypatch):
        _fake_transports(
            monkeypatch,
            {"codex:gpt-6.1-sol": _review_text(), "claude-code:sonnet": _review_text()},
        )
        findings, usage = panel.panel_review("p", self.two, prices=PRICES)
        assert findings == [] and usage["consensus_passes"] == 2

    def test_empty_roster_is_an_error(self):
        with pytest.raises(ReviewError):
            panel.panel_review("p", [])


class TestTransports:
    def test_codex_reads_message_and_usage(self, monkeypatch):
        events = [
            {"type": "item.completed", "item": {"type": "error", "message": "warn"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        ]
        seen = {}

        def fake(cmd, **kw):
            seen["cmd"], seen["stdin"] = cmd, kw["stdin_text"]
            return subprocess.CompletedProcess(
                cmd, 0, "\n".join(json.dumps(e) for e in events), ""
            )

        monkeypatch.setattr(panel, "_run_bounded", fake)
        text, tokens, cost = panel.codex_transport(CODEX_SOL, "PROMPT", 5)
        assert (text, tokens["input_tokens"], cost) == ("hi", 7, None)
        assert Path(seen["cmd"][0]).name == "codex"
        assert seen["cmd"][1:4] == ["exec", "-m", "gpt-6.1-sol"]
        assert "read-only" in seen["cmd"] and seen["stdin"] == "PROMPT"

    def test_claude_is_toolless_hookless_and_reports_list_cost(self, monkeypatch):
        payload = {
            "result": "ok",
            "is_error": False,
            "total_cost_usd": 0.05,
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 5,
                "output_tokens": 4,
            },
        }
        seen = {}

        def fake(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw["env"]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setattr(panel, "_run_bounded", fake)
        text, tokens, cost = panel.claude_transport(CLAUDE_SONNET, "P", 5)
        cmd = seen["cmd"]
        assert cmd[cmd.index("--tools") + 1] == ""
        assert cmd[cmd.index("--setting-sources") + 1] == "project"
        assert "--no-session-persistence" not in cmd
        assert "CLAUDECODE" not in seen["env"]
        assert (text, cost, tokens["input_tokens"], tokens["output_tokens"]) == (
            "ok",
            0.05,
            17,
            4,
        )

    def test_claude_error_payload_raises(self, monkeypatch):
        monkeypatch.setattr(
            panel,
            "_run_bounded",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 1, json.dumps({"is_error": True, "result": "rate limited"}), ""
            ),
        )
        with pytest.raises(ReviewError, match="rate limited"):
            panel.claude_transport(CLAUDE_SONNET, "P", 5)

    def test_grok_is_toolless_and_gets_more_than_one_turn(self, monkeypatch):
        """Empty ``--tools`` plus one turn kills grok on large review prompts.

        Stock grok profiles inject tools before the allowlist, so ``--tools ""``
        still leaves read/grep/MCP. Combined with ``--max-turns 1`` that is the
        0/42 ledger signature and the 2026-10-21 degraded-panel fallback outage.
        """
        seen = {}

        def fake(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(panel, "_run_bounded", fake)
        text, _tokens, _cost = panel.grok_cli_transport(GROK_46, "P", 5)
        cmd = seen["cmd"]
        assert text == "ok"
        assert "--tools" not in cmd
        denied = cmd[cmd.index("--disallowed-tools") + 1]
        assert "read_file" in denied and "Agent" in denied
        assert int(cmd[cmd.index("--max-turns") + 1]) >= 2
        assert "--no-plan" in cmd and "--disable-web-search" in cmd
        assert panel.GROK_MAX_TURNS >= 2
        # Default (high) effort reasons past the 420s cap on real prompts.
        assert cmd[cmd.index("--reasoning-effort") + 1] == "low"

    def test_grok_removes_mcp_and_current_tool_ids(self, monkeypatch):
        """The failed 2026-10-22 sweep called search_tool/use_tool and write."""
        seen = {}

        def fake(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(panel, "_run_bounded", fake)
        panel.grok_cli_transport(GROK_46, "P", 5)
        cmd = seen["cmd"]
        denied = set(cmd[cmd.index("--disallowed-tools") + 1].split(","))
        assert {"search_tool", "use_tool", "write", "bash", "grep_search"} <= denied
        # Tool removal prevents turn-wasting; deny-all also gates any new ID
        # introduced by a CLI upgrade or a user/plugin configuration.
        assert cmd[cmd.index("--deny") + 1] == "*"
        # Large prompts otherwise become excerpts plus a file-read instruction,
        # which a tool-free reviewer cannot satisfy.
        assert "--verbatim" in cmd

    def test_grok_removes_plan_media_and_backend_tools(self, monkeypatch):
        """ErikBjare/bob#1423: plan/media stand-ins and backend X search hung at 420s."""
        seen = {}

        def fake(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw.get("env")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(panel, "_run_bounded", fake)
        panel.grok_cli_transport(GROK_46, "P", 5)
        cmd = seen["cmd"]
        denied = set(cmd[cmd.index("--disallowed-tools") + 1].split(","))
        assert {
            "enter_plan_mode",
            "exit_plan_mode",
            "image_edit",
            "image_to_video",
            "reference_to_video",
        } <= denied
        assert seen["env"]["GROK_BACKEND_SEARCH"] == "0"
        assert "PATH" in seen["env"]

    def test_grok_error_payload_raises(self, monkeypatch):
        monkeypatch.setattr(
            panel,
            "_run_bounded",
            lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 1, "", "Max turns reached Error: max turns reached"
            ),
        )
        with pytest.raises(ReviewError, match="max turns reached"):
            panel.grok_cli_transport(GROK_46, "P", 5)

    def test_resolve_bin_falls_back_when_path_is_minimal(self, monkeypatch, tmp_path):
        (tmp_path / "fakecli").write_text("#!/bin/sh\n")
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr(panel, "_BIN_FALLBACK_DIRS", (tmp_path,))
        assert panel._resolve_bin("fakecli") == str(tmp_path / "fakecli")
        assert panel._resolve_bin("nothere") == "nothere"

    def test_run_bounded_kills_on_timeout(self):
        with pytest.raises(ReviewError, match="timed out"):
            panel._run_bounded(["sleep", "30"], stdin_text=None, timeout=0.3)


class TestGrokDirectTransport:
    def write_auth(self, path, expires):
        path.write_text(
            json.dumps({"acct": {"key": "K", "expires_at": expires.isoformat()}})
        )

    def test_key_used_only_when_it_outlives_the_review(self, monkeypatch, tmp_path):
        auth = tmp_path / "auth.json"
        monkeypatch.setattr(panel, "GROK_AUTH_FILE", auth)
        now = datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
        assert panel._grok_direct_key(420, now) is None  # missing file
        self.write_auth(auth, now + timedelta(hours=3))
        assert panel._grok_direct_key(420, now) == "K"
        # Expiring mid-review: the CLI must refresh it, never this transport.
        self.write_auth(auth, now + timedelta(seconds=600))
        assert panel._grok_direct_key(420, now) is None
        auth.write_text("{not json")
        assert panel._grok_direct_key(420, now) is None

    def test_stale_key_falls_back_to_cli(self, monkeypatch):
        monkeypatch.setattr(
            panel, "grok_cli_transport", lambda r, p, t: ("cli", {}, None)
        )
        assert panel.grok_transport(GROK_46, "P", 5)[0] == "cli"

    def stream(self, monkeypatch, events, stall=False):
        seen = {}
        closed = threading.Event()

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def close(self):
                closed.set()

            def __iter__(self):
                for e in events:
                    yield b"data: " + json.dumps(e).encode() + b"\n"
                if stall:  # server holds the connection open, sends nothing
                    closed.wait(5)
                    raise ValueError("I/O operation on closed file")
                yield b"data: [DONE]\n"

        def urlopen(request, timeout):
            seen["body"] = json.loads(request.data)
            seen["headers"] = dict(request.header_items())
            return Resp()

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(panel, "_grok_client_version", lambda: "1.0.46")
        return seen

    def test_direct_streams_low_effort_capped_answer(self, monkeypatch):
        seen = self.stream(
            monkeypatch,
            [
                {"choices": [{"delta": {"reasoning_content": "hm"}}]},
                {"choices": [{"delta": {"content": "```json\n{}"}}]},
                {"choices": [{"delta": {"content": "\n```"}, "finish_reason": "stop"}]},
            ],
        )
        text, tokens, cost = panel.grok_direct_transport(GROK_46, "PROMPT", 5, "K")
        assert text == "```json\n{}\n```"
        assert cost is None and tokens["tokens_estimated"] == 1
        body = seen["body"]
        assert body["reasoning_effort"] == "low"
        assert body["max_tokens"] == panel.GROK_DIRECT_MAX_TOKENS
        assert body["messages"][-1] == {"role": "user", "content": "PROMPT"}
        assert seen["headers"]["X-grok-client-version"] == "1.0.46"
        assert seen["headers"]["Authorization"] == "Bearer K"

    def test_direct_truncation_and_empty_answer_fail(self, monkeypatch):
        self.stream(
            monkeypatch,
            [{"choices": [{"delta": {"content": "x"}, "finish_reason": "length"}]}],
        )
        with pytest.raises(ReviewError, match="max_tokens"):
            panel.grok_direct_transport(GROK_46, "P", 5, "K")
        self.stream(
            monkeypatch, [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        )
        with pytest.raises(ReviewError, match="no content"):
            panel.grok_direct_transport(GROK_46, "P", 5, "K")

    def test_direct_deadline_is_a_wall_clock_timeout(self, monkeypatch):
        """The 24h timeout breaker must still recognise a direct-path hang."""
        self.stream(monkeypatch, [{"choices": [{"delta": {"content": "x"}}]}] * 3)
        with pytest.raises(ReviewError) as exc:
            panel.grok_direct_transport(GROK_46, "P", -1, "K")
        assert "timed out" in str(exc.value)

    def test_stalled_stream_is_bounded_and_trips_the_breaker(self, monkeypatch):
        """A silent open connection must not outlive the wall-clock budget."""
        self.stream(
            monkeypatch, [{"choices": [{"delta": {"content": "x"}}]}], stall=True
        )
        started = time.monotonic()
        with pytest.raises(ReviewError) as exc:
            panel.grok_direct_transport(GROK_46, "P", 0.3, "K")
        assert time.monotonic() - started < 3
        assert "timed out" in str(exc.value)

    def test_client_version_failure_is_a_review_error(self, monkeypatch):
        monkeypatch.setattr(panel, "_resolve_bin", lambda name: "/nonexistent/grok")
        with pytest.raises(ReviewError, match="grok --version failed"):
            panel._grok_client_version()


def test_portable_import_without_brain(tmp_path):
    import os
    import subprocess

    package_src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(package_src)}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gptme_review import panel; import sys; assert not any(n.startswith(('ai_review_', 'openrouter_keys')) for n in sys.modules); assert not hasattr(panel, 'REPO_ROOT'); assert panel.load_prices() == {}",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_price_table_is_explicit(tmp_path):
    path = tmp_path / "prices.toml"
    path.write_text('[prices.codex]\n"gpt-6.1-sol" = [2.0, 10.0]\n')
    assert (
        panel.list_price_usd(
            CODEX_SOL,
            {"input_tokens": 1000, "output_tokens": 100},
            panel.load_prices(path),
        )
        == 0.003
    )
    assert panel.list_price_usd(CODEX_SOL, {"input_tokens": 1000}) is None


class TestCopilotTransport:
    def checkpoint(self, tool_count=0):
        return {
            "type": "session.usage_checkpoint",
            "data": {
                "promptCacheBreakState": [
                    {"models": {"gemini-3.8-flash": {"tool_count": tool_count}}}
                ],
                "accountingSnapshot": {
                    "tokenDetails": {
                        "input": {"tokenCount": 11},
                        "output": {"tokenCount": 7},
                    }
                },
                "totalNanoAiu": 2300000000,
            },
        }

    def fake_transport(self, monkeypatch, events, rc=0):
        seen: dict[str, Any] = {}

        def fake(cmd, **kw):
            seen.update(cmd=cmd, **kw)
            return subprocess.CompletedProcess(
                cmd, rc, "\n".join(json.dumps(e) for e in events), "error"
            )

        monkeypatch.setattr(panel, "_run_bounded", fake)
        monkeypatch.setattr(panel, "_resolve_bin", lambda name: name)
        return seen

    def test_transport_stdin_low_effort_zero_tools_and_credits_not_usd(
        self, monkeypatch
    ):
        seen = self.fake_transport(
            monkeypatch,
            [
                {"type": "assistant.message", "data": {"content": _review_text()}},
                self.checkpoint(),
            ],
        )
        result = panel.run_reviewer(COPILOT_GEMINI, "PROMPT", timeout=5)
        assert result.ok, result.error
        assert seen["stdin_text"] == "PROMPT" and "PROMPT" not in seen["cmd"]
        assert seen["timeout"] == 5
        assert "--available-tools=__none__" in seen["cmd"]
        assert "--disable-builtin-mcps" in seen["cmd"]
        assert "--no-custom-instructions" in seen["cmd"]
        assert seen["cmd"][seen["cmd"].index("--reasoning-effort") + 1] == "low"
        assert result.tokens["tool_count"] == 0
        assert (
            result.tokens["input_tokens"] == 11 and result.tokens["output_tokens"] == 7
        )
        assert result.tokens["copilot_credits"] == pytest.approx(2.3)
        assert result.list_price_usd is None
        assert result.row()["tokens"]["copilot_credits"] == pytest.approx(2.3)

    @pytest.mark.parametrize("checkpoints", [[], [None], [1], [False], ["0"], [1, 0]])
    def test_no_zero_tool_attestation_is_failed_reviewer(
        self, monkeypatch, checkpoints
    ):
        self.fake_transport(
            monkeypatch,
            [
                {"type": "assistant.message", "data": {"content": _review_text()}},
                *(self.checkpoint(n) for n in checkpoints),
            ],
        )
        result = panel.run_reviewer(COPILOT_GEMINI, "P", timeout=5)
        assert not result.ok and "tool" in result.error.lower()

    @pytest.mark.parametrize(
        "rc,content", [(1, _review_text()), (0, ""), (0, "not json")]
    )
    def test_transport_and_parse_failures_do_not_vote(self, monkeypatch, rc, content):
        self.fake_transport(
            monkeypatch,
            [
                {"type": "assistant.message", "data": {"content": content}},
                self.checkpoint(),
            ],
            rc=rc,
        )
        result = panel.run_reviewer(COPILOT_GEMINI, "P", timeout=5)
        assert not result.ok

    @pytest.mark.parametrize(
        "states",
        [
            None,
            [],
            {},
            [None],
            [{}],
            [{"models": {}}],
            [{"models": {"gemini-3.8-flash": []}}],
        ],
    )
    def test_malformed_checkpoint_fails_closed(self, monkeypatch, states):
        checkpoint = self.checkpoint()
        checkpoint["data"]["promptCacheBreakState"] = states
        self.fake_transport(
            monkeypatch,
            [
                {"type": "assistant.message", "data": {"content": _review_text()}},
                checkpoint,
            ],
        )
        result = panel.run_reviewer(COPILOT_GEMINI, "P", timeout=5)
        assert not result.ok and "tool" in result.error.lower()


class TestPureUtilities:
    def test_route_uses_only_supplied_admission_and_counts(self):
        common: dict[str, Any] = dict(
            mode="primary",
            available=[CODEX_SOL],
            sensitive_repo=True,
            key_remaining=5.0,
            public_headroom_usd=None,
            primary_daily_max=2,
        )
        assert panel.choose_primary_route(**common)[0] == "subscription"
        assert (
            panel.choose_primary_route(**common, primary_runs_today=2)[0]
            == "openrouter"
        )
        assert (
            panel.choose_primary_route(**{**common, "available": []})[0] == "openrouter"
        )
        assert (
            panel.choose_primary_route(**{**common, "mode": "shadow"})[0]
            == "openrouter"
        )
        assert (
            panel.choose_primary_route(
                **{**common, "sensitive_repo": False, "public_headroom_usd": 3.0}
            )[0]
            == "openrouter"
        )

    def test_headroom_parsing(self):
        assert panel.parse_headroom({}) is None
        assert panel.parse_headroom({panel.PUBLIC_HEADROOM_ENV: "nonsense"}) is None
        assert panel.parse_headroom({panel.PUBLIC_HEADROOM_ENV: " 1.5 "}) == 1.5

    def test_cap_projection_has_no_persistence(self):
        now = datetime(2026, 11, 13, tzinfo=UTC)
        record = panel.panel_cap_shadow_record(
            repo="owner/repo",
            number=1,
            sha="a" * 40,
            panel=[CODEX_SOL, CLAUDE_SONNET],
            available=[CODEX_SOL, CLAUDE_SONNET, GROK_46],
            now=now,
        )
        assert record["actual_panel"] == [CODEX_SOL.key, CLAUDE_SONNET.key]
        assert record["shadow_panel"] == [CLAUDE_SONNET.key]
        assert record["families_free_for_gate_under_cap"] == ["openai", "xai"]
        assert record["ts"] == now.isoformat()

    def test_openrouter_row_cost_is_not_subscription_credit(self):
        row = panel.openrouter_reviewer_row(
            "openrouter/vendor/model", {"cost": 0.05, "prompt_tokens": 3}, 1.234
        )
        assert row["route"] == "openrouter" and row["billed_usd"] == 0.05
        assert row["model"] == "vendor/model" and row["wall_s"] == 1.23
        assert (
            panel.openrouter_reviewer_row("vendor/model", {}, 0)["billed_usd"] is None
        )

    def test_compaction_retains_executor_provenance(self):
        provenance = [{"model": CODEX_SOL.key, "pass": 1, "aspect": "general"}]
        finding = panel.compact_finding(
            _finding(
                body="b" * 2000,
                quote="q" * 400,
                provenance=provenance,
                panel_models=[CODEX_SOL.key],
                agreement=1,
            )
        )
        assert len(finding["body"]) == 1500 and len(finding["quote"]) == 300
        assert finding["provenance"] == provenance and finding["models"] == [
            CODEX_SOL.key
        ]


def test_panel_stamps_transport_provenance(monkeypatch):
    forged = [{"model": "other:model", "pass": 9, "aspect": "security"}]
    _fake_transports(
        monkeypatch, {CODEX_SOL.key: _review_text(_finding(provenance=forged))}
    )
    findings, _ = panel.panel_review("p", [CODEX_SOL], prices=PRICES)
    assert findings[0]["provenance"] == [
        {"model": CODEX_SOL.key, "pass": 1, "aspect": "general"}
    ]


def test_threads_inherit_host_lifecycle_context(monkeypatch):
    from contextvars import ContextVar

    marker: ContextVar[str | None] = ContextVar("test_panel_context", default=None)
    seen = []
    monkeypatch.setattr(
        panel.lifecycle,
        "stage",
        lambda stage, **kw: seen.append((stage, marker.get(), kw["attempt"])),
    )
    _fake_transports(monkeypatch, {CODEX_SOL.key: _review_text()})
    token = marker.set("host-run")
    try:
        panel.panel_review("p", [CODEX_SOL], prices=PRICES)
    finally:
        marker.reset(token)
    assert seen == [("subscription_transport", "host-run", 1)]


def test_panel_join_deadline_records_abandoned_reviewer(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        release.wait(2)
        return panel.ReviewerResult(CODEX_SOL, True)

    class Deadline(panel.ReviewDeadline):
        def remaining(self, stage: str, *, minimum_s: float = 0) -> float:
            assert entered.wait(1)
            return 0.01

    monkeypatch.setattr(panel, "run_reviewer", slow)
    try:
        with pytest.raises(panel.PanelReviewError) as exc:
            panel.panel_review("p", [CODEX_SOL], prices=PRICES, deadline=Deadline(1))
        row = exc.value.reviewer_rows[0]
        assert (
            row["outcome"] == "failed" and row["error"] == "abandoned at panel deadline"
        )
    finally:
        release.set()


@pytest.mark.parametrize("raw", ["", "0", "-1", "garbage"])
def test_grok_turn_limit_keeps_safe_default(monkeypatch, raw):
    monkeypatch.setenv("AI_REVIEW_GROK_MAX_TURNS", raw)
    assert panel._positive_int_env("AI_REVIEW_GROK_MAX_TURNS", 3) == 3
