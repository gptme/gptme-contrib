"""Tests for the pr_review schema and corpus module (Phase 0 + Phase 2)."""

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from gptme_runloops.cli import main as cli_main
from gptme_runloops.pr_review import (
    Disposition,
    LocalReviewTarget,
    MergeSafety,
    ReviewArtifact,
    ReviewFinding,
    ReviewTarget,
    Severity,
    resolve_local_target,
)
from gptme_runloops.pr_review.corpus import (
    CorpusEntry,
    EvalResult,
    GroundTruthFinding,
    load_corpus,
    score_model,
)
from gptme_runloops.pr_review.reviewer import (
    _build_prompt,
    _diff_fingerprint,
    _extract_json,
    read_repo_instructions,
)
from gptme_runloops.pr_review.schema import SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"


# ── Schema tests ─────────────────────────────────────────────────────────────


class TestReviewFindingFingerprint:
    def test_stable(self):
        fp1 = ReviewFinding.fingerprint(
            repo="gptme/gptme-contrib",
            pr_number=1341,
            head_sha="abc123",
            file_path="deptree.py",
            title="Node ID collision",
            prompt_version="v1",
        )
        fp2 = ReviewFinding.fingerprint(
            repo="gptme/gptme-contrib",
            pr_number=1341,
            head_sha="abc123",
            file_path="deptree.py",
            title="Node ID collision",
            prompt_version="v1",
        )
        assert fp1 == fp2

    def test_differs_on_head_sha(self):
        common = dict(
            repo="gptme/gptme-contrib",
            pr_number=1341,
            file_path="deptree.py",
            title="Node ID collision",
            prompt_version="v1",
        )
        assert ReviewFinding.fingerprint(
            head_sha="abc", **common
        ) != ReviewFinding.fingerprint(head_sha="def", **common)

    def test_differs_on_prompt_version(self):
        common = dict(
            repo="gptme/gptme-contrib",
            pr_number=1341,
            head_sha="abc123",
            file_path="deptree.py",
            title="Node ID collision",
        )
        assert ReviewFinding.fingerprint(
            prompt_version="v1", **common
        ) != ReviewFinding.fingerprint(prompt_version="v2", **common)

    def test_length(self):
        fp = ReviewFinding.fingerprint(
            repo="r",
            pr_number=1,
            head_sha="h",
            file_path="f",
            title="t",
            prompt_version="v1",
        )
        assert len(fp) == 16


class TestReviewArtifact:
    def _artifact(self, findings=None):
        return ReviewArtifact(
            target=ReviewTarget(
                repo="gptme/gptme-contrib",
                pr_number=1341,
                base_sha="base",
                head_sha="head",
            ),
            model="deepseek/deepseek-v4-flash",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="Two real bugs found.",
            merge_safety=MergeSafety.unsafe,
            findings=findings or [],
            dry_run=True,
        )

    def test_schema_version(self):
        a = self._artifact()
        assert a.schema_version == SCHEMA_VERSION

    def test_confirmed_findings_filter(self):
        f1 = ReviewFinding(
            id="a",
            category="correctness",
            severity=Severity.high,
            confidence=0.9,
            file_path="deptree.py",
            line_range="42",
            title="T1",
            description="D1",
            evidence="E1",
            disposition=Disposition.confirmed,
        )
        f2 = ReviewFinding(
            id="b",
            category="security",
            severity=Severity.medium,
            confidence=0.7,
            file_path="deptree.py",
            line_range="55",
            title="T2",
            description="D2",
            evidence="E2",
            disposition=Disposition.dropped,
        )
        a = self._artifact(findings=[f1, f2])
        assert len(a.confirmed_findings) == 1
        assert a.confirmed_findings[0].title == "T1"

    def test_token_id_format(self):
        a = self._artifact()
        assert "gptme/gptme-contrib#1341@" in a.token_id

    def test_roundtrip_json(self):
        a = self._artifact()
        dumped = a.model_dump_json()
        loaded = ReviewArtifact.model_validate_json(dumped)
        assert loaded.schema_version == a.schema_version
        assert loaded.target.pr_number == 1341

    def test_dry_run_default(self):
        a = self._artifact()
        assert a.dry_run is True
        assert a.published_at is None


# ── Corpus tests ─────────────────────────────────────────────────────────────


