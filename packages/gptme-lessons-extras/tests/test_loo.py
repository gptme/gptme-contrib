"""The shared LOO contract uses measured exposure and independent outcomes."""

import copy
import json
import math
import subprocess
import sys

import pytest
from gptme_lessons_extras.loo import (
    analyze,
    compare_groups,
    sample_std,
    validate_sessions,
    welch_z,
)


def session(sid, outcome, lessons=(), category="code"):
    return {
        "session_id": sid,
        "outcome": outcome,
        "lessons": [{"id": lid} for lid in lessons],
        "category": category,
    }


def cli(path, *args):
    return subprocess.run(
        [sys.executable, "-m", "gptme_lessons_extras.loo", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_group_statistics_match_hand_calculation():
    comparison = compare_groups([1.0, 3.0], [0.0, 2.0], min_sessions=2)
    assert comparison is not None
    assert comparison.avg_reward_with == 2.0
    assert comparison.avg_reward_without == 1.0
    assert comparison.delta == 1.0
    # Each sample variance is 2; standard error is sqrt(2/2 + 2/2).
    assert comparison.z_score == round(1 / math.sqrt(2), 3)
    assert comparison.p_value_approx == round(math.erfc(0.5), 4)
    assert comparison.confidence == 0.07
    assert not comparison.coverage_ok
    assert sample_std([1.0, 3.0]) == pytest.approx(math.sqrt(2))


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ([1.0, 1.0], [0.0, 0.0], (100.0, 0.0)),
        ([0.0, 0.0], [1.0, 1.0], (-100.0, 0.0)),
        ([1.0, 1.0], [1.0, 1.0], (0.0, 1.0)),
        ([1.0], [0.0], (0.0, 1.0)),
    ],
)
def test_constant_and_small_sample_welch_edges(a, b, expected):
    assert welch_z(a, b) == expected


def test_group_sample_guard_and_support_saturation():
    assert compare_groups([1.0] * 4, [0.0] * 30) is None
    comparison = compare_groups([1.0] * 30, [0.0] * 60)
    assert comparison is not None
    assert comparison.confidence == 1.0
    assert comparison.coverage_ok
    with pytest.raises(ValueError, match="positive"):
        compare_groups([], [], min_sessions=0)


@pytest.mark.parametrize("outcome", [True, float("nan"), float("inf"), None])
def test_outcome_must_be_measured_finite_number(outcome):
    with pytest.raises(ValueError, match="outcome must be a finite number"):
        analyze([session("s", outcome)])


def test_unknown_exposure_is_rejected_but_confirmed_none_is_valid():
    row = session("s", 1.0)
    del row["lessons"]
    with pytest.raises(ValueError, match="lessons must be an array"):
        analyze([row])
    report = analyze([session("s", 1.0)])
    assert report["total_sessions"] == 1
    assert report["results"] == []


def test_duplicate_session_ids_require_caller_reconciliation():
    with pytest.raises(ValueError, match="duplicate session_id"):
        analyze([session("same", 1.0, ["a"]), session("same", 0.0)])


def test_duplicate_exposure_counts_once_and_keeps_first_rating():
    a = session("a", 0.8)
    a["lessons"] = [
        {"id": "verify", "trigger_accuracy": 0.25},
        {"id": "verify", "trigger_accuracy": 0.75},
    ]
    report = analyze([a, session("b", 0.2)], min_sessions=1)
    result = report["results"][0]
    assert result["sessions_with"] == 1
    assert result["delta"] == 0.6
    assert result["avg_trigger_accuracy"] == 0.25
    assert result["trigger_accuracy_samples"] == 1


def test_opaque_ids_with_same_basename_remain_distinct():
    report = analyze(
        [session("a", 1.0, ["one/rule.md"]), session("b", 0.0, ["two/rule.md"])],
        min_sessions=1,
    )
    assert report["total_lessons"] == 2
    assert [r["id"] for r in report["results"]] == ["one/rule.md", "two/rule.md"]
    assert [r["delta"] for r in report["results"]] == [1.0, -1.0]


def test_insufficient_samples_report_unknown_effect_not_zero():
    result = analyze([session("a", 0.9, ["a"])])["results"][0]
    assert result["exposed_sessions"] == 1
    assert result["unexposed_sessions"] == 0
    assert result["insufficient_data"]
    assert result["delta"] is None
    assert result["avg_reward_without"] is None
    assert result["z_score"] is None
    assert result["p_value_approx"] is None
    assert result["confidence"] == 0.0
    assert not result["coverage_ok"]
    assert any("insufficient_samples" in w for w in result["warnings"])


