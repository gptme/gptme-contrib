"""Tests for ProjectMonitoringRun class."""

import json
from unittest.mock import MagicMock, patch

import pytest
from gptme_runloops.project_monitoring import ProjectMonitoringRun, WorkItem


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Create logs directory for state tracking
    logs_dir = workspace_dir / "logs"
    logs_dir.mkdir()

    return workspace_dir


def test_project_monitoring_init(workspace):
    """Test ProjectMonitoringRun initialization."""
    run = ProjectMonitoringRun(workspace)

    assert run.workspace == workspace
    assert run.run_type == "project-monitoring"
    assert run.timeout == 1800  # 30 minutes default (overridden by has_work())
    assert run.lock_wait is False
    assert run.target_orgs == []  # Default is empty list
    assert run.target_repos == []  # Default is empty list
    assert run.author == ""  # No default author
    assert run.agent_name == "Agent"  # Default agent name
    assert run.state_dir.exists()


def test_compute_timeout_single_assigned_issue(workspace):
    """Assigned issues use the longest per-item budget."""
    run = ProjectMonitoringRun(workspace)
    items = [
        WorkItem(
            repo="r/r",
            item_type="assigned_issue",
            number=1,
            title="t",
            url="u",
            details="d",
        )
    ]
    assert run._compute_timeout(items) == 1500


def test_compute_timeout_single_pr_update(workspace):
    """PR updates use the medium-tier budget."""
    run = ProjectMonitoringRun(workspace)
    items = [
        WorkItem(
            repo="r/r",
            item_type="pr_update",
            number=1,
            title="t",
            url="u",
            details="d",
        )
    ]
    assert run._compute_timeout(items) == 1200


def test_compute_timeout_single_notification(workspace):
    """Notifications use the shortest budget."""
    run = ProjectMonitoringRun(workspace)
    items = [
        WorkItem(
            repo="r/r",
            item_type="notification",
            number=1,
            title="t",
            url="u",
            details="d",
        )
    ]
    assert run._compute_timeout(items) == 600


def test_compute_timeout_multiple_items_summed(workspace):
    """Multiple items sum their budgets."""
    run = ProjectMonitoringRun(workspace)
    items = [
        WorkItem(
            repo="r/r",
            item_type="assigned_issue",
            number=1,
            title="t",
            url="u",
            details="d",
        ),
        WorkItem(
            repo="r/r", item_type="pr_update", number=2, title="t", url="u", details="d"
        ),
        WorkItem(
            repo="r/r",
            item_type="notification",
            number=3,
            title="t",
            url="u",
            details="d",
        ),
    ]
    # 1500 + 1200 + 600 = 3300, within cap
    assert run._compute_timeout(items) == 3300


def test_compute_timeout_capped_at_max(workspace):
    """Many items are capped at _MAX_TIMEOUT."""
    run = ProjectMonitoringRun(workspace)
    items = [
        WorkItem(
            repo="r/r",
            item_type="assigned_issue",
            number=i,
            title="t",
            url="u",
            details="d",
        )
        for i in range(10)  # 10 × 1500 = 15000 → capped at 3600
    ]
    assert run._compute_timeout(items) == run._MAX_TIMEOUT


def test_compute_timeout_unknown_type_uses_default(workspace):
    """Unknown item types fall back to _DEFAULT_ITEM_TIMEOUT."""
    run = ProjectMonitoringRun(workspace)
    items = [
        WorkItem(
            repo="r/r",
            item_type="unknown_future_type",
            number=1,
            title="t",
            url="u",
            details="d",
        )
    ]
    assert run._compute_timeout(items) == run._DEFAULT_ITEM_TIMEOUT


def test_has_work_sets_timeout_dynamically(workspace):
    """has_work() updates self.timeout based on discovered item complexity."""
    run = ProjectMonitoringRun(workspace)
    assert run.timeout == 1800  # initial default

    item = WorkItem(
        repo="r/r",
        item_type="assigned_issue",
        number=1,
        title="t",
        url="u",
        details="d",
    )
    with patch.object(run, "discover_work", return_value=[item]):
        result = run.has_work()

    assert result is True
    assert run.timeout == 1500  # adjusted for assigned_issue


def test_project_monitoring_custom_org(workspace):
    """Test ProjectMonitoringRun with custom organization."""
    run = ProjectMonitoringRun(
        workspace, target_orgs=["custom-org"], author="custom-author"
    )

    assert run.target_orgs == ["custom-org"]
    assert run.author == "custom-author"