class TestCorpusEntry:
    def _entry(self) -> CorpusEntry:
        return CorpusEntry(
            entry_id="test-1",
            repo="gptme/gptme-contrib",
            pr_number=1341,
            pr_title="feat: Mermaid DAG",
            pr_description="Adds mermaid output",
            diff_summary="deptree.py: new render_dag_mermaid()",
            ground_truth=[
                GroundTruthFinding(
                    label="true_positive",
                    category="correctness",
                    severity="high",
                    file_path="deptree.py",
                    title="Node ID collision",
                    description="Short SHA prefix can collide.",
                    verification="Greptile flagged; fixed in round 2.",
                ),
                GroundTruthFinding(
                    label="false_positive",
                    category="security",
                    severity="medium",
                    file_path="deptree.py",
                    title="Spurious FP",
                    description="Not actually an issue.",
                    verification="Reviewed and dismissed.",
                ),
            ],
        )

    def test_true_positive_filter(self):
        e = self._entry()
        assert len(e.true_positives) == 1
        assert e.true_positives[0].title == "Node ID collision"

    def test_false_positive_filter(self):
        e = self._entry()
        assert len(e.false_positives) == 1

    def test_to_model_input_excludes_ground_truth(self):
        e = self._entry()
        model_input = e.to_model_input()
        # Ground truth (with post-merge verification notes) must not leak to the model
        assert "ground_truth" not in model_input
        assert "verification" not in str(model_input)

    def test_to_model_input_includes_diff_fields(self):
        e = self._entry()
        model_input = e.to_model_input()
        assert model_input["entry_id"] == "test-1"
        assert model_input["pr_title"] == "feat: Mermaid DAG"
        assert model_input["diff_summary"] == "deptree.py: new render_dag_mermaid()"


class TestScoreModel:
    def test_perfect_precision(self):
        corpus = [
            CorpusEntry(
                entry_id="e1",
                repo="r",
                pr_number=1,
                pr_title="t",
                pr_description="d",
                diff_summary="s",
                ground_truth=[
                    GroundTruthFinding(
                        label="true_positive",
                        category="correctness",
                        severity="high",
                        file_path="f.py",
                        title="Bug",
                        description="A bug",
                    )
                ],
            )
        ]
        results = [
            EvalResult(
                entry_id="e1",
                model="test-model",
                matched_tp=[(0, "Bug", 1.0, "f.py")],
                false_positives_produced=0,
            )
        ]
        scores = score_model(results, corpus)
        assert scores.precision == 1.0
        assert scores.recall == 1.0
        assert scores.fp_rate == 0.0
        assert scores.location_accuracy == 1.0

    def test_location_accuracy_compares_file_paths(self):
        corpus = [
            CorpusEntry(
                entry_id="e1",
                repo="r",
                pr_number=1,
                pr_title="t",
                pr_description="d",
                diff_summary="s",
                ground_truth=[
                    GroundTruthFinding(
                        label="true_positive",
                        category="correctness",
                        severity="high",
                        file_path="expected.py",
                        title="Bug",
                        description="A bug",
                    )
                ],
            )
        ]
        results = [
            EvalResult(
                entry_id="e1",
                model="test-model",
                matched_tp=[(0, "Bug", 1.0, "wrong.py")],
            )
        ]

        scores = score_model(results, corpus)

        assert scores.precision == 1.0
        assert scores.location_accuracy == 0.0

    def test_all_false_positives(self):
        corpus = [
            CorpusEntry(
                entry_id="e1",
                repo="r",
                pr_number=1,
                pr_title="t",
                pr_description="d",
                diff_summary="s",
                ground_truth=[],
            )
        ]
        results = [
            EvalResult(
                entry_id="e1",
                model="test-model",
                matched_tp=[],
                false_positives_produced=3,
            )
        ]
        scores = score_model(results, corpus)
        assert scores.precision == 0.0
        assert scores.fp_rate == 3.0

    def test_passes_gate(self):
        corpus = [
            CorpusEntry(
                entry_id="e1",
                repo="r",
                pr_number=1,
                pr_title="t",
                pr_description="d",
                diff_summary="s",
                ground_truth=[
                    GroundTruthFinding(
                        label="true_positive",
                        category="correctness",
                        severity="high",
                        file_path="f.py",
                        title="Bug",
                        description="A bug",
                    )
                ],
            )
        ]
        results = [
            EvalResult(
                entry_id="e1",
                model="test-model",
                matched_tp=[(0, "Bug", 1.0, "f.py")],
                false_positives_produced=0,
            )
        ]
        scores = score_model(results, corpus)
        assert scores.passes_gate()

    def test_fails_gate_low_precision(self):
        corpus = [
            CorpusEntry(
                entry_id="e1",
                repo="r",
                pr_number=1,
                pr_title="t",
                pr_description="d",
                diff_summary="s",
                ground_truth=[],
            )
        ]
        results = [
            EvalResult(
                entry_id="e1",
                model="test-model",
                matched_tp=[],
                false_positives_produced=5,
            )
        ]
        scores = score_model(results, corpus)
        assert not scores.passes_gate()

    def test_empty_predicted_file_path_raises(self):
        """score_model() must reject matched_tp entries with empty predicted_file_path.

        An empty path silently zeroes location_accuracy without warning.
        """
        import pytest

        corpus = [
            CorpusEntry(
                entry_id="e1",
                repo="r",
                pr_number=1,
                pr_title="t",
                pr_description="d",
                diff_summary="s",
                ground_truth=[
                    GroundTruthFinding(
                        label="true_positive",
                        category="correctness",
                        severity="high",
                        file_path="f.py",
                        title="Bug",
                        description="A bug",
                    )
                ],
            )
        ]
        results = [
            EvalResult(
                entry_id="e1",
                model="test-model",
                matched_tp=[(0, "Bug", 0.9, "")],  # empty predicted_file_path
            )
        ]
        with pytest.raises(ValueError, match="predicted_file_path"):
            score_model(results, corpus)


