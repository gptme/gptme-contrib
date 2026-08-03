"""Golden corpus for PR reviewer evaluation.

Corpus format: a list of CorpusEntry objects, each representing a historical PR
with hand-labeled ground-truth findings. Used in Phase 0 to evaluate candidate
models before selecting a default.

Scoring metrics (from the MVP spec):
  - precision: fraction of model findings that are true positives
  - recall: fraction of known true bugs that the model found
  - fp_rate: false-positive findings per review
  - location_accuracy: finding points to the correct file (soft: same file)
  - duplicate_rate: how often the same finding appears twice
  - latency: time to first finding (seconds)
  - cost: estimated token cost in USD
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class GroundTruthFinding:
    """A human-labeled finding used as the evaluation standard."""

    label: Literal["true_positive", "false_positive", "true_negative"]
    category: str
    severity: str
    file_path: str
    title: str
    description: str
    # How the finding was verified
    verification: str = ""


@dataclass
class CorpusEntry:
    """One PR with hand-labeled ground truth for evaluation."""

    entry_id: str
    repo: str
    pr_number: int
    pr_title: str
    pr_description: str
    # Unified diff (or a summary for large PRs)
    diff_summary: str
    # Reviewer's ground truth
    ground_truth: list[GroundTruthFinding] = field(default_factory=list)
    # Source of truth (greptile, human-review, post-merge incident)
    attribution: str = ""
    notes: str = ""

    @property
    def true_positives(self) -> list[GroundTruthFinding]:
        return [f for f in self.ground_truth if f.label == "true_positive"]

    @property
    def false_positives(self) -> list[GroundTruthFinding]:
        return [f for f in self.ground_truth if f.label == "false_positive"]


@dataclass
class EvalResult:
    """Evaluation result for one model run against one corpus entry."""

    entry_id: str
    model: str
    # Matched findings:
    # (ground_truth_idx, model_finding_title, match_score 0-1, predicted_file_path)
    matched_tp: list[tuple[int, str, float, str]] = field(default_factory=list)
    false_positives_produced: int = 0
    duplicate_findings: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0

    @property
    def precision(self) -> float:
        total = len(self.matched_tp) + self.false_positives_produced
        return len(self.matched_tp) / total if total > 0 else 1.0

    @property
    def recall(self) -> float:
        # Caller must supply total known TPs for this entry
        return 0.0  # overridden in score_model()


@dataclass
class ModelScores:
    """Aggregate evaluation scores across the full corpus."""

    model: str
    precision: float
    recall: float
    fp_rate: float
    location_accuracy: float
    duplicate_rate: float
    avg_latency_s: float
    avg_cost_usd: float
    n_entries: int

    def summary(self) -> str:
        return (
            f"{self.model}: precision={self.precision:.2f} recall={self.recall:.2f} "
            f"fp_rate={self.fp_rate:.2f} loc_acc={self.location_accuracy:.2f} "
            f"dup_rate={self.duplicate_rate:.2f} "
            f"latency={self.avg_latency_s:.1f}s cost=${self.avg_cost_usd:.4f}/review "
            f"(n={self.n_entries})"
        )

    def passes_gate(
        self,
        min_precision: float = 0.80,
        max_fp_rate: float = 2.0,
    ) -> bool:
        """Whether this model meets the Phase 0 gate for pilot use."""
        return self.precision >= min_precision and self.fp_rate <= max_fp_rate


def load_corpus(path: Path | None = None) -> list[CorpusEntry]:
    """Load the golden corpus from the default fixtures file or a custom path."""
    if path is None:
        path = (
            Path(__file__).parent.parent.parent.parent
            / "tests"
            / "fixtures"
            / "pr_review_corpus.json"
        )
    with open(path) as f:
        raw = json.load(f)
    entries = []
    for item in raw["entries"]:
        findings = [GroundTruthFinding(**g) for g in item.pop("ground_truth", [])]
        entries.append(CorpusEntry(**item, ground_truth=findings))
    return entries


def score_model(
    model_results: list[EvalResult],
    corpus: list[CorpusEntry],
) -> ModelScores:
    """Aggregate per-entry EvalResults into a ModelScores summary.

    Call this after running the model against every corpus entry.
    """
    total_tp = sum(len(e.true_positives) for e in corpus)
    corpus_by_id = {entry.entry_id: entry for entry in corpus}
    matched_tp_total = 0
    fp_total = 0
    dup_total = 0
    location_hits = 0
    location_checked = 0

    for result in model_results:
        entry = corpus_by_id.get(result.entry_id)
        if entry is None:
            raise ValueError(f"No corpus entry for EvalResult {result.entry_id!r}")

        matched_tp_total += len(result.matched_tp)
        fp_total += result.false_positives_produced
        dup_total += result.duplicate_findings

        for gt_idx, _title, _match_score, predicted_file_path in result.matched_tp:
            try:
                expected_file_path = entry.true_positives[gt_idx].file_path
            except IndexError as exc:
                raise ValueError(
                    f"Invalid ground-truth index {gt_idx} for {result.entry_id!r}"
                ) from exc
            location_checked += 1
            if predicted_file_path == expected_file_path:
                location_hits += 1

    n = len(model_results)
    precision = (
        matched_tp_total / (matched_tp_total + fp_total)
        if (matched_tp_total + fp_total) > 0
        else 1.0
    )
    recall = matched_tp_total / total_tp if total_tp > 0 else 1.0
    fp_rate = fp_total / n if n > 0 else 0.0
    dup_rate = dup_total / n if n > 0 else 0.0
    loc_acc = location_hits / location_checked if location_checked > 0 else 1.0
    avg_latency = sum(r.latency_s for r in model_results) / n if n > 0 else 0.0
    avg_cost = sum(r.cost_usd for r in model_results) / n if n > 0 else 0.0

    return ModelScores(
        model=model_results[0].model if model_results else "unknown",
        precision=precision,
        recall=recall,
        fp_rate=fp_rate,
        location_accuracy=loc_acc,
        duplicate_rate=dup_rate,
        avg_latency_s=avg_latency,
        avg_cost_usd=avg_cost,
        n_entries=n,
    )
