"""Observational lesson with/without comparisons, independent of any agent's policy.

The historical name LOO does not imply a randomized ablation experiment.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

COVERAGE_MIN_N = 30


@dataclass(frozen=True)
class GroupComparison:
    sessions_with: int
    sessions_without: int
    avg_reward_with: float
    avg_reward_without: float
    delta: float
    confidence: float
    z_score: float
    p_value_approx: float
    coverage_ok: bool


def sample_std(values: Sequence[float]) -> float:
    """Sample standard deviation; zero for fewer than two observations."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1))


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def welch_z(group_a: Sequence[float], group_b: Sequence[float]) -> tuple[float, float]:
    """Welch statistic with a two-sided normal approximation, not an exact t-test."""
    n_a, n_b = len(group_a), len(group_b)
    if min(n_a, n_b) < 2:
        return 0.0, 1.0
    mean_a, mean_b = sum(group_a) / n_a, sum(group_b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in group_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in group_b) / (n_b - 1)
    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0.0:
        if mean_a != mean_b:
            return (100.0 if mean_a > mean_b else -100.0), 0.0
        return 0.0, 1.0
    z = (mean_a - mean_b) / se
    return z, 2 * (1 - normal_cdf(abs(z)))


def compare_groups(
    with_values: Sequence[float],
    without_values: Sequence[float],
    *,
    min_sessions: int = 5,
) -> GroupComparison | None:
    """Compare measured rewards, preserving the original analysis' rounding.

    Callers supply finite normalized scores in [0, 1]; analyze() validates
    this boundary for session records before invoking the comparison helpers.
    Confidence is sample support, min(n_with, n_without)/30 capped at one.
    It is neither an effect probability nor a statistical coverage guarantee.
    """
    if min_sessions < 1:
        raise ValueError("min_sessions must be positive")
    n_with, n_without = len(with_values), len(without_values)
    if min(n_with, n_without) < min_sessions:
        return None
    avg_with = sum(with_values) / n_with
    avg_without = sum(without_values) / n_without
    z, p = welch_z(with_values, without_values)
    return GroupComparison(
        n_with,
        n_without,
        round(avg_with, 4),
        round(avg_without, 4),
        round(avg_with - avg_without, 4),
        round(min(1.0, min(n_with, n_without) / COVERAGE_MIN_N), 2),
        round(z, 3),
        round(p, 4),
        min(n_with, n_without) >= COVERAGE_MIN_N,
    )


