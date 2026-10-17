"""Tests for gptme_runloops.pr_review.core_compat — forge-neutral → core conversion."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from gptme_runloops.pr_review.core_compat import (
    _DISPOSITION_MAP,
    _SEVERITY_MAP,
    finding_to_core_dict,
    to_core_dict,
)
from gptme_runloops.pr_review.schema import (
    Disposition,
    LocalReviewTarget,
    MergeSafety,
    ReviewArtifact,
    ReviewFinding,
    ReviewTarget,
    Severity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
_HEAD = "abc1234567890def"
_BASE = "00000000deadbeef"


def _finding(**kwargs) -> ReviewFinding:
    defaults = dict(
        id="fp0000000000",
        category="correctness",
        severity=Severity.high,
        confidence=0.9,
        file_path="gptme/util/review.py",
        line_range="42-49",
        title="Missing null check",
        description="d.get() may return None here.",
        evidence="line 42: d.get('key').lower()",
        disposition=Disposition.confirmed,
    )
    defaults.update(kwargs)
    return ReviewFinding(**defaults)


def _hosted_artifact(findings=None, merge_safety=MergeSafety.safe) -> ReviewArtifact:
    return ReviewArtifact(
        target=ReviewTarget(
            repo="gptme/gptme",
            pr_number=4390,
            base_sha=_BASE,
            head_sha=_HEAD,
        ),
        model="gpt-4o",
        prompt_version="v2",
        started_at=_NOW,
        completed_at=_NOW,
        summary="Looks good.",
        merge_safety=merge_safety,
        findings=findings or [],
    )


def _local_artifact(findings=None) -> ReviewArtifact:
    return ReviewArtifact(
        target=LocalReviewTarget(
            checkout="/tmp/gptme",
            repo_name="gptme/gptme",
            base_sha=_BASE,
            head_sha=None,
            diff_fingerprint="deadbeef0000",
        ),
        model="gpt-4o",
        prompt_version="v2",
        started_at=_NOW,
        completed_at=_NOW,
        summary="Working-tree review.",
        merge_safety=MergeSafety.needs_review,
        findings=findings or [],
    )


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------


class TestSeverityMapping:
    def test_all_levels_present(self):
        for level in ("critical", "high", "medium", "low", "info"):
            assert level in _SEVERITY_MAP, f"missing {level!r}"

    @pytest.mark.parametrize(
        "runloops,expected",
        [
            ("critical", "critical"),
            ("high", "error"),
            ("medium", "warning"),
            ("low", "note"),
            ("info", "note"),
        ],
    )
    def test_values(self, runloops, expected):
        assert _SEVERITY_MAP[runloops] == expected


# ---------------------------------------------------------------------------
# Disposition mapping
# ---------------------------------------------------------------------------


class TestDispositionMapping:
    @pytest.mark.parametrize(
        "runloops,expected",
        [
            ("confirmed", "open"),
            ("needs_validation", "open"),
            ("dropped", "dropped"),
        ],
    )
    def test_values(self, runloops, expected):
        assert _DISPOSITION_MAP[runloops] == expected

    def test_confirmed_is_open_not_confirmed(self):
        """Validated defects must stay open — they are not yet fixed."""
        assert _DISPOSITION_MAP["confirmed"] == "open"


# ---------------------------------------------------------------------------
# finding_to_core_dict
# ---------------------------------------------------------------------------


class TestFindingToCoreDict:
    def test_basic_fields(self):
        f = _finding()
        d = finding_to_core_dict(f)
        assert d["file"] == "gptme/util/review.py"
        assert d["line"] == 42
        assert d["severity"] == "error"  # high → error
        assert d["status"] == "open"  # confirmed → open

    def test_body_joins_title_description_evidence(self):
        f = _finding(title="T", description="D", evidence="E")
        d = finding_to_core_dict(f)
        assert d["body"] == "T\n\nD\n\nE"

    def test_independence_key_from_id(self):
        f = _finding(id="myfingerprint")
        d = finding_to_core_dict(f)
        assert d["independence_key"] == "myfingerprint"

    def test_empty_id_not_in_dict(self):
        f = _finding(id="")
        d = finding_to_core_dict(f)
        assert "independence_key" not in d

    def test_category_preserved(self):
        f = _finding(category="security")
        d = finding_to_core_dict(f)
        assert d["category"] == "security"

    def test_empty_category_not_in_dict(self):
        f = _finding(category="")
        d = finding_to_core_dict(f)
        assert "category" not in d

    def test_dropped_disposition(self):
        f = _finding(disposition=Disposition.dropped)
        d = finding_to_core_dict(f)
        assert d["status"] == "dropped"

    def test_needs_validation_disposition(self):
        f = _finding(disposition=Disposition.needs_validation)
        d = finding_to_core_dict(f)
        assert d["status"] == "open"

    def test_line_range_point(self):
        f = _finding(line_range="99")
        d = finding_to_core_dict(f)
        assert d["line"] == 99

    def test_line_range_dash(self):
        f = _finding(line_range="10-20")
        d = finding_to_core_dict(f)
        assert d["line"] == 10

    def test_all_severity_levels(self):
        expected = {
            Severity.critical: "critical",
            Severity.high: "error",
            Severity.medium: "warning",
            Severity.low: "note",
            Severity.info: "note",
        }
        for sev, exp in expected.items():
            f = _finding(severity=sev)
            d = finding_to_core_dict(f)
            assert d["severity"] == exp, f"severity {sev!r} → expected {exp!r}"

    def test_no_private_fields_leaked(self):
        """Fields without core equivalents must not appear in the output."""
        f = _finding()
        d = finding_to_core_dict(f)
        assert "confidence" not in d
        assert "validated_by" not in d
        assert "validation_note" not in d
        assert "published" not in d
        assert "fix_hint" not in d
        assert "line_side" not in d


# ---------------------------------------------------------------------------
# to_core_dict — hosted target
# ---------------------------------------------------------------------------


class TestToCoreDict_Hosted:
    def test_schema_version(self):
        d = to_core_dict(_hosted_artifact())
        assert d["schema_version"] == 2

    def test_pr_identity(self):
        d = to_core_dict(_hosted_artifact())
        assert d["pr"]["owner"] == "gptme"
        assert d["pr"]["repo"] == "gptme"
        assert d["pr"]["number"] == 4390

    def test_target_kind(self):
        d = to_core_dict(_hosted_artifact())
        assert d["target_kind"] == "hosted"

    def test_no_diff_fingerprint_for_hosted(self):
        d = to_core_dict(_hosted_artifact())
        assert "diff_fingerprint" not in d or d.get("diff_fingerprint") == ""

    def test_merge_safety_safe(self):
        d = to_core_dict(_hosted_artifact(merge_safety=MergeSafety.safe))
        assert d["merge_safety"] == "safe"

    def test_merge_safety_unsafe(self):
        d = to_core_dict(_hosted_artifact(merge_safety=MergeSafety.unsafe))
        assert d["merge_safety"] == "unsafe"

    def test_merge_safety_needs_review(self):
        d = to_core_dict(_hosted_artifact(merge_safety=MergeSafety.needs_review))
        assert d["merge_safety"] == "needs_review"

    def test_review_status_complete_on_clean(self):
        d = to_core_dict(_hosted_artifact())
        assert d["review_status"] == "complete"

    def test_empty_findings(self):
        d = to_core_dict(_hosted_artifact(findings=[]))
        assert d["findings"] == []
        assert d["validation_errors"] == 0

    def test_findings_converted(self):
        f = _finding()
        d = to_core_dict(_hosted_artifact(findings=[f]))
        assert len(d["findings"]) == 1
        fd = d["findings"][0]
        assert fd["file"] == "gptme/util/review.py"
        assert fd["severity"] == "error"

    def test_conversion_error_propagates(self):
        """Never return a partial artifact when a finding cannot be converted."""
        malformed = _finding().model_copy(update={"title": object()})
        artifact = _hosted_artifact(findings=[_finding(), malformed])

        with pytest.raises(AttributeError, match="strip"):
            to_core_dict(artifact)

    def test_multiple_findings(self):
        findings = [
            _finding(title="A", id="fp1", severity=Severity.critical),
            _finding(
                title="B",
                id="fp2",
                severity=Severity.low,
                disposition=Disposition.dropped,
            ),
        ]
        d = to_core_dict(_hosted_artifact(findings=findings))
        assert len(d["findings"]) == 2
        assert d["findings"][0]["severity"] == "critical"
        assert d["findings"][1]["severity"] == "note"
        assert d["findings"][1]["status"] == "dropped"


# ---------------------------------------------------------------------------
# to_core_dict — local target
# ---------------------------------------------------------------------------


class TestToCoreDict_Local:
    def test_target_kind_local(self):
        d = to_core_dict(_local_artifact())
        assert d["target_kind"] == "local"

    def test_diff_fingerprint_preserved(self):
        d = to_core_dict(_local_artifact())
        assert d["diff_fingerprint"] == "deadbeef0000"

    def test_merge_safety_needs_review(self):
        d = to_core_dict(_local_artifact())
        assert d["merge_safety"] == "needs_review"

    @pytest.mark.parametrize(
        "repo_name,owner,repo",
        [("gptme/gptme", "gptme", "gptme"), ("gptme", "", "gptme"), ("", "", "")],
    )
    def test_pr_coordinates_from_repo_name(self, repo_name, owner, repo):
        artifact = _local_artifact()
        artifact.target.repo_name = repo_name
        d = to_core_dict(artifact)
        assert d["pr"] == {"owner": owner, "repo": repo, "number": 0}

    def test_schema_version(self):
        d = to_core_dict(_local_artifact())
        assert d["schema_version"] == 2


# ---------------------------------------------------------------------------
# Round-trip: to_core_dict output is parseable by a naive reader
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Verify the output dict structure is consumable with basic dict operations.

    We cannot import gptme.util.review here (no gptme dep), so we verify the
    dict structure is what from_dict expects, not the Python object.
    """

    def test_required_fields_present(self):
        d = to_core_dict(_hosted_artifact(findings=[_finding()]))
        assert "schema_version" in d
        assert "pr" in d
        assert "review_status" in d
        assert "merge_safety" in d
        assert "findings" in d
        assert "validation_errors" in d

    def test_finding_required_fields(self):
        d = to_core_dict(_hosted_artifact(findings=[_finding()]))
        fd = d["findings"][0]
        assert "body" in fd
        assert "file" in fd
        assert "severity" in fd
        assert "status" in fd

    def test_hosted_repo_requires_owner(self):
        """A hosted target needs qualified coordinates, unlike a local display name."""
        art = ReviewArtifact(
            target=ReviewTarget(
                repo="singlename",
                pr_number=1,
                base_sha=_BASE,
                head_sha=_HEAD,
            ),
            model="m",
            prompt_version="v1",
            started_at=_NOW,
            completed_at=_NOW,
            summary="x",
            merge_safety=MergeSafety.unknown,
        )
        with pytest.raises(ValueError, match="owner/repo"):
            to_core_dict(art)
