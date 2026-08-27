"""Tests for the adversarial verifier (Stage 2 of the two-stage review pipeline).

All model calls are mocked — these tests validate the logic, schema parsing,
shadow ledger writes, and artifact mutation, not the LLM output quality.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from gptme_runloops.pr_review.schema import (
    Disposition,
    LocalReviewTarget,
    MergeSafety,
    ReviewArtifact,
    ReviewFinding,
    Severity,
)
from gptme_runloops.pr_review.verifier import (
    VerifierVerdict,
    _build_verifier_prompt,
    _extract_verifier_json,
    verify_artifact,
    verify_finding,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_finding(
    title: str = "Test finding",
    severity: Severity = Severity.medium,
    disposition: Disposition = Disposition.needs_validation,
) -> ReviewFinding:
    fp = ReviewFinding.local_fingerprint(
        repo="test/repo",
        head_sha="abc123",
        file_path="foo.py",
        title=title,
        prompt_version="v1",
    )
    return ReviewFinding(
        id=fp,
        category="correctness",
        severity=severity,
        confidence=0.8,
        file_path="foo.py",
        line_range="10-15",
        title=title,
        description="A test finding description.",
        evidence="x = unsafe_call()",
        fix_hint="Use safe_call() instead.",
        disposition=disposition,
    )


def _make_artifact(findings: list[ReviewFinding]) -> ReviewArtifact:
    target = LocalReviewTarget(
        checkout="/tmp/test-repo",
        repo_name="test/repo",
        base_sha="base000",
        head_sha="head111",
    )
    return ReviewArtifact(
        target=target,
        model="test-model",
        prompt_version="v1",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        summary="Test changes",
        merge_safety=MergeSafety.needs_review,
        findings=findings,
    )


SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -8,7 +8,10 @@
 def do_thing():
     x = setup()
-    return old_call(x)
+    return unsafe_call(x)
"""


# ── _extract_verifier_json ────────────────────────────────────────────────────


class TestExtractVerifierJson:
    def test_clean_json(self):
        text = '{"real": true, "worth_fixing": false, "severity": "low", "rationale": "nit"}'
        result = _extract_verifier_json(text)
        assert result["real"] is True
        assert result["worth_fixing"] is False

    def test_fenced_json(self):
        text = '```json\n{"real": false, "worth_fixing": false, "severity": "info", "rationale": "hypothetical"}\n```'
        result = _extract_verifier_json(text)
        assert result["real"] is False

    def test_json_with_preamble(self):
        text = 'Here is my analysis:\n{"real": true, "worth_fixing": true, "severity": "high", "rationale": "real bug"}'
        result = _extract_verifier_json(text)
        assert result["real"] is True
        assert result["severity"] == "high"

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="No valid JSON"):
            _extract_verifier_json("no json here at all")


# ── _build_verifier_prompt ────────────────────────────────────────────────────


class TestBuildVerifierPrompt:
    def test_includes_finding_title(self):
        finding = _make_finding(title="My unique finding title")
        prompt = _build_verifier_prompt(finding, SAMPLE_DIFF)
        assert "My unique finding title" in prompt

    def test_includes_diff(self):
        finding = _make_finding()
        prompt = _build_verifier_prompt(finding, SAMPLE_DIFF)
        assert "unsafe_call" in prompt

    def test_truncates_large_diff(self):
        finding = _make_finding()
        large_diff = "x" * 100_000
        prompt = _build_verifier_prompt(finding, large_diff)
        assert "TRUNCATED" in prompt

    def test_contains_severity(self):
        finding = _make_finding(severity=Severity.critical)
        prompt = _build_verifier_prompt(finding, SAMPLE_DIFF)
        assert "critical" in prompt


# ── VerifierVerdict ───────────────────────────────────────────────────────────


class TestVerifierVerdict:
    def test_fields(self):
        v = VerifierVerdict(
            real=True,
            worth_fixing=False,
            severity=Severity.low,
            rationale="Style nit, not worth blocking merge.",
        )
        assert v.real is True
        assert v.worth_fixing is False
        assert v.severity == Severity.low
        assert "Style" in v.rationale

    def test_severity_promotion(self):
        """A verifier that promotes severity from medium to critical is valid."""
        v = VerifierVerdict(
            real=True,
            worth_fixing=True,
            severity=Severity.critical,
            rationale="SQL injection via user-controlled input.",
        )
        assert v.severity == Severity.critical


# ── verify_finding ────────────────────────────────────────────────────────────


class TestVerifyFinding:
    def test_confirmed_finding(self):
        finding = _make_finding()
        model_output = json.dumps(
            {
                "real": True,
                "worth_fixing": True,
                "severity": "high",
                "rationale": "Confirmed: unsafe_call is reachable without validation.",
            }
        )
        with patch(
            "gptme_runloops.pr_review.verifier._invoke_verifier_model",
            return_value=model_output,
        ):
            verdict = verify_finding(finding, SAMPLE_DIFF, Path("/tmp"))
        assert verdict.real is True
        assert verdict.worth_fixing is True
        assert verdict.severity == Severity.high

    def test_refuted_not_real(self):
        finding = _make_finding()
        model_output = json.dumps(
            {
                "real": False,
                "worth_fixing": False,
                "severity": "info",
                "rationale": "unsafe_call is already validated by the caller.",
            }
        )
        with patch(
            "gptme_runloops.pr_review.verifier._invoke_verifier_model",
            return_value=model_output,
        ):
            verdict = verify_finding(finding, SAMPLE_DIFF, Path("/tmp"))
        assert verdict.real is False

    def test_fallback_on_bad_severity(self):
        finding = _make_finding(severity=Severity.medium)
        model_output = json.dumps(
            {
                "real": True,
                "worth_fixing": True,
                "severity": "not-a-valid-severity",
                "rationale": "ok",
            }
        )
        with patch(
            "gptme_runloops.pr_review.verifier._invoke_verifier_model",
            return_value=model_output,
        ):
            verdict = verify_finding(finding, SAMPLE_DIFF, Path("/tmp"))
        # Bad severity falls back to original finding severity
        assert verdict.severity == Severity.medium


