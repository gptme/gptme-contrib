"""Tests for ``gptme-runloops select --lane`` (lane policy, effort step-down)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from gptme_runloops import select_usage
from gptme_runloops.cli import main
from gptme_runloops.select_lane import select_lane, validate_policy
from gptme_runloops.select_policy import (
    LaneRow,
    PoolPolicy,
    parse_arm,
    parse_routing,
    policy_version,
)
from gptme_runloops.select_rules import (
    compute_effort,
    format_shell,
    pair_exclusions,
)
from gptme_runloops.select_usage import (
    PoolReading,
    parse_claude_usage,
    read_pool_usage,
)

LANE_ARM = "claude-code:claude-opus-5-5"
FALLBACK_ARM = "gptme:gemini/gemini-flash-latest"

POLICY = """
[routing]
schema_version = 1

[pools.claude-max]
provider = "fake"
owner_reserve = 0.10
no_capacity_at = 0.90
effort_steps = [{ at = 0.60, down = 1 }, { at = 0.85, to = "low" }]
pace_guard = 0.10

[lanes.autonomous]
arm = "claude-code:claude-opus-5-5"
effort = "high"
effort_ladder = ["high", "medium", "low"]
pool = "claude-max"
expires = 2099-01-01
fallback = [
  { arm = "gptme:gemini/gemini-flash-latest", scope = "lights-on", on = ["auth", "blocked"] },
]