def test_category_control_reverses_simpson_confound():
    # The lesson helps by .1 within both categories, but appears mostly in
    # hard work. Raw comparisons therefore suggest harm.
    rows = []
    for category, with_n, without_n, a, b in (
        ("hard", 9, 1, 0.2, 0.1),
        ("easy", 1, 9, 0.9, 0.8),
    ):
        for i in range(with_n):
            rows.append(session(f"{category}-with-{i}", a, ["a"], category))
        for i in range(without_n):
            rows.append(session(f"{category}-without-{i}", b, [], category))
    raw = analyze(rows, min_sessions=1)["results"][0]
    controlled = analyze(rows, min_sessions=1, category_controlled=True)
    result = controlled["results"][0]
    assert raw["delta"] == -0.46
    assert result["delta"] == 0.1
    assert result["categories_used"] == 2
    assert result["sessions_with"] == 10
    assert result["sessions_without"] == 10
    assert any("not a category-adjusted" in w for w in controlled["warnings"])


def test_category_weighting_excludes_categories_without_comparison():
    rows = [session("a", 1.0, ["a"], "small"), session("b", 0.0, [], "small")]
    rows += [session(f"large-with-{i}", 0.6, ["a"], "large") for i in range(3)]
    rows += [session(f"large-without-{i}", 0.4, [], "large") for i in range(3)]
    rows += [session("only-exposed", 0.0, ["a"], "unmatched")]
    result = analyze(rows, min_sessions=1, category_controlled=True)["results"][0]
    assert result["delta"] == 0.4  # (2 * 1.0 + 6 * .2) / 8
    assert result["categories_used"] == 2
    assert result["sessions_with"] == 4
    assert result["exposed_sessions"] == 5


def test_trigger_relevance_is_independent_and_missing_is_not_imputed():
    a = session("a", 1.0, ["a", "b"])
    a["lessons"][0]["trigger_accuracy"] = 0.2
    report = analyze([a, session("none", 0.0)], min_sessions=1)
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["a"]["avg_trigger_accuracy"] == 0.2
    assert by_id["a"]["trigger_accuracy_samples"] == 1
    assert by_id["b"]["avg_trigger_accuracy"] is None
    assert by_id["b"]["trigger_accuracy_samples"] == 0
    invalid = session("bad", 0.0, ["a"])
    invalid["lessons"][0]["trigger_accuracy"] = 1.1
    with pytest.raises(ValueError, match="between 0 and 1"):
        validate_sessions([invalid])


def test_catalog_recovery_and_repeated_analysis_warnings():
    a = session("a", 0.1, ["missing"])
    a["lessons"][0]["event"] = "error_recovery"
    report = analyze(
        [a, session("b", 0.5)],
        min_sessions=1,
        catalog={"current": {}},
        analysis_passes=2,
    )
    warnings = report["results"][0]["warnings"]
    assert any("ghost_lesson" in w for w in warnings)
    assert any("error_recovery_confound" in w for w in warnings)
    assert any("Over-grazing" in w for w in report["warnings"])
    assert any("Observational" in w for w in report["warnings"])
    assert any(
        "Ghost-lesson detection unavailable" in w for w in analyze([])["warnings"]
    )


def test_repeat_analysis_preserves_input_and_results():
    rows = [session("a", 0.8, ["a"]), session("b", 0.2)]
    original = copy.deepcopy(rows)
    first = analyze(rows, min_sessions=1, catalog={"a": {}})
    assert first == analyze(rows, min_sessions=1, catalog={"a": {}})
    assert rows == original
    assert not first["results"][0]["warnings"]


def test_module_cli_reports_json_and_catalog(tmp_path):
    path = tmp_path / "sessions.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                session("a", 0.8, ["known", "ghost"]),
                session("b", 0.2),
            ]
        )
    )
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"known": {}}')
    proc = cli(path, "--min-sessions", "1", "--catalog", str(catalog))
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["version"] == 1
    assert report["total_sessions"] == 2
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["known"]["delta"] == 0.6
    assert not by_id["known"]["warnings"]
    assert any("ghost_lesson" in w for w in by_id["ghost"]["warnings"])


@pytest.mark.parametrize(
    "content", ['{"session_id":', '{"session_id":"a","lessons":[]}']
)
def test_cli_bad_input_exits_with_error_without_partial_report(tmp_path, content):
    path = tmp_path / "bad.jsonl"
    path.write_text(content)
    proc = cli(path)
    assert proc.returncode == 2
    assert not proc.stdout
    assert "error:" in proc.stderr


def test_welch_statistic_is_invariant_to_small_outcome_units():
    a = [2.0, 3.0, 4.0, 3.0, 3.0]
    b = [0.0, 1.0, 2.0, 1.0, 1.0]
    assert welch_z([x * 1e-11 for x in a], [x * 1e-11 for x in b]) == pytest.approx(
        welch_z(a, b)
    )
    assert welch_z([1e-11] * 5, [0.0] * 5) == (100.0, 0.0)


@pytest.mark.parametrize("outcome", [-0.1, 1.1, 1e155])
def test_outcome_contract_requires_normalized_scores(outcome, tmp_path):
    with pytest.raises(ValueError, match="normalized between 0 and 1"):
        analyze([session("s", outcome)])
    path = tmp_path / "sessions.jsonl"
    path.write_text(json.dumps(session("s", outcome)) + "\n")
    result = cli(path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "normalized between 0 and 1" in result.stderr
    assert "Traceback" not in result.stderr
