"""Tests for the pr_review schema and corpus module (Phase 0)."""

import json
from datetime import datetime, timezone
from pathlib import Path

from gptme_runloops.pr_review import (
    Disposition,
    MergeSafety,
    ReviewArtifact,
    ReviewFinding,
    ReviewTarget,
    Severity,
)
from gptme_runloops.pr_review.corpus import (
    CorpusEntry,
    EvalResult,
    GroundTruthFinding,
    load_corpus,
    score_model,
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
                matched_tp=[(0, "Bug", 1.0)],
                false_positives_produced=0,
            )
        ]
        scores = score_model(results, corpus)
        assert scores.precision == 1.0
        assert scores.recall == 1.0
        assert scores.fp_rate == 0.0

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
                matched_tp=[(0, "Bug", 1.0)],
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
