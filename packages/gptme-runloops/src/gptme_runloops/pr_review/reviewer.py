"""Core review runner for Phase 1 — local CLI, artifact only.

Supports three input modes (from the MVP contract):
  working-tree:  git diff HEAD — in-session, before a PR exists
  range:         git diff BASE_SHA..HEAD_SHA — local commit range, no forge
  hosted-pr:     thin adapter calls resolve_local_target after fetching diff

No forge API calls from this module. The GitHub/Forgejo adapters are callers,
not dependencies of the review logic.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .schema import (
    Disposition,
    LocalReviewTarget,
    MergeSafety,
    ReviewArtifact,
    ReviewFinding,
    Severity,
)

REVIEW_PROMPT_VERSION = "v1"

# Hard cap to stay within model context limits (~20k tokens for the diff).
# Larger diffs get a truncation note; future phases can chunk deterministically.
_MAX_DIFF_BYTES = 80_000

_REVIEW_INSTRUCTIONS = """\
You are a careful code reviewer. Analyze the provided diff and produce findings.

Rules:
- Only report findings with concrete evidence visible in the diff.
- Every finding MUST reference a specific file and line from the changed code.
- Focus on correctness first, then security, then missing test coverage.
- Skip purely stylistic findings unless they indicate a real bug risk.
- Be conservative: a missed real bug is worse than a skipped marginal finding.
- Do not invent findings about code not in the diff.