@patch("gptme_runloops.project_monitoring.subprocess.run")
def test_discover_repositories_success(mock_run, workspace):
    """Test successful repository discovery."""
    # Mock gh repo list response
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="gptme/gptme\ngptme/gptme-webui\ngptme/gptme-contrib\n",
        stderr="",
    )

    # Must specify target_orgs since default is empty
    run = ProjectMonitoringRun(workspace, target_orgs=["gptme"])
    repos = run.discover_repositories()

    assert len(repos) == 3
    assert "gptme/gptme" in repos
    assert "gptme/gptme-webui" in repos


@patch("gptme_runloops.project_monitoring.subprocess.run")
def test_discover_repositories_failure(mock_run, workspace):
    """Test repository discovery failure."""
    # Mock gh command failure
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="gh: not authenticated",
    )

    run = ProjectMonitoringRun(workspace)
    repos = run.discover_repositories()

    assert repos == []


def test_should_post_comment_first_time(workspace):
    """Test posting comment for first time."""
    run = ProjectMonitoringRun(workspace)

    # Mock gh pr view to return updated time and last comment author
    with patch("gptme_runloops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"updatedAt": "2025-11-25T10:00:00Z", "lastCommentAuthor": ""}',
            stderr="",
        )

        # First time: should post
        should_post = run.should_post_comment("gptme/gptme", 123, "update")
        assert should_post is True

        # State file should be created
        state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
        assert state_file.exists()


def test_should_post_comment_duplicate(workspace):
    """Test spam prevention for duplicate comments."""
    run = ProjectMonitoringRun(workspace)

    # Create existing state file (comment posted 1 hour ago)
    from datetime import datetime, timedelta

    prev_time = (datetime.now() - timedelta(hours=1)).isoformat()
    state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
    state_file.write_text(f"update {prev_time} 2025-11-25T09:00:00Z")

    # Mock gh pr view to return same updated time (no changes)
    with patch("gptme_runloops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"updatedAt": "2025-11-25T09:00:00Z", "lastCommentAuthor": ""}',
            stderr="",
        )

        # Should NOT post (duplicate)
        should_post = run.should_post_comment("gptme/gptme", 123, "update")
        assert should_post is False


def test_should_post_comment_pr_updated(workspace):
    """Test posting comment when PR is updated."""
    run = ProjectMonitoringRun(workspace)

    # Create existing state file
    from datetime import datetime, timedelta

    prev_time = (datetime.now() - timedelta(hours=1)).isoformat()
    state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
    state_file.write_text(f"update {prev_time} 2025-11-25T09:00:00Z")

    # Mock gh pr view to return newer updated time (PR has changes)
    with patch("gptme_runloops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"updatedAt": "2025-11-25T11:00:00Z", "lastCommentAuthor": "other-user"}',  # Newer than state file, not by self
            stderr="",
        )

        # Should post (PR updated)
        should_post = run.should_post_comment("gptme/gptme", 123, "update")
        assert should_post is True


def test_should_post_comment_type_changed(workspace):
    """Test posting comment when comment type changes."""
    run = ProjectMonitoringRun(workspace)

    # Create existing state file with "update" type
    from datetime import datetime, timedelta

    prev_time = (datetime.now() - timedelta(hours=1)).isoformat()
    state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
    state_file.write_text(f"update {prev_time} 2025-11-25T10:00:00Z")

    # Mock gh pr view
    with patch("gptme_runloops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"updatedAt": "2025-11-25T10:00:00Z", "lastCommentAuthor": ""}',
            stderr="",
        )

        # Should post (type changed from "update" to "ci_failure")
        should_post = run.should_post_comment("gptme/gptme", 123, "ci_failure")
        assert should_post is True


def test_should_post_comment_stale(workspace):
    """Test posting comment when previous comment is stale (24+ hours)."""
    run = ProjectMonitoringRun(workspace)

    # Create existing state file (comment posted 25 hours ago)
    from datetime import datetime, timedelta

    prev_time = (datetime.now() - timedelta(hours=25)).isoformat()
    state_file = run.state_dir / "gptme-gptme-pr-123-comment.state"
    state_file.write_text(f"update {prev_time} 2025-11-25T10:00:00Z")

    # Mock gh pr view
    with patch("gptme_runloops.project_monitoring.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"updatedAt": "2025-11-25T10:00:00Z", "lastCommentAuthor": ""}',
            stderr="",
        )

        # Should post (stale comment)
        should_post = run.should_post_comment("gptme/gptme", 123, "update")
        assert should_post is True


