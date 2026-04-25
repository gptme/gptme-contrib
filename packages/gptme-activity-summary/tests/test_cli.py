"""Tests for activity summary CLI helpers."""

from datetime import date
from unittest.mock import patch

from gptme_activity_summary.cli import generate_daily_with_cc
from gptme_activity_summary.github_data import GitHubActivity, PRReview
from gptme_activity_summary.session_data import SessionStats


def test_generate_daily_with_cc_deduplicates_matching_github_reviews(tmp_path):
    """LLM interactions and GitHub-derived reviews should not duplicate the same bullet."""
    target_date = date(2025, 1, 7)
    entry = tmp_path / "entry.md"
    entry.write_text("# Entry\n")

    activity = GitHubActivity(
        start_date=target_date,
        end_date=target_date,
        reviews_received=[
            PRReview(
                repo="gptme/gptme",
                pr_number=42,
                pr_title="Fix duplicated summary lines",
                reviewer="greptile-apps",
                url="https://github.com/gptme/gptme/pull/42",
            )
        ],
    )
    session_stats = SessionStats(start_date=target_date, end_date=target_date)
    llm_result = {
        "interactions": [
            {
                "type": "github_review",
                "person": "greptile-apps",
                "summary": "Reviewed gptme/gptme#42: Fix duplicated summary lines",
                "url": "https://github.com/gptme/gptme/pull/42",
            }
        ]
    }

    with (
        patch(
            "gptme_activity_summary.cli.get_journal_entries_for_date",
            return_value=[entry],
        ),
        patch("gptme_activity_summary.cli._fetch_data", return_value=(activity, session_stats)),
        patch("gptme_activity_summary.cli._build_extra_context", return_value=""),
        patch("gptme_activity_summary.cc_backend.summarize_daily_with_cc", return_value=llm_result),
    ):
        summary = generate_daily_with_cc(target_date)

    assert len(summary.interactions) == 1
    assert summary.interactions[0].person == "greptile-apps"
    assert (
        summary.interactions[0].summary == "Reviewed gptme/gptme#42: Fix duplicated summary lines"
    )
