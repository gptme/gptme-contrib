"""Public review outputs use the canonical v2 wire format, not internal v1."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from gptme_runloops.cli import main
from gptme_runloops.pr_review.core_compat import to_core_dict
from gptme_runloops.pr_review.github_adapter import run_github_review
from gptme_runloops.pr_review.reviewer import run_review
from gptme_runloops.pr_review.schema import (
    Disposition,
    LocalReviewTarget,
    MergeSafety,
    ReviewArtifact,
    ReviewFinding,
    ReviewTarget,
    Severity,
)


@pytest.fixture
def artifact():
    now = datetime.now(timezone.utc)
    return ReviewArtifact(
        target=ReviewTarget(
            repo="org/repo", pr_number=42, base_sha="a" * 40, head_sha="b" * 40
        ),
        model="test",
        prompt_version="v1",
        started_at=now,
        completed_at=now,
        summary="A live defect",
        merge_safety=MergeSafety.unsafe,
        findings=[
            ReviewFinding(
                id="stable-id",
                category="correctness",
                severity=Severity.high,
                confidence=0.9,
                file_path="example.py",
                line_range="10-12",
                title="Null dereference",
                description="The value can be None.",
                evidence="value.lower()",
                disposition=Disposition.confirmed,
            )
        ],
    )


def assert_canonical(data, artifact):
    assert data == to_core_dict(artifact)
    assert data["schema_version"] == 2
    assert data["findings"][0]["severity"] == "error"
    # A confirmed defect must remain actionable, not become a confirmed fix.
    assert data["findings"][0]["status"] == "open"
    assert data["findings"][0]["independence_key"] == "stable-id"


@pytest.mark.parametrize("hosted", [False, True])
def test_cli_stdout_is_canonical(artifact, tmp_path, hosted):
    if hosted:
        with patch(
            "gptme_runloops.pr_review.github_adapter.run_github_review",
            return_value=(artifact, 0, 0),
        ):
            result = CliRunner().invoke(main, ["review-pr", "org/repo#42"])
    else:
        artifact.target = LocalReviewTarget(
            checkout=str(tmp_path), base_sha="a" * 40, diff_fingerprint="diff-id"
        )
        with (
            patch(
                "gptme_runloops.pr_review.reviewer.resolve_local_target",
                return_value=(artifact.target, "+change"),
            ),
            patch(
                "gptme_runloops.pr_review.reviewer.run_review", return_value=artifact
            ),
        ):
            result = CliRunner().invoke(
                main, ["review", "--checkout", str(tmp_path), "--working-tree"]
            )
    assert result.exit_code == 1  # unsafe review is still nonzero
    assert_canonical(json.loads(result.stdout), artifact)


def test_reviewer_saved_output_is_canonical(artifact, tmp_path):
    response = {
        "summary": artifact.summary,
        "merge_safety": "unsafe",
        "findings": [artifact.findings[0].model_dump(mode="json")],
    }
    output = tmp_path / "nested" / "artifact.json"
    with patch(
        "gptme_runloops.pr_review.reviewer._invoke_model",
        return_value=json.dumps(response),
    ):
        reviewed = run_review(tmp_path, artifact.target, "+change", output_path=output)
    # Internal models remain v1 for the verifier and publisher during migration.
    assert reviewed.schema_version == "v1"
    data = json.loads(output.read_text())
    assert data == to_core_dict(reviewed)
    assert data["schema_version"] == 2
    assert data["findings"][0]["status"] == "open"


@pytest.mark.parametrize("verify", [False, True])
def test_hosted_saved_output_is_final_canonical(artifact, tmp_path, verify):
    output = tmp_path / "nested" / "artifact.json"

    def verify_review(reviewed, *_args, **_kwargs):
        assert json.loads(output.read_text())["schema_version"] == 2
        reviewed.findings[0].title = "Verified null dereference"
        reviewed.merge_safety = MergeSafety.needs_review
        return reviewed

    with (
        patch(
            "gptme_runloops.pr_review.github_adapter.fetch_pr_metadata",
            return_value={"base_sha": "a" * 40, "head_sha": "b" * 40},
        ),
        patch(
            "gptme_runloops.pr_review.github_adapter.fetch_pr_diff",
            return_value="+change",
        ),
        patch(
            "gptme_runloops.pr_review.reviewer._invoke_model",
            return_value=json.dumps(
                {
                    "summary": artifact.summary,
                    "merge_safety": "unsafe",
                    "findings": [artifact.findings[0].model_dump(mode="json")],
                }
            ),
        ),
        patch(
            "gptme_runloops.pr_review.verifier.verify_artifact",
            side_effect=verify_review,
        ),
        patch(
            "gptme_runloops.pr_review.github_adapter.publish_artifact",
            return_value=(0, 0),
        ),
    ):
        reviewed, _, _ = run_github_review(
            "org/repo", 42, checkout=tmp_path, output_path=output, verify=verify
        )
    data = json.loads(output.read_text())
    assert data == to_core_dict(reviewed)
    assert data["schema_version"] == 2
    if verify:
        assert reviewed.findings[0].title == "Verified null dereference"
        assert reviewed.merge_safety == MergeSafety.needs_review


@pytest.mark.parametrize("local", [False, True])
def test_core_schema_and_reader_roundtrip(artifact, tmp_path, local):
    """Optional contract check against a core build exposing the v2 schema."""
    core = pytest.importorskip("gptme.util.review")
    jsonschema = pytest.importorskip("jsonschema")
    if not hasattr(core, "REVIEW_ARTIFACT_SCHEMA_V2"):
        pytest.skip("installed gptme predates the canonical v2 JSON Schema")
    if local:
        artifact.target = LocalReviewTarget(
            checkout=str(tmp_path),
            repo_name="org/repo",
            base_sha="a" * 40,
            diff_fingerprint="working-tree-diff",
        )
    from gptme_runloops.pr_review.core_compat import artifact_json

    wire = json.loads(artifact_json(artifact))
    validator = jsonschema.Draft7Validator(core.REVIEW_ARTIFACT_SCHEMA_V2)
    validator.validate(wire)
    loaded = core.ReviewArtifact.from_dict(wire)
    assert loaded.review_status == core.ReviewStatus.COMPLETE
    assert loaded.validation_errors == 0
    assert loaded.merge_safety == core.MergeSafety.UNSAFE
    assert loaded.open_findings[0].severity == core.FindingSeverity.ERROR
    assert loaded.open_findings[0].independence_key == "stable-id"
    assert loaded.target_kind == ("local" if local else "hosted")
    reread = core.ReviewArtifact.from_json(loaded.to_json())
    assert reread.to_dict() == loaded.to_dict()
    validator.validate(reread.to_dict())


def test_saved_artifact_survives_publication_failure(artifact, tmp_path):
    output = tmp_path / "artifact.json"
    response = {
        "summary": artifact.summary,
        "merge_safety": "unsafe",
        "findings": [artifact.findings[0].model_dump(mode="json")],
    }
    with (
        patch(
            "gptme_runloops.pr_review.github_adapter.fetch_pr_metadata",
            return_value={"base_sha": "a" * 40, "head_sha": "b" * 40},
        ),
        patch(
            "gptme_runloops.pr_review.github_adapter.fetch_pr_diff",
            return_value="+change",
        ),
        patch(
            "gptme_runloops.pr_review.reviewer._invoke_model",
            return_value=json.dumps(response),
        ),
        patch(
            "gptme_runloops.pr_review.github_adapter.publish_artifact",
            side_effect=RuntimeError("forge unavailable"),
        ),
        pytest.raises(RuntimeError, match="forge unavailable"),
    ):
        run_github_review("org/repo", 42, checkout=tmp_path, output_path=output)
    data = json.loads(output.read_text())
    assert data["schema_version"] == 2
    assert data["merge_safety"] == "unsafe"
    assert len(data["findings"]) == 1