def test_should_post_comment_concurrent_first_time(workspace):
    """Race condition: two sessions on the same PR with no prior state.

    Only one should return True (post). Without flock the check-then-write
    window lets both sessions see state_file.exists()==False and both post.
    """
    import threading
    from unittest.mock import MagicMock, patch

    results = []
    barrier = threading.Barrier(2)

    def call_should_post():
        run = ProjectMonitoringRun(workspace)
        with patch("gptme_runloops.project_monitoring.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"updatedAt": "2025-11-25T10:00:00Z", "lastCommentAuthor": ""}',
                stderr="",
            )
            # Sync both threads at the gate so they enter should_post_comment
            # at the same time, maximising the chance of hitting the race.
            barrier.wait()
            result = run.should_post_comment("gptme/gptme", 456, "update")
            results.append(result)

    t1 = threading.Thread(target=call_should_post)
    t2 = threading.Thread(target=call_should_post)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one session should have won the race and posted.
    assert (
        results.count(True) == 1
    ), f"Expected exactly 1 True (one poster), got {results}"


@patch("gptme_runloops.project_monitoring.subprocess.run")
def test_check_pr_updates_new_pr(mock_run, workspace):
    """Test detecting new PR updates."""
    # Mock gh pr list response
    pr_data = json.dumps(
        [
            {
                "number": 123,
                "title": "Add new feature",
                "updatedAt": "2025-11-25T10:00:00Z",
                "url": "https://github.com/gptme/gptme/pull/123",
            }
        ]
    )
    comment_state = '{"updatedAt": "2025-11-25T10:00:00Z", "lastCommentAuthor": ""}'

    def side_effect(args, **kwargs):
        if "updatedAt,comments" in args:
            return MagicMock(returncode=0, stdout=comment_state, stderr="")
        return MagicMock(returncode=0, stdout=pr_data, stderr="")

    mock_run.side_effect = side_effect

    run = ProjectMonitoringRun(workspace)
    work_items = run.check_pr_updates("gptme/gptme")

    assert len(work_items) == 1
    assert work_items[0].item_type == "pr_update"
    assert work_items[0].number == 123
    assert work_items[0].repo == "gptme/gptme"


@patch("gptme_runloops.project_monitoring.subprocess.run")
def test_check_pr_updates_no_change(mock_run, workspace):
    """Test PR with no updates."""
    # Mock gh pr list response
    pr_data = json.dumps(
        [
            {
                "number": 123,
                "title": "Add new feature",
                "updatedAt": "2025-11-25T10:00:00Z",
                "url": "https://github.com/gptme/gptme/pull/123",
            }
        ]
    )
    comment_state = '{"updatedAt": "2025-11-25T10:00:00Z", "lastCommentAuthor": ""}'

    def side_effect(args, **kwargs):
        if "updatedAt,comments" in args:
            return MagicMock(returncode=0, stdout=comment_state, stderr="")
        return MagicMock(returncode=0, stdout=pr_data, stderr="")

    mock_run.side_effect = side_effect

    run = ProjectMonitoringRun(workspace)

    # First check: should find work
    work_items = run.check_pr_updates("gptme/gptme")
    assert len(work_items) == 1

    # Second check with same timestamp: should find nothing
    work_items = run.check_pr_updates("gptme/gptme")
    assert len(work_items) == 0


@patch("gptme_runloops.project_monitoring.subprocess.run")
def test_check_pr_updates_surfaces_draft(mock_run, workspace):
    """Draft PRs authored by self should still surface as pr_update items.

    Regression: prior behavior skipped drafts entirely, so a Bob-authored draft
    PR was invisible to monitoring until manually promoted. Erik flagged this
    on gptme/gptme#2390.
    """
    pr_data = json.dumps(
        [
            {
                "number": 999,
                "title": "WIP: thing",
                "updatedAt": "2026-05-12T20:17:24Z",
                "url": "https://github.com/gptme/gptme/pull/999",
                "headRefName": "wip-thing",
                "isDraft": True,
            }
        ]
    )
    comment_state = '{"updatedAt": "2026-05-12T20:17:24Z", "lastCommentAuthor": ""}'

    def side_effect(args, **kwargs):
        if "updatedAt,comments" in args:
            return MagicMock(returncode=0, stdout=comment_state, stderr="")
        return MagicMock(returncode=0, stdout=pr_data, stderr="")

    mock_run.side_effect = side_effect

    run = ProjectMonitoringRun(workspace)
    work_items = run.check_pr_updates("gptme/gptme")

    assert len(work_items) == 1
    item = work_items[0]
    assert item.item_type == "pr_update"
    assert item.number == 999
    assert item.title.startswith("[DRAFT] ")
    assert "Draft" in item.details

    # Second call with same timestamp: state written, no duplicate surfaced
    work_items = run.check_pr_updates("gptme/gptme")
    assert len(work_items) == 0