[lanes.autonomous.routine]
effort = "medium"
"""


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def reading() -> Iterator[Callable[..., None]]:
    """Install a fake usage provider named ``fake``; call it to set the reading."""
    state: dict[str, object] = {"reading": PoolReading(source="fake")}

    def provider(options: object) -> PoolReading:
        value = state["reading"]
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, PoolReading)
        return value

    def set_reading(
        used: float | None = None,
        pace_gap: float | None = None,
        raises: Exception | None = None,
    ) -> None:
        state["reading"] = raises or PoolReading(used, pace_gap, 10.0, "fake")

    with patch.dict(select_usage.PROVIDERS, {"fake": provider}):
        yield set_reading


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, object]]:
    """All binaries present, no blocks; returns knobs tests can change."""
    knobs: dict[str, object] = {"missing": set(), "blocked": {}}

    def binary(backend: str) -> bool:
        return backend not in knobs["missing"]  # type: ignore[operator]

    def block(state_dir: Path, backend: str, model: str, pool: str | None):
        return knobs["blocked"].get(f"{backend}:{model}")  # type: ignore[attr-defined]

    with (
        patch("gptme_runloops.select._binary_available", binary),
        patch("gptme_runloops.select_lane._lane_block", block),
    ):
        yield knobs


def write_config(tmp_path: Path, text: str = POLICY) -> Path:
    path = tmp_path / "harness-quota.toml"
    path.write_text(text)
    return path


def run(tmp_path: Path, lane: str = "autonomous", **kwargs):
    if "config_path" not in kwargs:
        kwargs["config_path"] = write_config(tmp_path)
    kwargs.setdefault("state_dir", tmp_path / "state")
    kwargs.setdefault("today", date(2026, 11, 12))
    return select_lane(lane, **kwargs)


def json_line(output: str) -> dict:
    lines = [ln for ln in output.splitlines() if ln.startswith("{")]
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Effort step-down (rules lane / lane_pressure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("used", "gap", "effort", "rule"),
    [
        (0.0, None, "high", "lane"),
        (0.59, 0.0, "high", "lane"),
        (0.60, None, "medium", "lane_pressure"),
        (0.84, None, "medium", "lane_pressure"),
        (0.85, None, "low", "lane_pressure"),
        (0.89, None, "low", "lane_pressure"),
        (0.30, 0.10, "high", "lane"),  # guard is strict: exactly 0.10 is fine
        (0.30, 0.11, "medium", "lane_pressure"),  # pace guard: one step
        (0.70, 0.30, "medium", "lane_pressure"),  # max, not sum
        (0.86, 0.30, "low", "lane_pressure"),
        (None, 0.50, "high", "lane"),  # usage unknown: base effort
    ],
)
def test_effort_thresholds(tmp_path, reading, env, used, gap, effort, rule):
    reading(used, gap)
    out = run(tmp_path)
    assert out.exit_code == 0
    d = out.decision
    assert (d["effort"], d["rule"]) == (effort, rule), d["reason"]
    assert (d["backend"], d["model"]) == ("claude-code", "claude-opus-5-5")
    assert d["pool"]["name"] == "claude-max" and d["pool"]["source"] == "fake"


def test_lane_pressure_reason_names_threshold(tmp_path, reading, env):
    reading(0.67, 0.04)
    d = run(tmp_path).decision
    assert d["reason"] == "weekly 0.67 >= 0.60: high -> medium"
    assert d["pool"]["used"] == 0.67 and d["pool"]["pace_gap"] == 0.04


def test_compute_effort_never_steps_up():
    pool = PoolPolicy("p", effort_steps=parse_routing_steps())
    effort, _ = compute_effort("low", ("low",), pool, PoolReading(0.95, 0.5))
    assert effort == "low"


def parse_routing_steps():
    raw = {
        "routing": {"schema_version": 1},
        "pools": {"p": {"effort_steps": [{"at": 0.6, "down": 1}]}},
    }
    return parse_routing(raw, date(2026, 1, 1)).pools["p"].effort_steps


def test_class_row_overrides_lane_row(tmp_path, reading, env):
    reading(0.67)
    d = run(tmp_path, cls="routine").decision
    assert d["class"] == "routine"
    assert d["effort"] == "low"  # medium base, one step down


def test_unknown_class_falls_back_to_lane_row(tmp_path, reading, env):
    reading(0.1)
    d = run(tmp_path, cls="nonexistent").decision
    assert d["class"] is None and d["effort"] == "high" and d["rule"] == "lane"


def test_expired_row_still_used(tmp_path, reading, env):
    reading(0.95)  # also: an expired row never changes no_capacity semantics
    cfg = write_config(tmp_path, POLICY.replace("2099-01-01", "2020-01-01"))
    out = run(tmp_path, config_path=cfg)
    assert out.decision["policy_expired"] is True
    reading(0.1)
    out = run(tmp_path, config_path=cfg)
    assert out.decision["rule"] == "lane" and out.decision["policy_expired"] is True
    lines, code = validate_policy(cfg, today=date(2026, 11, 12))
    assert code == 0 and any("expired" in ln for ln in lines)


# ---------------------------------------------------------------------------
# Fallback, exclusions, no_capacity
# ---------------------------------------------------------------------------


def test_fallback_on_block(tmp_path, reading, env):
    reading(0.1)
    env["blocked"] = {LANE_ARM: "blocked until 2099-01-01T00:00:00+00:00"}
    out = run(tmp_path)
    d = out.decision
    assert out.exit_code == 0
    assert (d["rule"], d["backend"], d["scope"]) == ("fallback", "gptme", "lights-on")
    assert d["skipped"][0]["arm"] == LANE_ARM
    assert "blocked until" in d["skipped"][0]["why"]


def test_fallback_on_missing_binary(tmp_path, reading, env):
    reading(0.1)
    env["missing"] = {"claude-code"}
    cfg = write_config(tmp_path, POLICY.replace(', on = ["auth", "blocked"]', ""))
    d = run(tmp_path, config_path=cfg).decision
    assert d["rule"] == "fallback" and d["model"] == "gemini/gemini-flash-latest"
    assert "not found" in d["skipped"][0]["why"]


def test_missing_binary_not_in_on_list_is_no_arm(tmp_path, reading, env):
    reading(0.1)
    env["missing"] = {"claude-code"}
    out = run(tmp_path)
    assert out.exit_code == 69
    assert (out.decision["rule"], out.decision["trigger"]) == ("no_arm", "missing")


def test_fallback_skipped_when_its_binary_missing(tmp_path, reading, env):
    reading(0.1)
    env["blocked"] = {LANE_ARM: "blocked"}
    env["missing"] = {"gptme"}
    out = run(tmp_path)
    assert out.decision["rule"] == "no_arm" and out.exit_code == 69
    assert out.decision["trigger"] == "blocked"
    assert [s["arm"] for s in out.decision["skipped"]] == [LANE_ARM, FALLBACK_ARM]


def test_excluded_fallback_matching_reason(tmp_path, reading, env):
    reading(0.1)
    out = run(tmp_path, excluded={LANE_ARM: "auth"})
    d = out.decision
    assert out.exit_code == 0 and d["trigger"] == "auth"
    assert (d["rule"], d["backend"], d["scope"]) == (
        "excluded_fallback",
        "gptme",
        "lights-on",
    )


def test_unknown_reason_treated_as_unspecified(tmp_path, reading, env):
    reading(0.1)
    cfg = write_config(
        tmp_path, POLICY.replace('["auth", "blocked"]', '["rate_limited"]')
    )
    out = run(tmp_path, config_path=cfg, excluded={LANE_ARM: "rate-limited"})
    d = out.decision
    assert (d["rule"], d["trigger"], out.exit_code) == ("no_arm", "unspecified", 69)
    assert "unknown --reason 'rate-limited' treated as unspecified" in d["reason"]
    out = run(tmp_path, config_path=cfg, excluded={LANE_ARM: "rate_limited"})
    assert out.decision["rule"] == "excluded_fallback"
    assert "unknown --reason" not in out.decision["reason"]


def test_excluded_fallback_non_matching_reason(tmp_path, reading, env):
    reading(0.1)
    out = run(tmp_path, excluded={LANE_ARM: "crash"})
    assert out.exit_code == 69 and out.decision["rule"] == "no_arm"
    assert out.decision["trigger"] == "crash"
    assert "does not match crash" in out.decision["skipped"][1]["why"]


def test_excluded_fallback_arm_is_skipped(tmp_path, reading, env):
    reading(0.1)
    out = run(tmp_path, excluded={LANE_ARM: "auth", FALLBACK_ARM: "auth"})
    assert out.decision["rule"] == "no_arm" and out.exit_code == 69


def test_no_capacity_exit_76(tmp_path, reading, env):
    reading(0.90)
    out = run(tmp_path)
    assert out.exit_code == 76
    assert out.decision["rule"] == "no_capacity" and out.decision["backend"] is None
    assert out.decision["trigger"] == "no_capacity"


def test_lane_arm_chosen_has_null_trigger(tmp_path, reading, env):
    reading(0.67)
    assert run(tmp_path).decision["trigger"] is None


def test_owner_reserve_sets_ceiling_when_no_capacity_at_unset(tmp_path, reading, env):
    reading(0.91)
    cfg = write_config(tmp_path, POLICY.replace("no_capacity_at = 0.90\n", ""))
    assert run(tmp_path, config_path=cfg).exit_code == 76  # 1 - 0.10


def test_no_capacity_uses_fallback_whose_on_matches(tmp_path, reading, env):
    reading(0.95)
    cfg = write_config(tmp_path, POLICY.replace('"blocked"]', '"no_capacity"]'))
    d = run(tmp_path, config_path=cfg).decision
    assert d["rule"] == "fallback" and d["backend"] == "gptme"
    assert d["trigger"] == "no_capacity"


def test_unknown_usage_never_no_capacity(tmp_path, reading, env):
    reading(None)
    out = run(tmp_path)
    assert out.exit_code == 0 and out.decision["rule"] == "lane"


def test_pair_exclusions():
    assert pair_exclusions(["a", "b"], ["auth"]) == {"a": "auth", "b": "auth"}
    assert pair_exclusions(["a", "b"], ["auth", "crash"]) == {
        "a": "auth",
        "b": "crash",
    }
    assert pair_exclusions(["a"], []) == {"a": None}


# ---------------------------------------------------------------------------
# Fail open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "this is [[[ not toml",
        POLICY.replace("schema_version = 1", "schema_version = 2"),
        POLICY.replace("[routing]\nschema_version = 1\n", ""),
        POLICY.replace("claude-opus-5-5", "opus"),
    ],
    ids=["corrupt", "schema-2", "no-schema", "bare-alias"],
)
def test_fail_open_with_default_arm(tmp_path, reading, env, text):
    reading(0.1)
    out = run(
        tmp_path,
        config_path=write_config(tmp_path, text),
        default_arm="claude-code:claude-opus-5-5",
        default_effort="high",
    )
    d = out.decision
    assert out.exit_code == 0 and out.error
    assert (d["rule"], d["backend"], d["effort"]) == (
        "fail_open",
        "claude-code",
        "high",
    )


def test_fail_open_without_default_arm_exits_2(tmp_path, reading, env):
    out = run(tmp_path, config_path=write_config(tmp_path, "[[[ nope"))
    assert out.exit_code == 2 and out.decision["backend"] is None


def test_fail_open_unknown_lane(tmp_path, reading, env):
    out = run(tmp_path, lane="nope", default_arm=LANE_ARM)
    assert out.decision["rule"] == "fail_open" and "unknown lane" in out.error


def test_fail_open_unexpected_exception(tmp_path, reading, env):
    with patch(
        "gptme_runloops.select_lane.choose_arm", side_effect=RuntimeError("boom")
    ):
        out = run(tmp_path, default_arm=LANE_ARM, default_effort="high")
    assert out.decision["rule"] == "fail_open" and "boom" in out.error


def test_errors_in_other_lane_do_not_stop_this_lane(tmp_path, reading, env):
    reading(0.1)
    cfg = write_config(tmp_path, POLICY + '\n[lanes.broken]\narm = "nocolon"\n')
    assert run(tmp_path, config_path=cfg).decision["rule"] == "lane"
    assert run(tmp_path, lane="broken", config_path=cfg).exit_code == 2


def test_provider_raising_reads_as_unknown(tmp_path, reading, env):
    reading(raises=RuntimeError("scrape failed"))
    out = run(tmp_path)
    d = out.decision
    assert out.exit_code == 0 and out.error is None
    assert (d["rule"], d["effort"]) == ("lane", "high")
    assert d["pool"]["used"] is None and "scrape failed" in d["reason"]


def test_log_dir_unwritable_is_not_an_error(tmp_path, reading, env, capsys):
    reading(0.1)
    blocker = tmp_path / "file"
    blocker.write_text("")
    out = run(tmp_path, log_path=blocker / "sub" / "decisions.jsonl")
    assert out.exit_code == 0 and out.decision["rule"] == "lane"
    assert "decision log not written" in capsys.readouterr().err


def test_block_registry_missing_fails_open(tmp_path):
    from gptme_runloops.select_lane import _lane_block

    (tmp_path / "claude-code-blocked-until.txt").write_text("2099-01-01T00:00:00+00:00")
    mods = (
        "gptme_block_registry",
        "gptme_block_registry.contract",
        "gptme_block_registry.writers",
    )
    with patch.dict(sys.modules, dict.fromkeys(mods)):
        assert _lane_block(tmp_path, "claude-code", "m", "pool") is None


def test_block_registry_pool_block(tmp_path):
    pytest.importorskip("gptme_block_registry")
    from gptme_runloops.select_lane import _lane_block

    (tmp_path / "pool-claude-max-blocked-until.txt").write_text(
        "2099-01-01T00:00:00+00:00"
    )
    assert _lane_block(tmp_path, "claude-code", "m", None) is None
    assert "2099" in (_lane_block(tmp_path, "claude-code", "m", "claude-max") or "")


# ---------------------------------------------------------------------------
# Parsing, validation, policy version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku", "fable", "claude-opus"])
def test_bare_alias_rejected(alias):
    with pytest.raises(ValueError, match="bare alias"):
        parse_arm(f"claude-code:{alias}")


def test_arm_model_may_contain_colon():
    arm = parse_arm("gptme:openrouter/x/y:free")
    assert (arm.backend, arm.model) == ("gptme", "openrouter/x/y:free")


@pytest.mark.parametrize(
    ("needle", "text"),
    [
        ("bare alias", POLICY.replace("claude-opus-5-5", "sonnet")),
        ("unknown pool", POLICY.replace('pool = "claude-max"', 'pool = "nope"')),
        (
            "does not contain base effort",
            POLICY.replace('["high", "medium", "low"]', '["medium", "low"]'),
        ),
        ("unknown provider", POLICY.replace('"fake"', '"nope"')),
    ],
)
def test_validate_errors(tmp_path, reading, needle, text):
    lines, code = validate_policy(write_config(tmp_path, text))
    assert code == 1
    assert any(needle in ln for ln in lines), lines


def test_validate_ok_cli(tmp_path, reading):
    result = CliRunner().invoke(
        main, ["select", "--validate", "--config", str(write_config(tmp_path))]
    )
    assert result.exit_code == 0, result.output
    assert "ok: 1 lane(s), 1 pool(s)" in result.output
    assert "0 warning(s)" in result.output


def test_validate_warns_on_steps_that_step_back_up(tmp_path, reading):
    steps = "effort_steps = [{ at = 0.50, down = 2 }, { at = 0.80, down = 1 }]"
    text = POLICY.replace(
        'effort_steps = [{ at = 0.60, down = 1 }, { at = 0.85, to = "low" }]', steps
    )
    lines, code = validate_policy(write_config(tmp_path, text))
    assert code == 0
    assert any("step back up at 0.8: medium after low at 0.5" in ln for ln in lines)


def test_policy_version_stable_and_sensitive():
    raw = {"routing": {"schema_version": 1}, "lanes": {"a": {"arm": "x:y"}}}
    same = {"lanes": {"a": {"arm": "x:y"}}, "routing": {"schema_version": 1}}
    changed = {"routing": {"schema_version": 1}, "lanes": {"a": {"arm": "x:z"}}}
    assert policy_version(raw) == policy_version(same)
    assert policy_version(raw) != policy_version(changed)
    assert len(policy_version(raw)) == 12
    # Non-routing tables (e.g. [[candidates]]) do not change the version.
    assert policy_version({**raw, "candidates": [{"backend": "b"}]}) == policy_version(
        raw
    )


def test_lane_row_fields_parse():
    raw = {
        "routing": {"schema_version": 1},
        "lanes": {"l": {"arm": "codex:gpt-5.4", "scope": "lights-on"}},
    }
    policy = parse_routing(raw, date(2026, 1, 1))
    row = policy.row("l", None)
    assert isinstance(row, LaneRow)
    assert row.effort is None and row.ladder == () and row.scope == "lights-on"


# ---------------------------------------------------------------------------
# Usage provider (claude-usage)
# ---------------------------------------------------------------------------


def test_parse_claude_usage_pace_sign():
    # Half the week left: pace target 0.45; used 0.67 -> 0.22 ahead of pace.
    data = {
        "seven_day": {"utilization": 0.67, "resets_in_seconds": 302400},
        "_pacing": {"pace_gap": -0.22},
    }
    used, gap = parse_claude_usage(data)
    assert used == 0.67 and gap == pytest.approx(0.22, abs=1e-3)


def _script_pace_gap(used: float, resets_in_seconds: int) -> float:
    """``_pacing.pace_gap`` exactly as scripts/check-claude-usage-parser.py computes it."""
    elapsed_frac = max(0.0, 1.0 - resets_in_seconds / (7 * 24 * 3600))
    target = elapsed_frac * 0.9
    return round(target - used, 3)


@pytest.mark.parametrize(
    ("used", "effort", "rule"),
    [
        (0.58, "medium", "lane_pressure"),  # 0.13 ahead of pace: guard fires
        (0.30, "high", "lane"),  # 0.15 behind pace: guard does not fire
    ],
)
def test_legacy_pace_branch_sign(tmp_path, monkeypatch, env, used, effort, rule):
    """Without gptme_subscription the script's ``target - used`` value is flipped."""
    monkeypatch.setitem(sys.modules, "gptme_subscription", None)
    monkeypatch.setitem(sys.modules, "gptme_subscription.routing", None)
    resets = 302400  # half the week left: pace target 0.45
    cache = tmp_path / "usage.json"
    cache.write_text(
        json.dumps(
            {
                "seven_day": {"utilization": used, "resets_in_seconds": resets},
                "_pacing": {"pace_gap": _script_pace_gap(used, resets)},
            }
        )
    )
    used_read, gap = parse_claude_usage(json.loads(cache.read_text()))
    assert gap == pytest.approx(round(used - 0.45, 3))
    assert (gap > 0) == (used > 0.45)
    text = POLICY.replace('provider = "fake"', f'usage_cache = "{cache}"')
    d = run(tmp_path, config_path=write_config(tmp_path, text)).decision
    assert d["pool"]["source"] == "claude-usage:cache"
    assert d["pool"]["pace_gap"] == gap
    assert (d["effort"], d["rule"]) == (effort, rule), d["reason"]