class TestLoadCorpus:
    def test_fixture_loads(self):
        corpus = load_corpus(FIXTURES / "pr_review_corpus.json")
        assert len(corpus) >= 6

    def test_fixture_has_valid_entries(self):
        corpus = load_corpus(FIXTURES / "pr_review_corpus.json")
        for entry in corpus:
            assert entry.repo
            assert entry.pr_number > 0
            assert entry.pr_title
            for gt in entry.ground_truth:
                assert gt.label in ("true_positive", "false_positive", "true_negative")
                assert gt.file_path
                assert gt.title

    def test_fixture_has_both_tp_and_fp(self):
        corpus = load_corpus(FIXTURES / "pr_review_corpus.json")
        has_tp = any(e.true_positives for e in corpus)
        has_fp = any(e.false_positives for e in corpus)
        assert has_tp, "corpus should have at least one true positive"
        assert has_fp, "corpus should have at least one false positive"

    def test_fixture_json_valid(self):
        raw = json.loads((FIXTURES / "pr_review_corpus.json").read_text())
        assert "corpus_version" in raw
        assert "entries" in raw
        assert raw["corpus_version"] == "v1"


# ── Phase 1: LocalReviewTarget, local_fingerprint, reviewer helpers ───────────


class TestLocalReviewTarget:
    def test_description_range(self):
        t = LocalReviewTarget(
            checkout="/repo",
            repo_name="org/repo",
            base_sha="aabbccdd",
            head_sha="11223344",
        )
        assert "aabbccdd" in t.description
        assert "11223344" in t.description
        assert "org/repo" in t.description

    def test_description_working_tree(self):
        t = LocalReviewTarget(
            checkout="/repo",
            repo_name="org/repo",
            base_sha="aabbccdd",
            head_sha=None,
            diff_fingerprint="fp16chars12345",
        )
        assert "working-tree" in t.description
        assert "fp16chars12345" in t.description

    def test_kind_default(self):
        t = LocalReviewTarget(checkout="/repo", base_sha="abc")
        assert t.kind == "local"

    def test_roundtrip_json(self):
        t = LocalReviewTarget(
            checkout="/repo",
            repo_name="org/repo",
            base_sha="abc",
            head_sha="def",
        )
        dumped = t.model_dump_json()
        loaded = LocalReviewTarget.model_validate_json(dumped)
        assert loaded.repo_name == "org/repo"
        assert loaded.head_sha == "def"


class TestLocalFingerprintVsHostedFingerprint:
    def test_local_fingerprint_stable(self):
        fp1 = ReviewFinding.local_fingerprint(
            repo="org/repo",
            head_sha="abc123",
            file_path="src/foo.py",
            title="Null deref",
            prompt_version="v1",
        )
        fp2 = ReviewFinding.local_fingerprint(
            repo="org/repo",
            head_sha="abc123",
            file_path="src/foo.py",
            title="Null deref",
            prompt_version="v1",
        )
        assert fp1 == fp2

    def test_local_fingerprint_differs_from_hosted(self):
        local_fp = ReviewFinding.local_fingerprint(
            repo="org/repo",
            head_sha="abc123",
            file_path="src/foo.py",
            title="T",
            prompt_version="v1",
        )
        hosted_fp = ReviewFinding.fingerprint(
            repo="org/repo",
            pr_number=42,
            head_sha="abc123",
            file_path="src/foo.py",
            title="T",
            prompt_version="v1",
        )
        assert local_fp != hosted_fp

    def test_local_fingerprint_length(self):
        fp = ReviewFinding.local_fingerprint(
            repo="r", head_sha="h", file_path="f", title="t", prompt_version="v1"
        )
        assert len(fp) == 16