@patch("gptme_runloops.project_monitoring.subprocess.run")
def test_check_ci_failures_still_skips_drafts(mock_run, workspace):
    """CI-failure path should keep skipping drafts.

    Drafts often have intentionally-broken CI during early development;
    chasing CI fixes on parked work is the failure mode that the original
    skip (commit 91a8ca5) addressed. Only the pr_update path changed.
    """
    pr_data = json.dumps(
        [
            {
                "number": 999,
                "title": "WIP: thing",
                "url": "https://github.com/gptme/gptme/pull/999",
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
                "isDraft": True,
            }
        ]
    )
    mock_run.return_value = MagicMock(returncode=0, stdout=pr_data, stderr="")

    run = ProjectMonitoringRun(workspace)
    work_items = run.check_ci_failures("gptme/gptme")

    assert work_items == []


@patch("gptme_runloops.project_monitoring.subprocess.run")
def test_check_ci_failures(mock_run, workspace):
    """Test detecting CI failures."""
    # Mock gh pr list response with failing checks
    pr_data = json.dumps(
        [
            {
                "number": 123,
                "title": "Add new feature",
                "url": "https://github.com/gptme/gptme/pull/123",
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "FAILURE"},
                ],
            }
        ]
    )
    comment_state = '{"updatedAt": "2025-11-25T10:00:00Z", "lastCommentAuthor": ""}'

    def side_effect(args, **kwargs):
        if "updatedAt,comments" in args:
            return MagicMock(returncode=0, stdout=comment_state, stderr="")
        return MagicMock(returncode=0, stdout=pr_data, stderr="")

    mock_run.side_effect = side_effect

    run = ProjectMonitoringRun(workspace)
    work_items = run.check_ci_failures("gptme/gptme")

    assert len(work_items) == 1
    assert work_items[0].item_type == "ci_failure"
    assert work_items[0].number == 123


@patch("gptme_runloops.project_monitoring.subprocess.run")
def test_check_assigned_issues(mock_run, workspace):
    """Test detecting assigned issues."""
    # Mock gh issue list response
    issue_data = json.dumps(
        [
            {
                "number": 456,
                "title": "Fix bug",
                "url": "https://github.com/gptme/gptme/issues/456",
            }
        ]
    )
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=issue_data,
        stderr="",
    )

    run = ProjectMonitoringRun(workspace)
    work_items = run.check_assigned_issues("gptme/gptme")

    assert len(work_items) == 1
    assert work_items[0].item_type == "assigned_issue"
    assert work_items[0].number == 456


def test_generate_prompt_no_work(workspace):
    """Test prompt generation when no work found."""
    run = ProjectMonitoringRun(workspace)

    with patch.object(run, "discover_work", return_value=[]):
        prompt = run.generate_prompt()
        assert prompt == ""


def test_generate_prompt_with_work(workspace):
    """Test prompt generation with cached work items."""
    run = ProjectMonitoringRun(workspace)

    work_items = [
        WorkItem(
            repo="gptme/gptme",
            item_type="pr_update",
            number=123,
            title="Add feature",
            url="https://github.com/gptme/gptme/pull/123",
            details="PR #123 updated",
        )
    ]

    # Set cached work directly (simulates has_work() having been called)
    run._discovered_work = work_items
    prompt = run.generate_prompt()

    assert "gptme/gptme" in prompt
    assert "PR #123" in prompt
    assert "GREEN" in prompt
    assert "RED" in prompt