Return a single JSON object with this exact structure (no preamble, no fences):
{
  "summary": "One to two sentence summary of the changes and overall quality.",
  "merge_safety": "safe" | "unsafe" | "needs_review",
  "findings": [
    {
      "category": "correctness" | "security" | "test-coverage" | "performance" | "style",
      "severity": "critical" | "high" | "medium" | "low" | "info",
      "confidence": <float 0.0-1.0>,
      "file_path": "<exact path from diff>",
      "line_range": "<line or range, e.g. 42 or 42-49>",
      "title": "<short specific title>",
      "description": "<full description of the problem>",
      "evidence": "<exact code snippet from the diff showing the issue>",
      "fix_hint": "<optional fix suggestion>"
    }
  ]
}""".strip()


def get_diff(
    checkout: Path,
    *,
    working_tree: bool = False,
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> tuple[str, str, str | None]:
    """Get unified diff from a local checkout.

    Returns:
        (diff_text, resolved_base_sha, resolved_head_sha)
        resolved_head_sha is None for working-tree reviews.

    Raises:
        ValueError: if neither working_tree nor both base_sha/head_sha are given.
        subprocess.CalledProcessError: if git commands fail.
    """
    if working_tree:
        base = _git(checkout, "rev-parse", "HEAD").strip()
        # Include both staged and unstaged changes relative to HEAD
        unstaged = _git(checkout, "diff", "HEAD")
        staged = _git(checkout, "diff", "--cached", "HEAD")
        # Merge: staged usually overlaps with unstaged (diff HEAD covers both)
        diff = unstaged if unstaged else staged
        return diff, base, None
    elif base_sha and head_sha:
        diff = _git(checkout, "diff", f"{base_sha}..{head_sha}")
        return diff, base_sha, head_sha
    else:
        raise ValueError(
            "Specify --working-tree or both --base <SHA> and --head <SHA>."
        )


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def read_repo_instructions(checkout: Path) -> str:
    """Read AGENTS.md, CLAUDE.md, or similar repo instruction files.

    Returns the content of the first file found, truncated to 8 KB.
    Returns an empty string if no instruction file exists.
    """
    candidates = ["AGENTS.md", "CLAUDE.md", ".github/CONTRIBUTING.md"]
    for name in candidates:
        path = checkout / name
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace")
            if len(content) > 8_000:
                content = content[:8_000] + "\n... [truncated for context size]"
            return f"# Repository instructions ({name})\n\n{content}"
    return ""


def _diff_fingerprint(diff: str) -> str:
    """Deterministic 16-char fingerprint of a working-tree diff."""
    return hashlib.sha256(diff.encode()).hexdigest()[:16]


def _detect_repo_name(checkout: Path) -> str:
    """Infer repo name from git remote or directory name."""
    try:
        remote = _git(checkout, "remote", "get-url", "origin").strip()
        # github.com/org/repo or git@github.com:org/repo.git → org/repo
        m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
        if m:
            return m.group(1)
    except subprocess.CalledProcessError:
        pass
    return checkout.name


def resolve_local_target(
    checkout: Path,
    *,
    working_tree: bool = False,
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> tuple[LocalReviewTarget, str]:
    """Resolve CLI args to a (LocalReviewTarget, diff_text) pair.

    This is the entry point from the CLI. The diff_text is returned separately
    so callers can inspect size, truncate, or log it before passing to run_review.

    Args:
        checkout: Path to the git repository root.
        working_tree: If True, review the current working-tree diff (HEAD).
        base_sha: Base commit SHA for a range review.
        head_sha: Head commit SHA for a range review.

    Returns:
        (target, diff_text) — target is a validated LocalReviewTarget,
        diff_text is the raw unified diff.
    """
    diff, resolved_base, resolved_head = get_diff(
        checkout,
        working_tree=working_tree,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    fingerprint = _diff_fingerprint(diff) if resolved_head is None else ""
    repo_name = _detect_repo_name(checkout)
    target = LocalReviewTarget(
        checkout=str(checkout.resolve()),
        repo_name=repo_name,
        base_sha=resolved_base,
        head_sha=resolved_head,
        diff_fingerprint=fingerprint,
    )
    return target, diff


def _build_prompt(diff: str, repo_instructions: str, target_desc: str) -> str:
    """Build the review prompt, truncating large diffs with a clear note."""
    diff_bytes = diff.encode()
    truncation_note = ""
    if len(diff_bytes) > _MAX_DIFF_BYTES:
        diff = diff_bytes[:_MAX_DIFF_BYTES].decode(errors="replace")
        truncation_note = (
            f"\n\n[DIFF TRUNCATED at {_MAX_DIFF_BYTES} bytes — "
            "report findings only for code visible above]"
        )

    parts: list[str] = [_REVIEW_INSTRUCTIONS]
    if repo_instructions:
        parts.append(f"\n\n---\n\n{repo_instructions}")
    parts.append(f"\n\n---\n\nReview target: {target_desc}")
    parts.append(f"\n\n```diff\n{diff}\n```{truncation_note}")
    return "".join(parts)


def _extract_json(text: str) -> dict:
    """Extract the first/only JSON object from model output.

    The model is prompted to return only JSON, but may add preamble
    or wrap in a code fence. This extracts robustly.
    """
    text = text.strip()
    # Direct parse first (clean output)
    try:
        result = json.loads(text)
        assert isinstance(result, dict)
        return result
    except (json.JSONDecodeError, AssertionError):
        pass
    # Strip code fences (```json ... ```)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            result = json.loads(fenced.group(1))
            assert isinstance(result, dict)
            return result
        except (json.JSONDecodeError, AssertionError):
            pass
    # Find outermost JSON object in the text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(0))
            assert isinstance(result, dict)
            return result
        except (json.JSONDecodeError, AssertionError):
            pass
    raise ValueError(
        f"No valid JSON object found in model output (first 500 chars):\n{text[:500]}"
    )


def _invoke_model(prompt: str, model: str | None, checkout: Path) -> str:
    """Invoke gptme in one-shot mode with no tools and return stdout.

    Uses --tools none to get a clean text response (no tool calls interspersed).
    """
    cmd = ["gptme", "--tools", "none"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    result = subprocess.run(
        cmd,
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=300,
    )
    # gptme exits 0 on success; non-zero indicates an error (not just "no findings")
    if result.returncode not in (0, 1):
        stderr_snip = result.stderr[:1_000] if result.stderr else "(no stderr)"
        raise RuntimeError(
            f"gptme exited with code {result.returncode}:\n{stderr_snip}"
        )
    return result.stdout


def run_review(
    checkout: Path,
    target: LocalReviewTarget,
    diff: str,
    *,
    model: str | None = None,
    output_path: Path | None = None,
) -> ReviewArtifact:
    """Run a local review and return the validated ReviewArtifact.

    Args:
        checkout: Path to the repository root (used for repo instructions and gptme cwd).
        target: Resolved LocalReviewTarget (from resolve_local_target).
        diff: Unified diff text to review.
        model: gptme model spec (e.g. "anthropic/claude-sonnet-4-6"). If None,
               gptme uses its configured default.
        output_path: If set, write the artifact JSON here before returning.

    Returns:
        Validated ReviewArtifact. Findings start in needs_validation disposition;
        the Phase 2 publication path confirms or drops them before posting.
    """
    started_at = datetime.now(timezone.utc)

    repo_instructions = read_repo_instructions(checkout)
    prompt = _build_prompt(diff, repo_instructions, target.description)
    raw_output = _invoke_model(prompt, model, checkout)

    completed_at = datetime.now(timezone.utc)

    data = _extract_json(raw_output)

    # Resolve identity anchor for fingerprinting
    head_anchor = target.head_sha or target.diff_fingerprint or ""
    repo_id = target.repo_name or target.checkout

    findings: list[ReviewFinding] = []
    for raw_f in data.get("findings", []):
        fp = ReviewFinding.local_fingerprint(
            repo=repo_id,
            head_sha=head_anchor,
            file_path=raw_f.get("file_path", ""),
            title=raw_f.get("title", ""),
            prompt_version=REVIEW_PROMPT_VERSION,
        )
        try:
            severity = Severity(raw_f.get("severity", "medium"))
        except ValueError:
            severity = Severity.medium

        finding = ReviewFinding(
            id=fp,
            category=raw_f.get("category", "correctness"),
            severity=severity,
            confidence=float(raw_f.get("confidence", 0.5)),
            file_path=raw_f.get("file_path", ""),
            line_range=str(raw_f.get("line_range", "1")),
            title=raw_f.get("title", ""),
            description=raw_f.get("description", ""),
            evidence=raw_f.get("evidence", ""),
            fix_hint=raw_f.get("fix_hint", ""),
            disposition=Disposition.needs_validation,
        )
        findings.append(finding)

    try:
        merge_safety = MergeSafety(data.get("merge_safety", "unknown"))
    except ValueError:
        merge_safety = MergeSafety.unknown

    artifact = ReviewArtifact(
        target=target,
        model=model or "default",
        prompt_version=REVIEW_PROMPT_VERSION,
        started_at=started_at,
        completed_at=completed_at,
        summary=data.get("summary", ""),
        merge_safety=merge_safety,
        findings=findings,
        dry_run=True,
    )

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")

    return artifact
