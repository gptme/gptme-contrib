"""Preserve the static five-scenario browser operation proxy.

This script does not execute the HTML fixture, a browser, either implementation,
or an LLM. It assigns proxy units to hard-coded action strings so the original
11→7 scenario arithmetic stays reproducible without being mislabeled as measured
performance.

The two columns are intentionally not comparable LLM-call counts: the raw side
stands in for outer-agent snapshot interpretation, while the semantic side
stands in for semantic resolution operations. A real benchmark must execute
both paths against the same page and report success, outer turns, inner provider
requests/tokens, and latency separately.

Usage (from this package directory)::

    python benchmark.py
    # or: make benchmark
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Task:
    name: str
    description: str
    raw_actions: list[str]
    semantic_actions: list[str]


# The 5 representative tasks from the Path A design doc.
TASKS: list[Task] = [
    Task(
        name="click_first_headline",
        description="Click the first headline link on the page",
        raw_actions=[
            "snapshot_page()",  # 1 raw proxy unit
            "click_element(text='First story')",  # 0
            "snapshot_page()",  # 1 raw proxy unit (verify)
        ],
        semantic_actions=[
            "browser_observe('the first headline link')",  # 1 semantic proxy unit
            "browser_act(<observed>)",  # 0
        ],
    ),
    Task(
        name="read_all_comment_counts",
        description="Read every comment count visible on the page",
        raw_actions=[
            "snapshot_page()",  # 1 raw proxy unit
            "# agent parses comment counts by hand from snapshot text",
        ],
        semantic_actions=[
            "browser_extract('all comment counts')",  # 1 semantic proxy unit
        ],
    ),
    Task(
        name="submit_search_query",
        description="Type 'rust async' into the search box and submit",
        raw_actions=[
            "snapshot_page()",  # 1 raw proxy unit
            "fill_element([name='q'], 'rust async')",  # 0
            "snapshot_page()",  # 1 raw proxy unit (verify)
            "click_element(text='Search')",  # 0
        ],
        semantic_actions=[
            "browser_observe('the search box')",  # 1 semantic proxy unit
            "browser_act(<observed>, method='fill', arguments=['rust async'])",  # 0
            "browser_act('click the Search button')",  # 1 semantic proxy unit
        ],
    ),
    Task(
        name="multi_step_open_click_scroll",
        description="Open page, click 'more' link, scroll down 2 screens",
        raw_actions=[
            "open_page(url)",  # 0 LLM (navigation only in this heuristic)
            "snapshot_page()",  # 1 raw proxy unit
            "click_element(text='more')",  # 0
            "snapshot_page()",  # 1 raw proxy unit (verify)
            "scroll_page(down, 1000)",  # 0
            "snapshot_page()",  # 1 raw proxy unit (verify)
        ],
        semantic_actions=[
            "browser_observe('the more link')",  # 1 semantic proxy unit
            "browser_act(<observed>)",  # 0
            "scroll_page(down, 1000)",  # 0
        ],
    ),
    Task(
        name="multi_step_with_verification",
        description="Open, click, then read state to verify result",
        raw_actions=[
            "open_page(url)",  # 0
            "snapshot_page()",  # 1 raw proxy unit
            "click_element(text='Submit')",  # 0
            "snapshot_page()",  # 1 raw proxy unit (verify)
            "snapshot_page()",  # 1 raw proxy unit (re-verify)
        ],
        semantic_actions=[
            "browser_observe('the submit button')",  # 1 semantic proxy unit
            "browser_act(<observed>)",  # 0
            "browser_extract('the current page state')",  # 1 semantic proxy unit
        ],
    ),
]


def count_proxy_units(actions: list[str], is_semantic: bool) -> int:
    """Count historical interpretation units in an action sequence.

    This is scenario accounting only. It does not claim that a unit equals one
    end-to-end LLM request.
    """
    units = 0
    for action in actions:
        if is_semantic:
            if "browser_observe" in action:
                units += 1
            elif "browser_extract" in action:
                units += 1
            elif "browser_act(" in action and "<observed>" not in action:
                units += 1
        elif "snapshot_page()" in action:
            units += 1
    return units


def main() -> None:
    fixture = Path(__file__).parent / "fixtures" / "hn.html"
    assert fixture.exists(), f"fixture missing: {fixture}"

    rows = []
    total_raw = 0
    total_sem = 0
    for task in TASKS:
        raw_units = count_proxy_units(task.raw_actions, is_semantic=False)
        sem_units = count_proxy_units(task.semantic_actions, is_semantic=True)
        delta = sem_units - raw_units
        total_raw += raw_units
        total_sem += sem_units
        rows.append((task.name, raw_units, sem_units, delta))

    report = [
        "# Browser semantic primitives — static operation proxy",
        "",
        f"Fixture: `{fixture.name}` (existence checked; not executed)",
        "",
        "| Task | Raw proxy units | Semantic proxy units | Δ |",
        "|------|---:|---:|---:|",
    ]
    for name, raw, sem, delta in rows:
        report.append(f"| {name} | {raw} | {sem} | {delta:+d} |")
    report.extend(
        [
            f"| **Total** | **{total_raw}** | **{total_sem}** | **{total_sem - total_raw:+d}** |",
            "",
            "This is static scenario accounting. It does not execute a browser or "
            "model and does not measure success, turns, tokens, or latency.",
        ]
    )

    out = {
        "fixture": str(fixture.name),
        "accounting": "static_scenario_proxy",
        "executed": False,
        "tasks": [
            {
                "name": r[0],
                "raw_proxy_units": r[1],
                "semantic_proxy_units": r[2],
                "delta": r[3],
            }
            for r in rows
        ],
        "totals": {
            "raw": total_raw,
            "semantic": total_sem,
            "delta": total_sem - total_raw,
        },
        "not_measured": [
            "success",
            "outer_turns",
            "provider_calls",
            "tokens",
            "latency",
        ],
    }
    report.extend(["", "```json", json.dumps(out, indent=2), "```", ""])
    sys.stdout.write("\n".join(report))


if __name__ == "__main__":
    main()