@patch("gptme_runloops.utils.executor.execute_gptme")
def test_execute_with_work(mock_execute, workspace):
    """Test execute when work is found."""
    # Mock gptme execution - use ExecutionResult
    from gptme_runloops.utils.execution import ExecutionResult

    mock_execute.return_value = ExecutionResult(exit_code=0, timed_out=False)

    # Create work items (simulates has_work() having been called)
    work_items = [
        WorkItem(
            repo="gptme/gptme",
            item_type="pr_update",
            number=123,
            title="Add feature",
            url="https://github.com/gptme/gptme/pull/123",
            details="PR #123 updated",
        )
    ]

    run = ProjectMonitoringRun(workspace)
    # Set cached work directly (simulates has_work() having been called)
    run._discovered_work = work_items

    prompt = run.generate_prompt()
    result = run.execute(prompt)

    assert result.exit_code == 0
    mock_execute.assert_called_once()


def test_execute_no_work(workspace):
    """Test execute when no work found."""
    run = ProjectMonitoringRun(workspace)

    with patch.object(run, "discover_work", return_value=[]):
        prompt = run.generate_prompt()
        result = run.execute(prompt)

        assert result.exit_code == 0
        assert result.success is True


def _notification(
    notif_id: str,
    subject_type: str,
    subject_url: str,
    latest_comment_url: str = "",
    reason: str = "mention",
    repo: str = "owner/repo",
    title: str = "Test notification",
) -> dict:
    """Build a GitHub notification payload as returned by `gh api notifications`."""
    return {
        "id": notif_id,
        "reason": reason,
        "unread": True,
        "repository": {"full_name": repo},
        "subject": {
            "title": title,
            "type": subject_type,
            "url": subject_url,
            "latest_comment_url": latest_comment_url,
        },
    }


def _run_check_notifications(run, notifications):
    """Invoke check_notifications with a mocked `gh api notifications` call."""
    mock_result = MagicMock(returncode=0, stdout=json.dumps(notifications))
    with patch(
        "gptme_runloops.project_monitoring.subprocess.run", return_value=mock_result
    ):
        return run.check_notifications()


def test_check_notifications_handles_commit_comments(workspace):
    """Commit-comment notifications must yield a WorkItem with a commit HTML URL."""
    run = ProjectMonitoringRun(workspace)
    items = _run_check_notifications(
        run,
        [
            _notification(
                "1",
                "Commit",
                "https://api.github.com/repos/owner/repo/commits/abc123def456",
                latest_comment_url="https://api.github.com/repos/owner/repo/comments/789",
            )
        ],
    )

    assert len(items) == 1
    item = items[0]
    assert item.item_type == "notification"
    assert item.number == 789
    assert (
        item.url
        == "https://github.com/owner/repo/commit/abc123def456#commitcomment-789"
    )
    assert "Type: Commit" in item.details


def test_check_notifications_pull_request_still_works(workspace):
    """PullRequest notifications keep their existing behavior."""
    run = ProjectMonitoringRun(workspace)
    items = _run_check_notifications(
        run,
        [
            _notification(
                "2",
                "PullRequest",
                "https://api.github.com/repos/owner/repo/pulls/123",
                reason="review_requested",
            )
        ],
    )

    assert len(items) == 1
    assert items[0].number == 123
    assert items[0].url == "https://github.com/owner/repo/pull/123"

    # Processed notifications are recorded in state so they aren't re-surfaced
    state_file = run.state_dir / "notifications.state"
    assert "2" in state_file.read_text().split("\n")


def test_check_notifications_unparseable_not_marked_processed(workspace):
    """Notifications we can't parse must NOT be marked processed.

    Previously they were saved to state on first sight and permanently
    skipped, even if a later code update (e.g. commit-comment support)
    made them parseable.
    """
    run = ProjectMonitoringRun(workspace)
    items = _run_check_notifications(
        run,
        [
            _notification(
                "3",
                "Discussion",
                "https://api.github.com/repos/owner/repo/discussions/55",
            )
        ],
    )

    assert items == []
    state_file = run.state_dir / "notifications.state"
    assert "3" not in state_file.read_text().split("\n")


def test_check_notifications_previously_processed_skipped(workspace):
    """Already-processed notifications are skipped but kept in state."""
    run = ProjectMonitoringRun(workspace)
    state_file = run.state_dir / "notifications.state"
    state_file.write_text("4")

    items = _run_check_notifications(
        run,
        [
            _notification(
                "4",
                "PullRequest",
                "https://api.github.com/repos/owner/repo/pulls/44",
            )
        ],
    )

    assert items == []
    assert "4" in state_file.read_text().split("\n")