def summarize_trigger_accuracy(values: Sequence[float]) -> tuple[float | None, int]:
    """Summarize independently measured relevance, never impute from outcomes."""
    return (sum(values) / len(values), len(values)) if values else (None, 0)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def validate_sessions(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Validate complete session observations; reject unknown exposure/outcome."""
    sessions = []
    seen: set[str] = set()
    for index, row in enumerate(records, 1):
        label = f"row {index}"
        if not isinstance(row, dict):
            raise ValueError(f"{label} must be an object")
        sid = _identifier(row.get("session_id"), f"{label} session_id")
        if sid in seen:
            raise ValueError(
                f"{label}: duplicate session_id {sid!r}; join/deduplicate first"
            )
        seen.add(sid)
        outcome = _number(row.get("outcome"), f"{label} outcome")
        if not 0 <= outcome <= 1:
            raise ValueError(f"{label} outcome must be normalized between 0 and 1")
        category = row.get("category", "unknown")
        category = _identifier(category, f"{label} category")
        if not isinstance(row.get("lessons"), list):
            raise ValueError(
                f"{label} lessons must be an array (empty means confirmed none)"
            )
        lessons: dict[str, dict[str, Any]] = {}
        for item in row["lessons"]:
            if not isinstance(item, dict):
                raise ValueError(f"{label} lesson must be an object")
            lid = _identifier(item.get("id"), f"{label} lesson id")
            exposure: dict[str, Any] = {"id": lid}
            if "trigger_accuracy" in item:
                accuracy = _number(
                    item["trigger_accuracy"], f"{label} trigger_accuracy"
                )
                if not 0 <= accuracy <= 1:
                    raise ValueError(
                        f"{label} trigger_accuracy must be between 0 and 1"
                    )
                exposure["trigger_accuracy"] = accuracy
            for key in ("keyword", "event"):
                if key in item:
                    exposure[key] = _identifier(item[key], f"{label} {key}")
            lessons.setdefault(lid, exposure)
        sessions.append(
            {
                "session_id": sid,
                "outcome": outcome,
                "category": category,
                "lessons": lessons,
            }
        )
    return sessions


def analyze(
    records: Iterable[Any],
    *,
    min_sessions: int = 5,
    category_controlled: bool = False,
    catalog: Mapping[str, Any] | None = None,
    analysis_passes: int = 1,
) -> dict[str, Any]:
    """Compute descriptive comparisons; no lesson lifecycle or bandit mutations."""
    if min_sessions < 1 or analysis_passes < 1:
        raise ValueError("min_sessions and analysis_passes must be positive")
    sessions = validate_sessions(records)
    warnings = [
        "Observational associations only: exposure is not randomized; deltas do not establish benefit or harm.",
        "Error-recovery confound: lessons firing after errors inherit harder sessions; a negative delta is not evidence of harm.",
        "Confidence measures sample support, not effect probability. P-values are normal approximations, unadjusted for multiple comparisons.",
    ]
    if catalog is None:
        warnings.append(
            "Ghost-lesson detection unavailable: no authoritative current catalog supplied."
        )
    if analysis_passes >= 2:
        warnings.append(
            "Over-grazing guard: repeated same-data tuning is not new evidence; validate on fresh sessions or a held-out cohort before changing lessons."
        )
    if category_controlled:
        warnings.append(
            "Category deltas use eligible category-size weights; pooled z/p are descriptive and are not a category-adjusted significance test."
        )
    lesson_ids = sorted({lid for s in sessions for lid in s["lessons"]})
    results = []
    for lid in lesson_ids:
        exposed = [s for s in sessions if lid in s["lessons"]]
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for session in sessions:
            groups[session["category"] if category_controlled else "all"].append(
                session
            )
        with_values: list[float] = []
        without_values: list[float] = []
        triggers: list[float] = []
        weighted_delta = 0.0
        total_weight = 0
        categories_used = 0
        for cohort in groups.values():
            with_rows = [s for s in cohort if lid in s["lessons"]]
            without_rows = [s for s in cohort if lid not in s["lessons"]]
            if (
                category_controlled
                and min(len(with_rows), len(without_rows)) < min_sessions
            ):
                continue
            a = [s["outcome"] for s in with_rows]
            b = [s["outcome"] for s in without_rows]
            if a and b:
                weighted_delta += (sum(a) / len(a) - sum(b) / len(b)) * len(cohort)
            total_weight += len(cohort)
            categories_used += 1
            with_values.extend(a)
            without_values.extend(b)
            triggers.extend(
                s["lessons"][lid]["trigger_accuracy"]
                for s in with_rows
                if "trigger_accuracy" in s["lessons"][lid]
            )
        comparison = compare_groups(
            with_values, without_values, min_sessions=min_sessions
        )
        accuracy, accuracy_n = summarize_trigger_accuracy(triggers)
        row_warnings = []
        if catalog is not None and lid not in catalog:
            row_warnings.append(
                "ghost_lesson: absent from current catalog; historical value does not establish a current gap"
            )
        if any(s["lessons"][lid].get("event") == "error_recovery" for s in exposed):
            row_warnings.append("error_recovery_confound: exposure follows failure")
        result = (
            asdict(comparison)
            if comparison
            else {
                "sessions_with": len(with_values),
                "sessions_without": len(without_values),
                "avg_reward_with": None,
                "avg_reward_without": None,
                "delta": None,
                "confidence": 0.0,
                "z_score": None,
                "p_value_approx": None,
                "coverage_ok": False,
            }
        )
        if comparison and category_controlled:
            result["delta"] = round(weighted_delta / total_weight, 4)
        if not comparison:
            row_warnings.append(
                "insufficient_samples: no eligible with/without comparison"
            )
        results.append(
            {
                "id": lid,
                **result,
                "exposed_sessions": len(exposed),
                "unexposed_sessions": len(sessions) - len(exposed),
                "categories_used": categories_used if category_controlled else None,
                "insufficient_data": comparison is None,
                "avg_trigger_accuracy": round(accuracy, 4)
                if accuracy is not None
                else None,
                "trigger_accuracy_samples": accuracy_n,
                "warnings": row_warnings,
            }
        )
    return {
        "version": 1,
        "total_sessions": len(sessions),
        "total_lessons": len(lesson_ids),
        "category_controlled": category_controlled,
        "warnings": warnings,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sessions", type=Path, help="JSONL of complete exposure/outcome observations"
    )
    parser.add_argument("--min-sessions", type=int, default=5)
    parser.add_argument("--category-controlled", action="store_true")
    parser.add_argument(
        "--catalog", type=Path, help="JSON object keyed by current lesson IDs"
    )
    parser.add_argument("--analysis-passes", type=int, default=1)
    args = parser.parse_args()
    try:
        with args.sessions.open() as stream:
            records = [json.loads(line) for line in stream if line.strip()]
        catalog = json.loads(args.catalog.read_text()) if args.catalog else None
        if catalog is not None and not isinstance(catalog, dict):
            raise ValueError("catalog must be an object keyed by current lesson IDs")
        report = analyze(
            records,
            min_sessions=args.min_sessions,
            category_controlled=args.category_controlled,
            catalog=catalog,
            analysis_passes=args.analysis_passes,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
