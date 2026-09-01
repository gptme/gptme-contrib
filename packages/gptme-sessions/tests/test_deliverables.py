from gptme_sessions.deliverables import (
    build_deliverable_detail,
    classify_commit_ownership,
    project_deliverable_details,
)


def test_classify_commit_ownership_trajectory_beats_missing_trailer():
    sha = "abc1234567890abcdef1234567890abcdef1234"
    assert classify_commit_ownership(sha, traj_sha_prefixes={"abc1234"}) == "trajectory_observed"


def test_classify_commit_ownership_trailer_match_is_owned():
    sha = "abc1234567890abcdef1234567890abcdef1234"
    assert (
        classify_commit_ownership(sha, session_id="73be", trailer_ids=["73be"])
        == "session_trailer_owned"
    )


def test_classify_commit_ownership_trailer_match_is_case_insensitive():
    sha = "abc1234567890abcdef1234567890abcdef1234"
    assert (
        classify_commit_ownership(sha, session_id="Session-Mine", trailer_ids=["session-mine"])
        == "session_trailer_owned"
    )


def test_classify_commit_ownership_other_trailer_is_foreign():
    sha = "abc1234567890abcdef1234567890abcdef1234"
    assert (
        classify_commit_ownership(sha, session_id="73be", trailer_ids=["6869"])
        == "explicitly_foreign"
    )


def test_classify_commit_ownership_untagged_is_ambiguous():
    sha = "b59e7f7f48353205eb6adbde9f06094b9dbf9f35"
    assert classify_commit_ownership(sha, session_id="73be") == "ambiguous"


def test_build_deliverable_detail_allows_explicit_kind_override():
    detail = build_deliverable_detail(
        "fix: ship thing (abc1234)",
        kind="commit",
        provenance_class="session_committed",
        evidence={"source": "trajectory", "tool_name": "shell"},
    )

    assert detail == {
        "value": "fix: ship thing (abc1234)",
        "kind": "commit",
        "provenance_class": "session_committed",
        "evidence": {"source": "trajectory", "tool_name": "shell"},
    }


def test_project_deliverable_details_gap_fills_missing_entries():
    details = project_deliverable_details(
        ["src/app.py", "abc1234567890abcdef1234567890abcdef1234"],
        {
            "src/app.py": build_deliverable_detail(
                "src/app.py",
                provenance_class="tool_authored",
                evidence={"source": "trajectory", "tool_name": "Write"},
            )
        },
        fallback_evidence={"source": "projection_fallback"},
    )

    assert details == [
        {
            "value": "src/app.py",
            "kind": "file",
            "provenance_class": "tool_authored",
            "evidence": {"source": "trajectory", "tool_name": "Write"},
        },
        {
            "value": "abc1234567890abcdef1234567890abcdef1234",
            "kind": "commit",
            "provenance_class": "fallback_observed",
            "evidence": {"source": "projection_fallback"},
        },
    ]
