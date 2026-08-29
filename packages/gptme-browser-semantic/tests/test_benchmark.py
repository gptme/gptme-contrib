"""Regression test for the LLM-call benchmark claim.

Pins the numbers from the design doc: on the 5-task HN fixture the raw
browser path costs 11 LLM calls, the Path A semantic path 7 (−36.4%)
under the conservative production-path proxy. If a change to the action
sequences or the counting heuristic moves these totals, the design-doc
claim must be re-verified (and re-documented) rather than letting this
test rot silently.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_PATH = PACKAGE_ROOT / "benchmark.py"


def _load_benchmark():
    spec = importlib.util.spec_from_file_location("path_a_benchmark", BENCHMARK_PATH)
    assert spec and spec.loader, "benchmark.py not loadable"
    module = importlib.util.module_from_spec(spec)
    sys.modules["path_a_benchmark"] = module
    spec.loader.exec_module(module)
    return module


def test_benchmark_fixture_exists() -> None:
    assert (PACKAGE_ROOT / "fixtures" / "hn.html").exists()


def test_benchmark_reproduces_recorded_claim() -> None:
    bench = _load_benchmark()
    total_raw = 0
    total_sem = 0
    expected = {
        "click_first_headline": (2, 1),
        "read_all_comment_counts": (1, 1),
        "submit_search_query": (2, 2),
        "multi_step_open_click_scroll": (3, 1),
        "multi_step_with_verification": (3, 2),
    }
    for task in bench.TASKS:
        raw = bench.count_llm_calls(task.raw_actions, is_semantic=False)
        sem = bench.count_llm_calls(task.semantic_actions, is_semantic=True)
        assert (raw, sem) == expected[task.name], task.name
        total_raw += raw
        total_sem += sem

    assert len(bench.TASKS) == 5
    assert total_raw == 11
    assert total_sem == 7
    pct = (total_raw - total_sem) / total_raw * 100
    assert abs(pct - 36.4) < 0.1
