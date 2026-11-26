"""CI discovery state records admission, not suppressed observations."""

import json
from unittest.mock import Mock, patch

import pytest
from gptme_runloops.project_monitoring import ProjectMonitoringRun

REPO = "example/repo"


def pr(number, conclusion="FAILURE", *, draft=False):
    return {
        "number": number,
        "title": f"Synthetic PR {number}",
        "url": f"https://github.com/{REPO}/pull/{number}",
        "isDraft": draft,
        "statusCheckRollup": [{"conclusion": conclusion}],
    }


def test_suppressed_failure_is_rediscovered_after_restart(tmp_path):
    run = ProjectMonitoringRun(tmp_path)
    response = Mock(returncode=0, stdout=json.dumps([pr(123)]))
    with (
        patch(
            "gptme_runloops.project_monitoring.subprocess.run", return_value=response
        ),
        patch.object(run, "should_post_comment", return_value=False) as gate,
    ):
        assert run.check_ci_failures(REPO) == []
        gate.assert_called_once_with(REPO, 123, "ci_failure")

    restarted = ProjectMonitoringRun(tmp_path)
    with (
        patch(
            "gptme_runloops.project_monitoring.subprocess.run", return_value=response
        ),
        patch.object(restarted, "should_post_comment", return_value=True) as gate,
    ):
        items = restarted.check_ci_failures(REPO)
        assert [item.number for item in items] == [123]
        assert items[0].item_type == "ci_failure"
        assert restarted.check_ci_failures(REPO) == []
        gate.assert_called_once_with(REPO, 123, "ci_failure")


def test_mixed_admission_keeps_only_delivered_failures_and_resets_on_green(tmp_path):
    run = ProjectMonitoringRun(tmp_path)
    response = Mock(returncode=0, stdout=json.dumps([pr(123), pr(456)]))
    with (
        patch(
            "gptme_runloops.project_monitoring.subprocess.run", return_value=response
        ),
        patch.object(
            run, "should_post_comment", side_effect=lambda repo, n, kind: n == 123
        ),
    ):
        assert [item.number for item in run.check_ci_failures(REPO)] == [123]

    response.stdout = json.dumps([pr(123, "SUCCESS"), pr(456)])
    with (
        patch(
            "gptme_runloops.project_monitoring.subprocess.run", return_value=response
        ),
        patch.object(run, "should_post_comment", return_value=True) as gate,
    ):
        assert [item.number for item in run.check_ci_failures(REPO)] == [456]
        gate.assert_called_once_with(REPO, 456, "ci_failure")
        response.stdout = json.dumps([pr(123), pr(456)])
        assert [item.number for item in run.check_ci_failures(REPO)] == [123]
        assert run.check_ci_failures(REPO) == []


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "CANCELLED", None])
def test_nonfailing_and_draft_prs_do_not_reach_comment_gate(tmp_path, conclusion):
    run = ProjectMonitoringRun(tmp_path)
    response = Mock(
        returncode=0, stdout=json.dumps([pr(123, conclusion), pr(456, draft=True)])
    )
    with (
        patch(
            "gptme_runloops.project_monitoring.subprocess.run", return_value=response
        ),
        patch.object(run, "should_post_comment") as gate,
    ):
        assert run.check_ci_failures(REPO) == []
        gate.assert_not_called()


def test_existing_admitted_state_is_preserved_and_closed_prs_are_removed(tmp_path):
    run = ProjectMonitoringRun(tmp_path)
    state = run.state_dir / "example-repo-ci-failures.state"
    state.write_text("123\n456")
    response = Mock(returncode=0, stdout=json.dumps([pr(123)]))
    with (
        patch(
            "gptme_runloops.project_monitoring.subprocess.run", return_value=response
        ),
        patch.object(run, "should_post_comment") as gate,
    ):
        assert run.check_ci_failures(REPO) == []
        assert state.read_text() == "123"
        response.stdout = "[]"
        assert run.check_ci_failures(REPO) == []
        assert state.read_text() == ""
        gate.assert_not_called()