# ── verify_artifact ───────────────────────────────────────────────────────────


class TestVerifyArtifact:
    def test_confirmed_findings_updated(self):
        findings = [_make_finding("Bug A"), _make_finding("Bug B")]
        artifact = _make_artifact(findings)

        model_output = json.dumps(
            {
                "real": True,
                "worth_fixing": True,
                "severity": "high",
                "rationale": "Genuine bug.",
            }
        )
        with patch(
            "gptme_runloops.pr_review.verifier._invoke_verifier_model",
            return_value=model_output,
        ):
            result = verify_artifact(artifact, SAMPLE_DIFF, Path("/tmp"))

        assert all(f.disposition == Disposition.confirmed for f in result.findings)
        assert all(f.severity == Severity.high for f in result.findings)
        assert all(f.validated_by is not None for f in result.findings)

    def test_dropped_findings_excluded(self):
        findings = [_make_finding("Nit"), _make_finding("Real bug")]
        artifact = _make_artifact(findings)

        call_count = 0

        def mock_invoke(prompt, model, checkout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First finding: nit, not worth fixing
                return json.dumps(
                    {
                        "real": True,
                        "worth_fixing": False,
                        "severity": "info",
                        "rationale": "Style nit.",
                    }
                )
            else:
                # Second finding: real bug
                return json.dumps(
                    {
                        "real": True,
                        "worth_fixing": True,
                        "severity": "high",
                        "rationale": "Real correctness issue.",
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "suppressed.jsonl"
            with patch(
                "gptme_runloops.pr_review.verifier._invoke_verifier_model",
                side_effect=mock_invoke,
            ):
                result = verify_artifact(
                    artifact, SAMPLE_DIFF, Path("/tmp"), shadow_ledger=ledger
                )

            dispositions = [f.disposition for f in result.findings]
            assert dispositions.count(Disposition.confirmed) == 1
            assert dispositions.count(Disposition.dropped) == 1

            # Dropped finding should be in shadow ledger
            assert ledger.exists()
            records = [json.loads(line) for line in ledger.read_text().splitlines()]
            assert len(records) == 1
            assert records[0]["title"] == "Nit"
            assert records[0]["worth_fixing"] is False

    def test_shadow_ledger_records_all_fields(self):
        findings = [_make_finding("Fake finding")]
        artifact = _make_artifact(findings)

        model_output = json.dumps(
            {
                "real": False,
                "worth_fixing": False,
                "severity": "info",
                "rationale": "Hypothetical scenario not present in the diff.",
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "suppressed.jsonl"
            with patch(
                "gptme_runloops.pr_review.verifier._invoke_verifier_model",
                return_value=model_output,
            ):
                verify_artifact(
                    artifact, SAMPLE_DIFF, Path("/tmp"), shadow_ledger=ledger
                )

            records = [json.loads(line) for line in ledger.read_text().splitlines()]
            assert len(records) == 1
            r = records[0]
            assert r["title"] == "Fake finding"
            assert r["real"] is False
            assert r["worth_fixing"] is False
            assert "rationale" in r
            assert "ts" in r
            assert r["repo"] == "test/repo"

    def test_already_decided_findings_skipped(self):
        """Findings already marked confirmed/dropped are not re-verified."""
        findings = [
            _make_finding("Pre-confirmed", disposition=Disposition.confirmed),
            _make_finding("Pre-dropped", disposition=Disposition.dropped),
        ]
        artifact = _make_artifact(findings)

        invoke_mock = MagicMock()
        with patch(
            "gptme_runloops.pr_review.verifier._invoke_verifier_model",
            invoke_mock,
        ):
            verify_artifact(artifact, SAMPLE_DIFF, Path("/tmp"))

        invoke_mock.assert_not_called()

    def test_error_leaves_needs_validation(self):
        """On tool failure, finding stays as needs_validation (conservative)."""
        findings = [_make_finding("Risky")]
        artifact = _make_artifact(findings)

        with patch(
            "gptme_runloops.pr_review.verifier._invoke_verifier_model",
            side_effect=RuntimeError("gptme timed out"),
        ):
            result = verify_artifact(artifact, SAMPLE_DIFF, Path("/tmp"))

        assert result.findings[0].disposition == Disposition.needs_validation
        assert "verifier error" in result.findings[0].validation_note

    def test_severity_promotion(self):
        """Verifier can promote a medium generator finding to critical."""
        findings = [_make_finding("SQLi vuln", severity=Severity.medium)]
        artifact = _make_artifact(findings)

        model_output = json.dumps(
            {
                "real": True,
                "worth_fixing": True,
                "severity": "critical",
                "rationale": "SQL injection via user-controlled parameter.",
            }
        )
        with patch(
            "gptme_runloops.pr_review.verifier._invoke_verifier_model",
            return_value=model_output,
        ):
            result = verify_artifact(artifact, SAMPLE_DIFF, Path("/tmp"))

        assert result.findings[0].severity == Severity.critical
        assert result.findings[0].disposition == Disposition.confirmed
