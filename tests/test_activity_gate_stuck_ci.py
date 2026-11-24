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
import shutil
import subprocess
import time
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "github" / "activity-gate.sh"


def _sha256_cmd() -> str:
    """A sha256 command available on this host.

    ``sha256sum`` is GNU coreutils and absent on macOS, which only ships
    ``shasum`` — the same portability the gate's own ``portable_hash()`` handles
    with an md5sum/shasum/cksum fallback chain.

    Both the pre-seeded state-file digest and the ``portable_hash()`` stub
    injected into the generated bash script go through this one helper. They
    must stay identical: hash them differently and the state file looks like a
    first sighting, so the test silently stops exercising the stuck path.
    """
    if shutil.which("sha256sum"):
        return "sha256sum"
    if shutil.which("shasum"):
        return "shasum -a 256"
    pytest.skip("no sha256 utility available (need sha256sum or shasum)")


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
        # A close/reopen retrigger (or a manual rerun) leaves the abandoned
        # attempt in the rollup forever. ActivityWatch/aw-server#206 looped
        # `ci_failure` for hours on 2026-11-22 with every selected check green
        # because an old macOS CANCELLED sat beside the new macOS SUCCESS.
        (
            "superseded cancellation is not a failure once the same workflow re-ran",
            [
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "completedAt": "2026-11-18T18:12:17Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-11-22T22:04:07Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
            ],
            "green",
        ),
        (
            "a later cancellation is not suppressed by an earlier success",
            [
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-11-18T18:12:17Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "completedAt": "2026-11-22T22:04:07Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
            ],
            "bad",
        ),
        (
            "another workflow's success does not suppress a cancellation",
            [
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "completedAt": "2026-11-18T18:12:17Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-11-22T22:04:07Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "codeql-workflow", "name": "CodeQL"}
                        }
                    },
                },
            ],
            "bad",
        ),
        (
            "a cancelled matrix sibling with a different name stays actionable",
            [
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "completedAt": "2026-11-18T18:12:17Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
                {
                    "name": "Test on ubuntu-24.04",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-11-22T22:04:07Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
            ],
            "bad",
        ),
        (
            "a superseded cancellation does not hide a real failure",
            [
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "completedAt": "2026-11-18T18:12:17Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
                {
                    "name": "Test on macOS-latest",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-11-22T22:04:07Z",
                    "checkSuite": {
                        "workflowRun": {
                            "workflow": {"id": "build-workflow", "name": "Build"}
                        }
                    },
                },
                {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
            ],
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
        (
            "status context failure fires even with checks still running",
            [
                {"__typename": "StatusContext", "state": "FAILURE"},
                {"status": "IN_PROGRESS", "conclusion": ""},
            ],
            "bad",
        ),
        # EXPECTED is StatusState's "required context that hasn't reported yet".
        # It has no .status and no .conclusion, so it must be recognised as
        # in-flight via .state or it reads as a non-green terminal result.
        (
            "status context expected is not yet a result",
            [{"__typename": "StatusContext", "state": "EXPECTED"}],
            "inflight",
        ),
        (
            "expected alongside a green check is still just waiting",
            [
                {"__typename": "StatusContext", "state": "EXPECTED"},
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "inflight",
        ),
        (
            "a real failure still wins over an EXPECTED context",
            [
                {"__typename": "StatusContext", "state": "EXPECTED"},
                {"__typename": "StatusContext", "state": "FAILURE"},
            ],
            "bad",
        ),
        # CheckStatusState has FIVE non-terminal members, not three. A CheckRun
        # sitting in PENDING/REQUESTED carries no `.state` and an empty
        # `.conclusion`, so when the classifier only listed
        # QUEUED/IN_PROGRESS/WAITING every predicate was false and the whole PR
        # read as `green` — deleting the stuck clock for the not-yet-started
        # shape this function exists to catch.
        (
            "checkrun PENDING is in flight, not green",
            [{"__typename": "CheckRun", "status": "PENDING", "conclusion": None}],
            "inflight",
        ),
        (
            "checkrun REQUESTED is in flight, not green",
            [{"__typename": "CheckRun", "status": "REQUESTED", "conclusion": None}],
            "inflight",
        ),
        (
            "PENDING alongside a green check is still just waiting",
            [
                {"__typename": "CheckRun", "status": "PENDING", "conclusion": None},
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "inflight",
        ),
        (
            "a CANCELLED supersede must not fire while a REQUESTED run is pending",
            [
                {"status": "COMPLETED", "conclusion": "CANCELLED"},
                {"__typename": "CheckRun", "status": "REQUESTED", "conclusion": None},
            ],
            "inflight",
        ),
        (
            "a real failure still wins over a PENDING checkrun",
            [
                {"__typename": "CheckRun", "status": "PENDING", "conclusion": None},
                {"status": "COMPLETED", "conclusion": "FAILURE"},
            ],
            "bad",
        ),
    ],
)
def test_ci_state_classification(label: str, rollup: list[dict], expected: str) -> None:
    assert _classify(rollup) == expected, label


@pytest.mark.parametrize(
    ("cancelled_at", "replacement_at", "expected"),
    [
        ("2026-11-22T22:04:07Z", "2026-11-22T22:04:07.5Z", "green"),
        ("2026-11-22T22:04:07.5Z", "2026-11-22T22:04:07Z", "bad"),
        ("2026-11-22T22:04:07.50Z", "2026-11-22T22:04:07.5Z", "bad"),
        (None, "2026-11-22T22:04:08Z", "bad"),
        ("2026-11-22T22:04:07Z", None, "bad"),
        ("invalid", "2026-11-22T22:04:08Z", "bad"),
    ],
)
def test_supersession_requires_comparable_completion_times(
    cancelled_at: str | None, replacement_at: str | None, expected: str
) -> None:
    def entry(conclusion: str, completed_at: str | None) -> dict:
        return {
            "name": "test",
            "status": "COMPLETED",
            "conclusion": conclusion,
            "completedAt": completed_at,
            # A start time is not a substitute for a missing completion time.
            "startedAt": "2026-11-22T22:00:00Z",
            "checkSuite": {
                "workflowRun": {"workflow": {"id": "build-workflow", "name": "Build"}}
            },
        }

    assert (
        _classify([entry("CANCELLED", cancelled_at), entry("SUCCESS", replacement_at)])
        == expected
    )


@pytest.mark.parametrize("replacement_id", ["other-workflow", None])
def test_same_display_name_is_not_workflow_identity(replacement_id: str | None) -> None:
    def entry(conclusion: str, completed_at: str, workflow_id: str | None) -> dict:
        return {
            "name": "test",
            "status": "COMPLETED",
            "conclusion": conclusion,
            "completedAt": completed_at,
            "checkSuite": {
                "workflowRun": {"workflow": {"name": "Build", "id": workflow_id}}
            },
        }

    assert (
        _classify(
            [
                entry("CANCELLED", "2026-11-22T22:04:07Z", "build-workflow"),
                entry("SUCCESS", "2026-11-22T22:04:08Z", replacement_id),
            ]
        )
        == "bad"
    )


def test_unknown_workflows_do_not_prove_supersession() -> None:
    assert (
        _classify(
            [
                {
                    "name": "test",
                    "status": "COMPLETED",
                    "conclusion": "CANCELLED",
                    "completedAt": "2026-11-22T22:04:07Z",
                },
                {
                    "name": "test",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-11-22T22:04:08Z",
                },
            ]
        )
        == "bad"
    )


def _fallback_entry(conclusion: str, completed_at: str, workflow: str) -> dict:
    """A CheckRun as `gh pr list --json statusCheckRollup` renders it."""
    return {
        "__typename": "CheckRun",
        "name": "test",
        "status": "COMPLETED",
        "conclusion": conclusion,
        "completedAt": completed_at,
        "workflowName": workflow,
    }


def test_supersession_works_on_gh_pr_list_fallback_shape() -> None:
    """gptme-contrib#2346 item 5: the `gh pr list` fallback has no checkSuite.

    Without keying on `workflowName` there, a superseded cancellation re-emits
    `ci_failure` on every run while the fallback is in use.
    """
    assert (
        _classify(
            [
                _fallback_entry("CANCELLED", "2026-11-18T10:00:00Z", "Build"),
                _fallback_entry("SUCCESS", "2026-11-22T10:00:00Z", "Build"),
            ]
        )
        == "green"
    )


def test_fallback_shape_other_workflow_does_not_supersede() -> None:
    assert (
        _classify(
            [
                _fallback_entry("CANCELLED", "2026-11-18T10:00:00Z", "Build"),
                _fallback_entry("SUCCESS", "2026-11-22T10:00:00Z", "CodeQL"),
            ]
        )
        == "bad"
    )


def test_fallback_shape_missing_workflow_name_does_not_supersede() -> None:
    assert (
        _classify(
            [
                _fallback_entry("CANCELLED", "2026-11-18T10:00:00Z", ""),
                _fallback_entry("SUCCESS", "2026-11-22T10:00:00Z", ""),
            ]
        )
        == "bad"
    )


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
    pr = {
        "number": 3472,
        "title": "stuck pr",
        "headRefOid": "a" * 40,
        "statusCheckRollup": rollup,
    }

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
            f"jq -r '{_extract_hash_program()}' | {_sha256_cmd()} | cut -d' ' -f1",
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
portable_hash() {{ {_sha256_cmd()} | cut -d' ' -f1; }}
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


def _ci_hash_key(rollup: list[dict], head_sha: str = "d" * 40) -> str:
    """The gate's dedup key for a rollup on a given head commit.

    ``head_sha`` defaults to a fixed value so callers comparing two rollups vary
    only the thing they mean to vary.
    """
    proc = subprocess.run(
        ["jq", "-r", _extract_hash_program()],
        input=json.dumps({"headRefOid": head_sha, "statusCheckRollup": rollup}),
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


def test_green_clears_state_file(tmp_path: Path) -> None:
    """A PR going green must delete the state file so a future inflight run that
    hashes to the same initial state doesn't inherit a stale mtime and appear
    immediately stuck.
    """
    rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    pr = {"number": 1, "title": "green pr", "statusCheckRollup": rollup}

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_file = state_dir / "o-r-pr-1-ci.state"
    # Plant a very old state file — simulating a prior inflight sighting.
    state_file.write_text("old-hash\n")
    old = time.time() - 7200
    os.utime(state_file, (old, old))

    script = f"""
set -uo pipefail
STATE_DIR={state_dir!s}
CI_STUCK_SECS=3600
portable_hash() {{ {_sha256_cmd()} | cut -d' ' -f1; }}
emit_item() {{ echo "EMIT type=$1 repo=$2 pr=$3 detail=$5"; }}
source_only=1
{_extract_function()}
check_ci_failures "o/r" '{json.dumps([pr])}'
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "EMIT" not in proc.stdout, f"green PR emitted: {proc.stdout!r}"
    assert not state_file.exists(), "green PR must clean up the state file"


def test_ci_failure_detail_includes_state_hash_token() -> None:
    """A terminal CI flip must change the grouped dispatch payload.

    ``activity-gate.sh`` state-tracks CI by ``ci_hash`` and emits only when the
    stored hash changes. But the downstream PM dispatcher dedupes grouped items
    by ``types`` + ``detail`` atoms. When the detail was the constant string
    ``CI failing``, a PR whose previous red check was already dispatched could
    flip to a different red check-set on the same head, pass activity-gate's
    ``ci_hash`` test, and still be swallowed as ``event_unchanged`` /
    ``event_subsumed`` by PM. Carrying the same state hash in the detail gives
    dispatch dedupe the same state boundary the gate already uses.
    """
    old_rollup = [{"status": "COMPLETED", "conclusion": "FAILURE", "name": "old"}]
    new_rollup = [
        {"status": "COMPLETED", "conclusion": "FAILURE", "name": "old"},
        {"status": "COMPLETED", "conclusion": "FAILURE", "name": "new"},
    ]
    old_key = _ci_hash_key(old_rollup)
    new_key = _ci_hash_key(new_rollup)
    assert old_key != new_key

    pr = {
        "number": 1,
        "title": "red pr",
        "headRefOid": "d" * 40,
        "statusCheckRollup": new_rollup,
    }

    expected_hash = subprocess.run(
        [
            "bash",
            "-c",
            f"jq -r '{_extract_hash_program()}' | {_sha256_cmd()} | cut -d' ' -f1",
        ],
        input=json.dumps(pr),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    old_pr = {**pr, "statusCheckRollup": old_rollup}
    old_hash = subprocess.run(
        [
            "bash",
            "-c",
            f"jq -r '{_extract_hash_program()}' | {_sha256_cmd()} | cut -d' ' -f1",
        ],
        input=json.dumps(old_pr),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    script = f"""
set -uo pipefail
STATE_DIR=$(mktemp -d)
CI_STUCK_SECS=3600
portable_hash() {{ {_sha256_cmd()} | cut -d' ' -f1; }}
emit_item() {{ echo "EMIT type=$1 repo=$2 pr=$3 detail=$5"; }}
source_only=1
{_extract_function()}
check_ci_failures "o/r" '{json.dumps([pr])}'
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert f"state_hash={expected_hash}" in proc.stdout, proc.stdout
    assert f"state_hash={old_hash}" not in proc.stdout, proc.stdout


def test_ci_hash_changes_on_new_head_commit() -> None:
    """A push must start a new stuck episode even for an identical rollup.

    The stuck clock is the state file's mtime, and the state file is only
    replaced when the hash changes. Without the head SHA in the hash, this
    sequence produces a false "CI stuck" report:

    1. checks go in flight on commit A; hash is "pending", file stamped at T0
    2. checks go green, then a force-push lands commit B (the whole green window
       fits between two gate runs, so the green cleanup never gets to run)
    3. checks go in flight on commit B; the hash is "pending" again and matches,
       so ``age = now - T0`` — hours — and the gate reports a run that started
       minutes ago as stuck.
    """
    rollup = [{"status": "QUEUED", "conclusion": ""}]
    assert _ci_hash_key(rollup, head_sha="a" * 40) != _ci_hash_key(
        rollup, head_sha="b" * 40
    ), "identical rollup on a new commit must not reuse the previous episode"


def test_new_head_commit_resets_the_stuck_clock(tmp_path: Path) -> None:
    """End-to-end form of the above, through the real shell function.

    An hours-old state file recorded against the *previous* head commit must not
    make freshly-queued checks on a new commit look stuck.
    """
    rollup = [
        {"status": "COMPLETED", "conclusion": "CANCELLED"},
        {"status": "QUEUED", "conclusion": ""},
    ]
    old_pr = {
        "number": 7,
        "title": "pushed pr",
        "headRefOid": "a" * 40,
        "statusCheckRollup": rollup,
    }
    new_pr = dict(old_pr, headRefOid="b" * 40)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    stale_digest = subprocess.run(
        [
            "bash",
            "-c",
            f"jq -r '{_extract_hash_program()}' | {_sha256_cmd()} | cut -d' ' -f1",
        ],
        input=json.dumps(old_pr),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    state_file = state_dir / "o-r-pr-7-ci.state"
    state_file.write_text(stale_digest + "\n")
    old = time.time() - 7200
    os.utime(state_file, (old, old))

    script = f"""
set -uo pipefail
STATE_DIR={state_dir!s}
CI_STUCK_SECS=3600
portable_hash() {{ {_sha256_cmd()} | cut -d' ' -f1; }}
emit_item() {{ echo "EMIT type=$1 repo=$2 pr=$3 detail=$5"; }}
source_only=1
{_extract_function()}
check_ci_failures "o/r" '{json.dumps([new_pr])}'
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "CI stuck" not in proc.stdout, (
        "checks queued on a brand-new commit reported as stuck using the "
        f"previous commit's clock: {proc.stdout!r}"
    )
    assert "EMIT" not in proc.stdout, f"unexpected emit: {proc.stdout!r}"
    # The new episode is recorded so the clock starts now, not two hours ago.
    assert state_file.read_text().strip() != stale_digest
    assert time.time() - state_file.stat().st_mtime < 60


def _run_gate_once(state_dir: Path, pr: dict) -> str:
    script = f"""
set -uo pipefail
STATE_DIR={state_dir!s}
CI_STUCK_SECS=3600
portable_hash() {{ {_sha256_cmd()} | cut -d' ' -f1; }}
emit_item() {{ echo "EMIT type=$1 repo=$2 pr=$3 detail=$5"; }}
source_only=1
{_extract_function()}
check_ci_failures "o/r" '{json.dumps([pr])}'
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout


def test_failed_pr_does_not_reemit_as_other_checks_finish(tmp_path: Path) -> None:
    """One red PR must emit once while its other checks complete, not once per
    completing check; a second failing check is a new signal."""
    failed = {"status": "COMPLETED", "conclusion": "FAILURE", "name": "lint"}
    pr = {
        "number": 9,
        "title": "red",
        "headRefOid": "c" * 40,
        "statusCheckRollup": [
            failed,
            {"status": "IN_PROGRESS", "conclusion": "", "name": "a"},
            {"status": "IN_PROGRESS", "conclusion": "", "name": "b"},
        ],
    }
    state = tmp_path / "state"
    state.mkdir()
    assert "EMIT" in _run_gate_once(state, pr)  # first failure fires at once

    one_done = dict(
        pr,
        statusCheckRollup=[
            failed,
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "a"},
            {"status": "IN_PROGRESS", "conclusion": "", "name": "b"},
        ],
    )
    assert "EMIT" not in _run_gate_once(state, one_done)

    all_done = dict(
        pr,
        statusCheckRollup=[
            failed,
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "a"},
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "b"},
        ],
    )
    assert "EMIT" not in _run_gate_once(state, all_done)

    second_red = dict(
        pr,
        statusCheckRollup=[
            failed,
            {"status": "COMPLETED", "conclusion": "FAILURE", "name": "a"},
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "b"},
        ],
    )
    assert "EMIT" in _run_gate_once(state, second_red)


def test_cancelled_with_running_check_waits_then_emits_once(tmp_path: Path) -> None:
    """The inflight clock is not a delivered failure: terminal cancellation
    must still emit its first actionable signal after the other check finishes.
    """
    cancelled = {"status": "COMPLETED", "conclusion": "CANCELLED", "name": "lint"}
    pr = {
        "number": 10,
        "title": "cancelled",
        "headRefOid": "d" * 40,
        "statusCheckRollup": [
            cancelled,
            {"status": "IN_PROGRESS", "conclusion": "", "name": "a"},
        ],
    }
    state = tmp_path / "state"
    state.mkdir()
    assert "EMIT" not in _run_gate_once(state, pr)
    assert "EMIT" not in _run_gate_once(state, pr)
    terminal = dict(
        pr,
        statusCheckRollup=[
            cancelled,
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "a"},
        ],
    )
    assert "EMIT" in _run_gate_once(state, terminal)
    assert "EMIT" not in _run_gate_once(state, terminal)
