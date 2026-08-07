"""check_ci_failures must catch CI that is terminal-but-not-green, or stuck.

Before this, the predicate was ``any(.conclusion == "FAILURE")``. The
2026-08-06 GitHub Actions outage produced two shapes in bulk that it missed,
and that no other gate predicate covers either:

* **terminal but not green** — CANCELLED / TIMED_OUT / ACTION_REQUIRED with
  nothing left running. The PR sits ``UNSTABLE``, so ``check_merge_ready``
  (wants CLEAN) skips it and ``check_merge_conflicts`` (wants DIRTY) skips it;
  once ``updatedAt`` stops moving ``check_pr_updates`` skips it too.
* **permanently QUEUED** — observed live on gptme/gptme#3472: three checks
  QUEUED for ~6h behind cancelled/orphaned jobs.

Such a PR is stranded indefinitely with no human push or comment.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "github" / "activity-gate.sh"


def _classify(rollup: list[dict]) -> str:
    """Run the gate's ci_state jq classifier exactly as the shell does."""
    program = _extract_classifier()
    proc = subprocess.run(
        ["jq", "-r", program],
        input=json.dumps({"statusCheckRollup": rollup}),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _extract_classifier() -> str:
    """Pull the jq program out of activity-gate.sh so the test can't drift from it."""
    src = GATE.read_text()
    marker = 'ci_state=$(echo "$pr_data" | jq -r \''
    assert (
        marker in src
    ), "ci_state classifier not found — did check_ci_failures change shape?"
    body = src.split(marker, 1)[1]
    return body.split("')", 1)[0]


@pytest.mark.parametrize(
    ("label", "rollup", "expected"),
    [
        ("all green", [{"status": "COMPLETED", "conclusion": "SUCCESS"}], "green"),
        (
            "skipped and neutral are green",
            [
                {"status": "COMPLETED", "conclusion": "SKIPPED"},
                {"status": "COMPLETED", "conclusion": "NEUTRAL"},
            ],
            "green",
        ),
        (
            "failure fires even with checks still running (unchanged behaviour)",
            [
                {"status": "COMPLETED", "conclusion": "FAILURE"},
                {"status": "QUEUED", "conclusion": ""},
            ],
            "bad",
        ),
        (
            "cancelled with nothing running is actionable",
            [{"status": "COMPLETED", "conclusion": "CANCELLED"}],
            "bad",
        ),
        (
            "cancel-in-progress supersede must NOT fire: new run still going",
            [
                {"status": "COMPLETED", "conclusion": "CANCELLED"},
                {"status": "IN_PROGRESS", "conclusion": ""},
            ],
            "inflight",
        ),
        ("timed out", [{"status": "COMPLETED", "conclusion": "TIMED_OUT"}], "bad"),
        ("only queued", [{"status": "QUEUED", "conclusion": ""}], "inflight"),
        # StatusContext shape (legacy commit statuses) carries .state, not .conclusion.
        ("status context green", [{"state": "SUCCESS"}], "green"),
        ("status context red", [{"state": "FAILURE"}], "bad"),
        ("status context pending", [{"state": "PENDING"}], "inflight"),
    ],
)
def test_ci_state_classification(label: str, rollup: list[dict], expected: str) -> None:
    assert _classify(rollup) == expected, label


def test_pr_3472_shape_is_inflight_not_green() -> None:
    """The real stranded PR: mostly green, one CANCELLED, three QUEUED for hours.

    It must classify as ``inflight`` so the staleness clock applies — not
    ``green`` (which would drop it) and not ``bad`` (which would fire instantly
    on every legitimately-running PR).
    """
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "build"},
        {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "lint"},
        {"status": "COMPLETED", "conclusion": "CANCELLED", "name": "typecheck"},
        {"status": "QUEUED", "conclusion": "", "name": "Test without API keys"},
        {"status": "QUEUED", "conclusion": "", "name": "API tests"},
        {"status": "COMPLETED", "conclusion": "SKIPPED", "name": "deploy"},
    ]
    assert _classify(rollup) == "inflight"


