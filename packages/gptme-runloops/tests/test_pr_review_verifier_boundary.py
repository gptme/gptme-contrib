"""Input-boundary regression tests for the two-stage review prompts.

The PR diff and the Stage 1 finding both derive from untrusted PR content.
gptme processes a programmatically built prompt with ``include_paths()`` before
the model call, so an ``@/abs/path`` reference or a URL in the diff would be
read from the reviewing host or fetched unless the payload is inert text.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from gptme_runloops.pr_review.reviewer import _invoke_model
from gptme_runloops.pr_review.schema import ReviewFinding, Severity
from gptme_runloops.pr_review.verifier import (
    _build_verifier_prompt,
    _invoke_verifier_model,
)

MARKER = "SYNTHETIC_ONLY_BOUNDARY_MARKER"


def _finding() -> ReviewFinding:
    return ReviewFinding(
        id="synthetic",
        category="correctness",
        severity=Severity.medium,
        confidence=0.9,
        file_path="README.md",
        line_range="2",
        title="Synthetic",
        description="Synthetic",
        evidence="Synthetic",
    )


def _diff_with_path_and_url(tmp_path: Path) -> str:
    marker_file = tmp_path / "synthetic.txt"
    marker_file.write_text(MARKER)
    return (
        "diff --git a/README.md b/README.md\n"
        f"+see @{marker_file} for details\n"
        "+see https://example.invalid/synthetic-only\n"
    )


def test_verifier_prompt_fences_untrusted_payloads(tmp_path):
    prompt = _build_verifier_prompt(_finding(), _diff_with_path_and_url(tmp_path))
    # The diff must live inside a fenced block, not as bare prose.
    assert "```json\n" in prompt
    fenced_region = prompt.split("Full diff context (JSON string):", 1)[1]
    assert fenced_region.strip().startswith(
        "```json"
    ) and fenced_region.strip().endswith("```")


def test_verifier_prompt_is_inert_to_path_and_url_inclusion(tmp_path):
    gptme_util = pytest.importorskip(
        "gptme.util.context", reason="gptme is not a package dependency"
    )
    gptme_message = pytest.importorskip("gptme.message")

    prompt = _build_verifier_prompt(_finding(), _diff_with_path_and_url(tmp_path))
    message = gptme_message.Message("user", prompt)

    included = gptme_util.include_paths(message)
    assert MARKER not in included.content
    assert gptme_util.extract_urls(prompt) == []


def test_verifier_model_disables_path_include(tmp_path):
    with patch("gptme_runloops.pr_review.verifier.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps(
            {
                "real": True,
                "worth_fixing": True,
                "severity": "medium",
                "rationale": "controls",
            }
        )
        _invoke_verifier_model("prompt", None, tmp_path)
    assert run.call_args.kwargs["env"]["GPTME_DISABLE_PATH_INCLUDE"] == "1"


def test_stage_one_model_disables_path_include(tmp_path):
    with patch("gptme_runloops.pr_review.reviewer.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = '{"findings": []}'
        _invoke_model("prompt", None, tmp_path)
    assert run.call_args.kwargs["env"]["GPTME_DISABLE_PATH_INCLUDE"] == "1"
