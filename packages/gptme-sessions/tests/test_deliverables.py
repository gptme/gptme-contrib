from gptme_sessions.deliverables import build_deliverable_detail, project_deliverable_details


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