class TestReviewArtifactDiscriminatedUnion:
    def test_hosted_target_roundtrip(self):
        a = ReviewArtifact(
            target=ReviewTarget(
                repo="org/repo",
                pr_number=7,
                base_sha="base",
                head_sha="head",
            ),
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="ok",
            merge_safety=MergeSafety.safe,
        )
        raw = a.model_dump_json()
        loaded = ReviewArtifact.model_validate_json(raw)
        assert isinstance(loaded.target, ReviewTarget)
        assert loaded.target.pr_number == 7

    def test_local_target_roundtrip(self):
        a = ReviewArtifact(
            target=LocalReviewTarget(
                checkout="/repo",
                repo_name="org/repo",
                base_sha="abc",
                head_sha=None,
                diff_fingerprint="fp1234567890abcd",
            ),
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="Local review",
            merge_safety=MergeSafety.needs_review,
        )
        raw = a.model_dump_json()
        loaded = ReviewArtifact.model_validate_json(raw)
        assert isinstance(loaded.target, LocalReviewTarget)
        assert loaded.target.diff_fingerprint == "fp1234567890abcd"

    def test_token_id_local(self):
        a = ReviewArtifact(
            target=LocalReviewTarget(
                checkout="/home/bob/bob",
                repo_name="org/repo",
                base_sha="aabbccdd",
                head_sha="11223344",
            ),
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="ok",
            merge_safety=MergeSafety.safe,
        )
        assert "org/repo@11223344" == a.token_id


class TestDiffFingerprint:
    def test_stable(self):
        assert _diff_fingerprint("abc\n") == _diff_fingerprint("abc\n")

    def test_differs_on_content(self):
        assert _diff_fingerprint("abc\n") != _diff_fingerprint("def\n")

    def test_length(self):
        assert len(_diff_fingerprint("x")) == 16


class TestExtractJson:
    def test_direct_json(self):
        raw = '{"summary": "ok", "merge_safety": "safe", "findings": []}'
        result = _extract_json(raw)
        assert result["summary"] == "ok"

    def test_json_in_prose(self):
        raw = 'Here is my review:\n{"summary": "ok", "merge_safety": "safe", "findings": []}\nDone.'
        result = _extract_json(raw)
        assert result["merge_safety"] == "safe"

    def test_json_in_code_fence(self):
        raw = '```json\n{"summary": "ok", "merge_safety": "safe", "findings": []}\n```'
        result = _extract_json(raw)
        assert result["findings"] == []

    def test_raises_on_no_json(self):
        import pytest

        with pytest.raises(ValueError, match="No valid JSON"):
            _extract_json("This is not JSON at all.")


class TestBuildPrompt:
    def test_contains_instructions(self):
        prompt = _build_prompt("diff content", "", "repo@abc")
        assert "merge_safety" in prompt
        assert "diff content" in prompt

    def test_contains_repo_instructions(self):
        prompt = _build_prompt("diff", "# AGENTS.md\n\nBe careful.", "repo@abc")
        assert "AGENTS.md" in prompt

    def test_truncates_large_diff(self):
        big_diff = "x" * 100_000
        prompt = _build_prompt(big_diff, "", "repo@abc")
        assert "TRUNCATED" in prompt
        assert len(prompt) < 120_000  # well within reason

    def test_no_truncation_small_diff(self):
        small_diff = "- old\n+ new\n"
        prompt = _build_prompt(small_diff, "", "repo@abc")
        assert "TRUNCATED" not in prompt