def test_parse_claude_usage_unknown_shapes():
    assert parse_claude_usage({}) == (None, None)
    assert parse_claude_usage({"seven_day": {"utilization": "x"}}) == (None, None)
    used, gap = parse_claude_usage({"seven_day": {"utilization": 0.5}})
    assert (used, gap) == (0.5, None)


def test_claude_usage_reads_cache(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"seven_day": {"utilization": 0.4}}))
    r = read_pool_usage("claude-usage", {"usage_cache": str(cache)})
    assert r.used == 0.4 and r.source == "claude-usage:cache" and r.age_s is not None


def test_claude_usage_stale_cache_is_unknown(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"seven_day": {"utilization": 0.95}}))
    old = time.time() - 7200
    os.utime(cache, (old, old))
    r = read_pool_usage(
        "claude-usage", {"usage_cache": str(cache), "usage_max_age_s": 3600}
    )
    assert r.used is None and "stale" in (r.note or "")


def test_claude_usage_missing_cache_is_unknown(tmp_path):
    r = read_pool_usage("claude-usage", {"usage_cache": str(tmp_path / "none")})
    assert r.used is None and r.note


def test_claude_usage_command(tmp_path):
    payload = json.dumps({"seven_day": {"utilization": 0.61}})
    cmd = [sys.executable, "-c", f"print({payload!r})"]
    r = read_pool_usage(
        "claude-usage",
        {"usage_command": cmd, "usage_cache": str(tmp_path / "none")},
    )
    assert r.used == 0.61 and r.source == "claude-usage:command"