@pytest.mark.parametrize("age_secs,should_emit", [(10, False), (7200, True)])
def test_stuck_inflight_emits_only_after_threshold(
    tmp_path: Path, age_secs: int, should_emit: bool
) -> None:
    """An in-flight PR is emitted only once its state has not moved for CI_STUCK_SECS."""
    rollup = [
        {"status": "COMPLETED", "conclusion": "CANCELLED"},
        {"status": "QUEUED", "conclusion": ""},
    ]
    pr = {"number": 3472, "title": "stuck pr", "statusCheckRollup": rollup}

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Pre-seed the state file with the current hash, aged to taste. Compute it
    # through the identical shell pipeline — hashing the jq output *without* its
    # trailing newline yields a different digest, the state file then looks like
    # a first sighting, and the test silently stops exercising the stuck path.
    digest = subprocess.run(
        [
            "bash",
            "-c",
            f"jq -r '{_extract_hash_program()}' | sha256sum | cut -d' ' -f1",
        ],
        input=json.dumps(pr),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    state_file = state_dir / "o-r-pr-3472-ci.state"
    state_file.write_text(digest + "\n")
    old = time.time() - age_secs
    os.utime(state_file, (old, old))

    script = f"""
set -uo pipefail
STATE_DIR={state_dir!s}
CI_STUCK_SECS=3600
portable_hash() {{ sha256sum | cut -d' ' -f1; }}
emit_item() {{ echo "EMIT type=$1 repo=$2 pr=$3 detail=$5"; }}
source_only=1
{_extract_function()}
check_ci_failures "o/r" '{json.dumps([pr])}'
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    emitted = "EMIT" in proc.stdout
    assert (
        emitted is should_emit
    ), f"stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r}"
    if should_emit:
        assert "CI stuck" in proc.stdout


def _extract_function() -> str:
    """Extract check_ci_failures so it can run without sourcing the whole gate."""
    src = GATE.read_text()
    start = src.index("check_ci_failures() {")
    rest = src[start:]
    end = rest.index("\n}\n") + len("\n}\n")
    return rest[:end]


def _extract_hash_program() -> str:
    """Pull the ci_hash jq program out of the gate, same anti-drift trick."""
    src = GATE.read_text()
    marker = 'ci_hash=$(echo "$pr_data" | jq -r \''
    assert marker in src, "ci_hash program not found — did check_ci_failures change?"
    return src.split(marker, 1)[1].split("'", 1)[0]


def _ci_hash_key(rollup: list[dict]) -> str:
    proc = subprocess.run(
        ["jq", "-r", _extract_hash_program()],
        input=json.dumps({"statusCheckRollup": rollup}),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_ci_hash_tracks_legacy_status_context_transitions() -> None:
    """A legacy StatusContext going PENDING -> FAILURE must change the hash.

    Regression test for a bug our own self-hosted reviewer caught on the PR that
    introduced the three-way classifier (gptme/gptme-contrib#1383, 2026-08-07).

    The classifier learned to read ``.state`` so it could see legacy
    StatusContext items, which carry no ``.conclusion`` at all. The dedup hash
    did not: it hashed ``.conclusion // "pending"``, so every StatusContext
    hashed as ``"pending"`` forever. A status flipping PENDING -> FAILURE was
    therefore classified ``bad`` and then silently swallowed by the
    unchanged-hash check — precisely the "CI failed but nothing fired" symptom
    the PR set out to fix.

    The classifier and the hash must agree on which fields carry CI state.
    """
    pending = [{"__typename": "StatusContext", "context": "ci", "state": "PENDING"}]
    failure = [{"__typename": "StatusContext", "context": "ci", "state": "FAILURE"}]

    assert _classify(pending) == "inflight"
    assert _classify(failure) == "bad"
    assert _ci_hash_key(pending) != _ci_hash_key(failure), (
        "legacy StatusContext transition is invisible to the dedup hash — "
        "the ci_failure emit will be suppressed"
    )


def test_ci_hash_still_tracks_checkrun_transitions() -> None:
    """The .state fallback must not blind the hash to ordinary CheckRun items."""
    running = [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}]
    failed = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}
    ]
    assert _ci_hash_key(running) != _ci_hash_key(failed)