class TestReadRepoInstructions:
    def test_reads_agents_md(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Instructions\nBe careful.")
        result = read_repo_instructions(tmp_path)
        assert "Be careful." in result
        assert "AGENTS.md" in result

    def test_returns_empty_when_none(self, tmp_path):
        result = read_repo_instructions(tmp_path)
        assert result == ""

    def test_truncates_long_file(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("x" * 10_000)
        result = read_repo_instructions(tmp_path)
        assert "truncated" in result
        assert len(result) < 12_000


class TestResolveLocalTarget:
    def _git(self, path: Path, *args: str, **extra_env: str) -> None:
        env = {**__import__("os").environ, "ALLOW_GIT_IDENTITY": "1", **extra_env}
        subprocess.run(
            ["git", *args], cwd=path, check=True, capture_output=True, env=env
        )

    def _init_git_repo(self, path: Path) -> None:
        self._git(path, "init")
        self._git(path, "config", "user.email", "test@test.com")
        self._git(path, "config", "user.name", "Test")
        (path / "README.md").write_text("# Repo\n")
        self._git(path, "add", ".")
        self._git(path, "commit", "-m", "init", "--no-verify")

    def test_working_tree(self, tmp_path):
        self._init_git_repo(tmp_path)
        (tmp_path / "new_file.py").write_text("x = 1\n")
        self._git(tmp_path, "add", ".")

        target, diff = resolve_local_target(tmp_path, working_tree=True)
        assert target.kind == "local"
        assert target.head_sha is None
        assert target.diff_fingerprint != ""
        assert "new_file.py" in diff

    def test_range(self, tmp_path):
        self._init_git_repo(tmp_path)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        (tmp_path / "change.py").write_text("def foo(): pass\n")
        self._git(tmp_path, "add", ".")
        self._git(tmp_path, "commit", "-m", "add change", "--no-verify")
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        target, diff = resolve_local_target(
            tmp_path, base_sha=base_sha, head_sha=head_sha
        )
        assert target.kind == "local"
        assert target.head_sha == head_sha
        assert target.base_sha == base_sha
        assert "change.py" in diff

    def test_raises_without_args(self, tmp_path):
        self._init_git_repo(tmp_path)

        with pytest.raises(ValueError, match="Specify"):
            resolve_local_target(tmp_path)


class TestReviewCliExitStatus:
    @pytest.mark.parametrize(
        ("merge_safety", "expected_exit_code"),
        [
            (MergeSafety.safe, 0),
            (MergeSafety.unsafe, 1),
            (MergeSafety.needs_review, 1),
            (MergeSafety.unknown, 1),
        ],
    )
    def test_only_safe_verdict_exits_zero(
        self, tmp_path, merge_safety, expected_exit_code
    ):
        target = LocalReviewTarget(
            checkout=str(tmp_path),
            base_sha="a" * 40,
            diff_fingerprint="b" * 16,
        )
        artifact = ReviewArtifact(
            target=target,
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="review",
            merge_safety=merge_safety,
        )

        with (
            patch(
                "gptme_runloops.pr_review.reviewer.resolve_local_target",
                return_value=(target, "+new line"),
            ),
            patch(
                "gptme_runloops.pr_review.reviewer.run_review",
                return_value=artifact,
            ),
        ):
            result = CliRunner().invoke(
                cli_main,
                ["review", "--checkout", str(tmp_path), "--working-tree"],
            )

        assert result.exit_code == expected_exit_code

    @pytest.mark.parametrize(
        "response",
        [
            "not JSON",
            '{"merge_safety":"safe","findings":[{"confidence":"high"}]}',
            '{"merge_safety":"safe","findings":[42]}',
            '{"merge_safety":"safe","findings":{}}',
        ],
    )
    def test_invalid_response_is_controlled_cli_error(self, tmp_path, response):
        target = LocalReviewTarget(
            checkout=str(tmp_path),
            base_sha="a" * 40,
            diff_fingerprint="b" * 16,
        )
        with (
            patch(
                "gptme_runloops.pr_review.reviewer.resolve_local_target",
                return_value=(target, "+new line"),
            ),
            patch(
                "gptme_runloops.pr_review.reviewer._invoke_model",
                return_value=response,
            ),
        ):
            result = CliRunner().invoke(
                cli_main,
                ["review", "--checkout", str(tmp_path), "--working-tree"],
            )

        assert result.exit_code == 1
        assert "Error: Invalid review response:" in result.output
        assert not isinstance(result.exception, TypeError | ValueError)


# ── Phase 2: GitHub adapter ───────────────────────────────────────────────────


class TestGetPostedFingerprints:
    """get_posted_fingerprints() parses hidden fp tags from existing comments."""

    def test_extracts_fingerprint_from_body(self):
        from gptme_runloops.pr_review.github_adapter import (
            _FP_PREFIX,
            _FP_SUFFIX,
            get_posted_fingerprints,
        )

        comment_body = (
            f"Some text.\n{_FP_PREFIX}abcd1234ef567890{_FP_SUFFIX}\nMore text."
        )
        mock_response = [{"body": comment_body}]

        with patch(
            "gptme_runloops.pr_review.github_adapter._gh_api",
            return_value=mock_response,
        ):
            fps = get_posted_fingerprints("org/repo", 42)

        assert "abcd1234ef567890" in fps

    def test_returns_empty_on_no_comments(self):
        from gptme_runloops.pr_review.github_adapter import get_posted_fingerprints

        with patch("gptme_runloops.pr_review.github_adapter._gh_api", return_value=[]):
            fps = get_posted_fingerprints("org/repo", 42)

        assert fps == set()

    def test_multiple_comments_multiple_fingerprints(self):
        from gptme_runloops.pr_review.github_adapter import (
            _FP_PREFIX,
            _FP_SUFFIX,
            get_posted_fingerprints,
        )

        mock_response = [
            {"body": f"Finding A\n{_FP_PREFIX}aaaa111122223333{_FP_SUFFIX}"},
            {"body": f"Finding B\n{_FP_PREFIX}bbbb444455556666{_FP_SUFFIX}"},
            {"body": "No fingerprint here"},
        ]
        with patch(
            "gptme_runloops.pr_review.github_adapter._gh_api",
            return_value=mock_response,
        ):
            fps = get_posted_fingerprints("org/repo", 42)

        assert fps == {"aaaa111122223333", "bbbb444455556666"}

    def test_uses_paginate_to_scan_all_pages(self):
        """get_posted_fingerprints must request all pages, not just the first."""
        from gptme_runloops.pr_review.github_adapter import get_posted_fingerprints

        with patch(
            "gptme_runloops.pr_review.github_adapter._gh_api",
            return_value=[],
        ) as mock_api:
            get_posted_fingerprints("org/repo", 42)

        _, call_kwargs = mock_api.call_args
        assert call_kwargs.get("paginate") is True


class TestSummaryComment:
    def test_dropped_findings_are_excluded_from_count_and_severity(self):
        from gptme_runloops.pr_review.github_adapter import _build_summary_comment_body

        kept = ReviewFinding(
            id="kept",
            category="correctness",
            severity=Severity.high,
            confidence=0.9,
            file_path="src/foo.py",
            line_range="42",
            title="Real bug",
            description="A bug.",
            evidence="code here",
            disposition=Disposition.confirmed,
        )
        dropped = ReviewFinding(
            id="dropped",
            category="style",
            severity=Severity.medium,
            confidence=0.9,
            file_path="src/foo.py",
            line_range="43",
            title="Suppressed nit",
            description="A nit.",
            evidence="code here",
            disposition=Disposition.dropped,
        )
        artifact = ReviewArtifact(
            target=ReviewTarget(
                repo="org/repo",
                pr_number=42,
                base_sha="base" * 10,
                head_sha="head" * 10,
            ),
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="ok",
            merge_safety=MergeSafety.needs_review,
            findings=[kept, dropped],
        )

        body = _build_summary_comment_body(artifact, posted_count=1, skipped_count=1)

        assert "**1 finding(s)**: 1 high" in body
        assert "medium" not in body

    def test_low_confidence_findings_are_excluded(self):
        from gptme_runloops.pr_review.github_adapter import _build_summary_comment_body

        kept = ReviewFinding(
            id="kept",
            category="correctness",
            severity=Severity.high,
            confidence=0.9,
            file_path="src/foo.py",
            line_range="42",
            title="Real bug",
            description="A bug.",
            evidence="code here",
        )
        low_confidence = ReviewFinding(
            id="low-confidence",
            category="correctness",
            severity=Severity.medium,
            confidence=0.5,
            file_path="src/foo.py",
            line_range="43",
            title="Uncertain bug",
            description="Maybe a bug.",
            evidence="code here",
        )
        artifact = ReviewArtifact(
            target=ReviewTarget(
                repo="org/repo",
                pr_number=42,
                base_sha="base" * 10,
                head_sha="head" * 10,
            ),
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="ok",
            merge_safety=MergeSafety.needs_review,
            findings=[kept, low_confidence],
        )

        body = _build_summary_comment_body(
            artifact, posted_count=1, skipped_count=1, min_confidence=0.6
        )

        assert "**1 finding(s)**: 1 high" in body
        assert "medium" not in body


class TestPublishArtifactShadowMode:
    """publish_artifact() in shadow mode must not call any GitHub API."""

    def _make_artifact(self, findings=None):
        return ReviewArtifact(
            target=ReviewTarget(
                repo="org/repo",
                pr_number=42,
                base_sha="base" * 10,
                head_sha="head" * 10,
            ),
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="ok",
            merge_safety=MergeSafety.needs_review,
            findings=findings or [],
        )

    def _make_finding(self, fp_id: str, confidence: float = 0.9) -> ReviewFinding:
        return ReviewFinding(
            id=fp_id,
            category="correctness",
            severity=Severity.high,
            confidence=confidence,
            file_path="src/foo.py",
            line_range="42",
            title=f"Bug {fp_id}",
            description="A bug.",
            evidence="code here",
        )

    def test_shadow_no_api_calls(self):
        from gptme_runloops.pr_review.github_adapter import publish_artifact

        artifact = self._make_artifact([self._make_finding("fp1")])
        with patch("gptme_runloops.pr_review.github_adapter._gh_api") as mock_api:
            posted, skipped = publish_artifact(
                artifact, repo="org/repo", pr_number=42, shadow=True
            )
            mock_api.assert_not_called()

        assert posted == 1
        assert skipped == 0

    def test_shadow_drops_low_confidence(self):
        from gptme_runloops.pr_review.github_adapter import publish_artifact

        f_high = self._make_finding("fp-high", confidence=0.9)
        f_low = self._make_finding("fp-low", confidence=0.3)
        artifact = self._make_artifact([f_high, f_low])

        with patch("gptme_runloops.pr_review.github_adapter._gh_api"):
            posted, skipped = publish_artifact(
                artifact, repo="org/repo", pr_number=42, shadow=True, min_confidence=0.6
            )

        assert posted == 1
        assert skipped == 1


class TestPublishArtifactIdempotency:
    """publish_artifact() skips findings already posted (idempotency guard)."""

    def _make_artifact(self, fp_id: str) -> ReviewArtifact:
        finding = ReviewFinding(
            id=fp_id,
            category="correctness",
            severity=Severity.medium,
            confidence=0.8,
            file_path="src/foo.py",
            line_range="10",
            title="Duplicate finding",
            description="Already posted.",
            evidence="...",
        )
        return ReviewArtifact(
            target=ReviewTarget(
                repo="org/repo",
                pr_number=99,
                base_sha="b" * 40,
                head_sha="h" * 40,
            ),
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="ok",
            merge_safety=MergeSafety.safe,
            findings=[finding],
        )

    def test_already_posted_fingerprint_is_skipped(self):
        from gptme_runloops.pr_review.github_adapter import publish_artifact

        artifact = self._make_artifact("alreadypostedfp")

        with (
            patch(
                "gptme_runloops.pr_review.github_adapter.get_posted_fingerprints",
                return_value={"alreadypostedfp"},
            ),
            patch(
                "gptme_runloops.pr_review.github_adapter.post_inline_finding"
            ) as mock_post,
        ):
            posted, skipped = publish_artifact(
                artifact, repo="org/repo", pr_number=99, shadow=False
            )
            mock_post.assert_not_called()

        assert posted == 0
        assert skipped == 1

    def test_new_fingerprint_is_posted(self):
        from gptme_runloops.pr_review.github_adapter import publish_artifact

        artifact = self._make_artifact("newfp1234567890a")

        with (
            patch(
                "gptme_runloops.pr_review.github_adapter.get_posted_fingerprints",
                return_value=set(),
            ),
            patch(
                "gptme_runloops.pr_review.github_adapter.post_inline_finding",
                return_value="comment-id-1",
            ) as mock_post,
            patch(
                "gptme_runloops.pr_review.github_adapter.post_summary_comment",
                return_value="s1",
            ),
        ):
            posted, skipped = publish_artifact(
                artifact, repo="org/repo", pr_number=99, shadow=False
            )
            mock_post.assert_called_once()

        assert posted == 1
        assert skipped == 0


class TestPostInlineFinding:
    """post_inline_finding() preserves the finding's declared diff side."""

    def _make_finding(self, *, line_side: str = "RIGHT") -> ReviewFinding:
        return ReviewFinding(
            id="fp1234567890abcd",
            category="correctness",
            severity=Severity.high,
            confidence=0.9,
            file_path="src/foo.py",
            line_range="10",
            line_side=line_side,
            title="Bug",
            description="A bug.",
            evidence="- old line",
        )

    @pytest.mark.parametrize("line_side", ["RIGHT", "LEFT"])
    def test_posts_on_declared_side(self, line_side: str):
        from gptme_runloops.pr_review.github_adapter import post_inline_finding

        with patch(
            "gptme_runloops.pr_review.github_adapter._gh_api",
            return_value={"id": 999},
        ) as mock_api:
            comment_id = post_inline_finding(
                "org/repo",
                42,
                "h" * 40,
                self._make_finding(line_side=line_side),
            )

        assert comment_id == "999"
        fields = mock_api.call_args[1]["fields"]
        assert fields["side"] == line_side

    def test_does_not_fall_back_to_opposite_side(self):
        """A rejected anchor must not attach to the same number on another side."""
        from gptme_runloops.pr_review.github_adapter import post_inline_finding

        with patch(
            "gptme_runloops.pr_review.github_adapter._gh_api",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ) as mock_api:
            with pytest.raises(subprocess.CalledProcessError):
                post_inline_finding(
                    "org/repo",
                    42,
                    "h" * 40,
                    self._make_finding(line_side="LEFT"),
                )

        mock_api.assert_called_once()
        assert mock_api.call_args.kwargs["fields"]["side"] == "LEFT"

    def test_iterates_range_when_first_line_rejected(self):
        """Falls back to later lines in the range when the first is not a postable anchor."""
        from gptme_runloops.pr_review.github_adapter import post_inline_finding

        finding = ReviewFinding(
            id="fp1234567890abcd",
            category="correctness",
            severity=Severity.high,
            confidence=0.9,
            file_path="src/foo.py",
            line_range="10-12",  # multi-line range; line 10 is outside the diff window
            title="Bug",
            description="A bug.",
            evidence="+ new line",
        )

        call_lines: list[int] = []

        def _api_side_effect(path, *, method="GET", fields=None, **kw):
            line = fields["line"] if fields else None
            call_lines.append(line)
            if line == 10:  # first line not in diff window
                raise subprocess.CalledProcessError(1, "gh")
            return {"id": 888}

        with patch(
            "gptme_runloops.pr_review.github_adapter._gh_api",
            side_effect=_api_side_effect,
        ):
            comment_id = post_inline_finding("org/repo", 42, "h" * 40, finding)

        assert comment_id == "888"
        # line 10 failed; line 11 succeeded, preserving RIGHT throughout.
        assert call_lines == [10, 11]

    def test_raises_when_all_range_lines_are_rejected(self):
        """If every same-side anchor fails, CalledProcessError propagates."""
        from gptme_runloops.pr_review.github_adapter import post_inline_finding

        with patch(
            "gptme_runloops.pr_review.github_adapter._gh_api",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                post_inline_finding("org/repo", 42, "h" * 40, self._make_finding())


class TestReviewPrCliCommand:
    """CLI integration tests for the review-pr command."""

    def test_invalid_pr_ref_format(self):
        result = CliRunner().invoke(cli_main, ["review-pr", "not-a-valid-ref"])
        assert result.exit_code != 0
        assert "Invalid PR reference" in result.output

    def test_invalid_pr_ref_missing_hash(self):
        result = CliRunner().invoke(cli_main, ["review-pr", "org/repo"])
        assert result.exit_code != 0

    def test_shadow_mode_calls_run_github_review(self):
        target = ReviewTarget(
            repo="org/repo",
            pr_number=42,
            base_sha="b" * 40,
            head_sha="h" * 40,
        )
        artifact = ReviewArtifact(
            target=target,
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="Looks good.",
            merge_safety=MergeSafety.safe,
        )

        with patch(
            "gptme_runloops.pr_review.github_adapter.run_github_review",
            return_value=(artifact, 0, 0),
        ):
            result = CliRunner().invoke(cli_main, ["review-pr", "org/repo#42"])

        assert result.exit_code == 0
        # Extract JSON object from output (stderr progress lines may be mixed in)
        m = re.search(r"\{.*\}", result.output, re.DOTALL)
        assert m is not None, f"No JSON in output: {result.output!r}"
        data = json.loads(m.group(0))
        assert data["summary"] == "Looks good."

    def test_unsafe_verdict_exits_nonzero(self):
        target = ReviewTarget(
            repo="org/repo",
            pr_number=1,
            base_sha="b" * 40,
            head_sha="h" * 40,
        )
        artifact = ReviewArtifact(
            target=target,
            model="test",
            prompt_version="v1",
            started_at=datetime.now(tz=timezone.utc),
            completed_at=datetime.now(tz=timezone.utc),
            summary="Bugs found.",
            merge_safety=MergeSafety.unsafe,
        )

        with patch(
            "gptme_runloops.pr_review.github_adapter.run_github_review",
            return_value=(artifact, 0, 0),
        ):
            result = CliRunner().invoke(cli_main, ["review-pr", "org/repo#1"])

        assert result.exit_code == 1