def test_claude_usage_command_timeout_is_unknown(tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
    r = read_pool_usage("claude-usage", {"usage_command": cmd, "usage_timeout_s": 0.2})
    assert r.used is None and "TimeoutExpired" in (r.note or "")
    assert select_usage.DEFAULT_TIMEOUT_S == 15.0


def test_claude_usage_command_failure_is_unknown(tmp_path):
    cmd = [sys.executable, "-c", "raise SystemExit(3)"]
    r = read_pool_usage("claude-usage", {"usage_command": cmd})
    assert r.used is None and "exited 3" in (r.note or "")


def test_provider_raising_never_propagates():
    def boom(options):
        raise OSError("nope")

    with patch.dict(select_usage.PROVIDERS, {"boom": boom}):
        r = read_pool_usage("boom", {})
    assert r.used is None and "OSError" in (r.note or "")


# ---------------------------------------------------------------------------
# CLI: output formats, shadow, decision log
# ---------------------------------------------------------------------------


def invoke(tmp_path: Path, *args: str):
    base = ["select", "--config", str(write_config(tmp_path))]
    base += ["--state-dir", str(tmp_path / "state")]
    return CliRunner().invoke(main, [*base, *args])


def test_cli_json_decision_and_log(tmp_path, reading, env):
    reading(0.67, 0.04)
    result = invoke(tmp_path, "--lane", "autonomous", "--work-item", "task-1")
    assert result.exit_code == 0, result.output
    d = json_line(result.stdout)
    assert d["rule"] == "lane_pressure" and d["work_item"] == "task-1"
    assert uuid.UUID(d["decision_id"]).version == 4
    log = tmp_path / "state" / "routing" / "decisions.jsonl"
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["decision_id"] == d["decision_id"] and row["ts"]
    for key in ("backend", "model", "effort", "rule", "reason", "policy_version"):
        assert key in row
    assert row["pool"] == {
        "name": "claude-max",
        "used": 0.67,
        "pace_gap": 0.04,
        "age_s": 10,
        "source": "fake",
    }


def test_cli_log_path_override_appends(tmp_path, reading, env):
    reading(0.1)
    log = tmp_path / "custom" / "d.jsonl"
    for _ in range(2):
        invoke(tmp_path, "--lane", "autonomous", "--log", str(log))
    assert len(log.read_text().splitlines()) == 2


def test_cli_shadow_always_exit_0(tmp_path, reading, env):
    reading(0.95)
    result = invoke(tmp_path, "--lane", "autonomous", "--shadow")
    assert result.exit_code == 0
    d = json_line(result.stdout)
    assert d["rule"] == "no_capacity" and d["shadow"] is True
    bad = CliRunner().invoke(
        main,
        ["select", "--config", str(tmp_path / "missing.toml"), "--lane", "x"]
        + ["--shadow", "--state-dir", str(tmp_path / "state")],
    )
    assert bad.exit_code == 0
    log = tmp_path / "state" / "routing" / "decisions.jsonl"
    assert all(json.loads(ln)["shadow"] for ln in log.read_text().splitlines())


def test_cli_no_capacity_exit_76(tmp_path, reading, env):
    reading(0.93)
    assert invoke(tmp_path, "--lane", "autonomous").exit_code == 76


def test_cli_no_arm_exit_69_and_shadow_0(tmp_path, reading, env):
    reading(0.1)
    args = ["--lane", "autonomous", "--exclude", LANE_ARM, "--reason", "crash"]
    result = invoke(tmp_path, *args, "--format", "shell")
    assert result.exit_code == 69
    assert "ROUTE_RULE=no_arm" in result.stdout
    assert "ROUTE_TRIGGER=crash" in result.stdout
    assert invoke(tmp_path, *args, "--shadow").exit_code == 0
    log = tmp_path / "state" / "routing" / "decisions.jsonl"
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [r["trigger"] for r in rows] == ["crash", "crash"]


def test_cli_exclude_with_reason(tmp_path, reading, env):
    reading(0.1)
    result = invoke(
        tmp_path, "--lane", "autonomous", "--exclude", LANE_ARM, "--reason", "auth"
    )
    assert json_line(result.stdout)["rule"] == "excluded_fallback"


def test_cli_shell_format_is_eval_safe(tmp_path, reading, env):
    reading(0.67)
    result = invoke(tmp_path, "--lane", "autonomous", "--format", "shell")
    assert result.exit_code == 0
    script = (
        result.stdout
        + '\nprintf "%s|%s|%s|%s" "$ROUTE_MODEL" "$ROUTE_EFFORT" "$ROUTE_RULE"'
        + ' "${ROUTE_TRIGGER-unset}"'
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.stdout == "claude-opus-5-5|medium|lane_pressure|"


def test_format_shell_quotes_hostile_values(tmp_path):
    nasty = 'it\'s $(touch pwned) `id` "x"; rm -rf nothing'
    text = format_shell({"backend": "b", "model": nasty, "scope": None})
    script = text + '\nprintf "%s" "$ROUTE_MODEL"; printf "|%s" "$ROUTE_SCOPE"'
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=tmp_path
    )
    assert out.stdout == nasty + "|"
    assert not (tmp_path / "pwned").exists()
    assert all("=" in ln for ln in text.splitlines())
    assert shlex.split(text.splitlines()[1])[0].startswith("ROUTE_MODEL=")


def test_cli_explain_goes_to_stderr(tmp_path, reading, env):
    reading(0.67, 0.04)
    result = invoke(tmp_path, "--lane", "autonomous", "--explain", "--format", "shell")
    assert "rule: lane_pressure" in result.stderr
    assert "rule:" not in result.stdout


def test_cli_fail_open_shell(tmp_path, reading, env):
    result = CliRunner().invoke(
        main,
        ["select", "--config", str(tmp_path / "missing.toml"), "--lane", "x"]
        + ["--default-arm", LANE_ARM, "--default-effort", "high"]
        + ["--format", "shell", "--state-dir", str(tmp_path / "state")],
    )
    assert result.exit_code == 0
    assert "ROUTE_RULE=fail_open" in result.stdout
    assert "failing open" in result.stderr


def test_cli_lane_flags_require_lane(tmp_path):
    result = invoke(tmp_path, "--shadow")
    assert result.exit_code == 2 and "requires --lane" in result.output


def test_cli_legacy_path_ignores_routing_tables(tmp_path):
    cfg = write_config(
        tmp_path,
        POLICY + '\n[[candidates]]\nbackend = "gptme"\nmodel = "m"\npriority = 1\n',
    )
    with (
        patch("gptme_runloops.select._binary_available", return_value=True),
        patch("gptme_runloops.select._is_block_active", return_value=False),
    ):
        result = CliRunner().invoke(main, ["select", "--config", str(cfg), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "backend": "gptme",
        "model": "m",
        "candidates": [{"backend": "gptme", "model": "m", "priority": 1}],
    }
