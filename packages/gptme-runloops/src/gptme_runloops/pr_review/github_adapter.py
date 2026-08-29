"""GitHub adapter for the PR review system (Phase 2).

Responsibilities:
- Fetch PR metadata (base/head SHA) via ``gh pr view``.
- Fetch PR diff via ``gh pr diff``.
- Map to a ReviewTarget (hosted PR, forge=github).
- Post review findings as inline PR comments with idempotency guard.
- Shadow mode: run without publishing (default; safe to run on any PR).

Design constraint: the review *core* (reviewer.py) is forge-neutral.
This adapter is the ONLY module that may call ``gh`` CLI or the GitHub REST API.

Idempotency:
- Each finding's fingerprint is embedded as a hidden HTML comment in the body.
- Before posting, existing PR comments are scanned for known fingerprints.
- Findings already posted are skipped, making re-runs safe.

Shadow mode (default):
- The adapter fetches the diff and runs the review but does not post comments.
- Output: a ReviewArtifact JSON file (via --output) or stdout.
- Enable publication with --publish.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .schema import (
    Disposition,
    ReviewArtifact,
    ReviewFinding,
    ReviewTarget,
    Severity,
)

# Minimum confidence threshold — findings below this are skipped.
_MIN_CONFIDENCE = 0.6

# Fingerprint tag embedded as a hidden HTML comment.
_FP_PREFIX = "<!-- pr-review-fp:"
_FP_SUFFIX = " -->"


def _gh(*args: str, repo: str | None = None) -> str:
    """Run a ``gh`` CLI command and return stdout as text.

    Raises:
        subprocess.CalledProcessError: on non-zero exit.
    """
    cmd: list[str] = ["gh"]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(args)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def _gh_api(
    path: str,
    *,
    method: str = "GET",
    fields: dict[str, Any] | None = None,
    paginate: bool = False,
) -> Any:
    """Call the GitHub REST API via ``gh api`` and return parsed JSON.

    Args:
        path: API path, e.g. ``/repos/owner/repo/pulls/1/comments``.
        method: HTTP method (GET, POST, PATCH, …).
        fields: Fields to pass as ``-f key=value`` or ``-F key=value``.
        paginate: If True, follow GitHub pagination and return all results
                  concatenated into a single list (list endpoints only).
    """
    cmd = ["gh", "api", "--method", method]
    if paginate:
        cmd.append("--paginate")
    cmd.append(path)
    if fields:
        for k, v in fields.items():
            # Use -F for values that should stay as native types (int, bool)
            if isinstance(v, int | bool):
                cmd.extend(["-F", f"{k}={v}"])
            else:
                cmd.extend(["-f", f"{k}={v}"])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    if not paginate:
        return json.loads(result.stdout)
    # gh api --paginate prints each page as a separate JSON document (one per line).
    # For list endpoints each page is a JSON array; concatenate them all.
    all_items: list[Any] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        page = json.loads(line)
        if isinstance(page, list):
            all_items.extend(page)
        else:
            all_items.append(page)
    return all_items


def fetch_pr_metadata(repo: str, pr_number: int) -> dict[str, str]:
    """Fetch PR base/head SHAs from GitHub.

    Args:
        repo: ``OWNER/REPO`` string.
        pr_number: Pull request number.

    Returns:
        Dict with keys ``base_sha`` and ``head_sha``.
    """
    raw = _gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "baseRefOid,headRefOid",
        repo=repo,
    )
    data = json.loads(raw)
    return {
        "base_sha": data["baseRefOid"],
        "head_sha": data["headRefOid"],
    }


def fetch_pr_diff(repo: str, pr_number: int) -> str:
    """Fetch the unified diff for a pull request.

    Args:
        repo: ``OWNER/REPO`` string.
        pr_number: Pull request number.

    Returns:
        Unified diff text.
    """
    return _gh("pr", "diff", str(pr_number), repo=repo)


def build_review_target(
    repo: str, pr_number: int, base_sha: str, head_sha: str
) -> ReviewTarget:
    """Build a forge-neutral ReviewTarget for a GitHub PR."""
    return ReviewTarget(
        kind="hosted",
        forge="github",
        repo=repo,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def get_posted_fingerprints(repo: str, pr_number: int) -> set[str]:
    """Scan existing PR review comments for already-posted fingerprints.

    Uses the fingerprint embedded as a hidden HTML comment to identify
    findings that have already been published. This prevents duplicates
    on re-runs of the same head SHA.

    Returns:
        Set of fingerprint ID strings that are already posted.
    """
    owner, name = repo.split("/", 1)
    raw = _gh_api(f"/repos/{owner}/{name}/pulls/{pr_number}/comments", paginate=True)
    if not isinstance(raw, list):
        return set()

    found: set[str] = set()
    pattern = re.compile(re.escape(_FP_PREFIX) + r"(\w+)" + re.escape(_FP_SUFFIX))
    for comment in raw:
        body = comment.get("body", "")
        for m in pattern.finditer(body):
            found.add(m.group(1))
    return found


def _iter_range_lines(line_range: str):
    """Yield each line number in a range string like '42' or '42-49'."""
    parts = line_range.split("-")
    try:
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 else start
    except (ValueError, IndexError):
        start, end = 1, 1
    yield from range(start, end + 1)


def _severity_label(severity: Severity) -> str:
    labels = {
        Severity.critical: "🔴 CRITICAL",
        Severity.high: "🟠 HIGH",
        Severity.medium: "🟡 MEDIUM",
        Severity.low: "🔵 LOW",
        Severity.info: "ℹ️ INFO",
    }
    return labels.get(severity, severity.value.upper())


def _build_inline_comment_body(finding: ReviewFinding) -> str:
    """Format a finding as an inline PR comment with embedded fingerprint."""
    lines = [
        f"{_FP_PREFIX}{finding.id}{_FP_SUFFIX}",
        f"**{_severity_label(finding.severity)}**: {finding.title}",
        "",
        finding.description,
    ]
    if finding.evidence:
        lines += ["", "```", finding.evidence, "```"]
    if finding.fix_hint:
        lines += ["", f"**Suggested fix**: {finding.fix_hint}"]
    lines += ["", "*Automated finding — self-hosted PR reviewer (Bob)*"]
    return "\n".join(lines)


def _build_summary_comment_body(
    artifact: ReviewArtifact,
    posted_count: int,
    skipped_count: int,
    min_confidence: float = _MIN_CONFIDENCE,
) -> str:
    """Build a top-level summary comment for the PR."""
    postable_findings = [
        finding
        for finding in artifact.findings
        if finding.disposition != Disposition.dropped
        and finding.confidence >= min_confidence
    ]
    n_total = len(postable_findings)
    n_by_severity: dict[str, int] = {}
    for f in postable_findings:
        n_by_severity[f.severity.value] = n_by_severity.get(f.severity.value, 0) + 1

    severity_line = ", ".join(
        f"{count} {sev}"
        for sev, count in sorted(
            n_by_severity.items(),
            key=lambda x: (
                ["critical", "high", "medium", "low", "info"].index(x[0])
                if x[0] in ["critical", "high", "medium", "low", "info"]
                else 99
            ),
        )
    )

    lines = [
        f"## PR Review — {artifact.merge_safety.value.replace('_', ' ').title()}",
        "",
        artifact.summary,
        "",
    ]

    if n_total == 0:
        lines.append("No findings.")
    else:
        lines.append(f"**{n_total} finding(s)**: {severity_line}")
        if skipped_count > 0:
            lines.append(
                f"*{skipped_count} finding(s) skipped — already posted or below confidence threshold.*"
            )
        if posted_count > 0:
            lines.append(f"*{posted_count} finding(s) posted as inline comments.*")

    lines += [
        "",
        f"*Model: `{artifact.model}` · Prompt: `{artifact.prompt_version}`*",
        "*Self-hosted PR reviewer (Bob) — Phase 2*",
    ]
    return "\n".join(lines)


def post_inline_finding(
    repo: str,
    pr_number: int,
    head_sha: str,
    finding: ReviewFinding,
) -> str:
    """Post a single finding as an inline PR review comment.

    Args:
        repo: ``OWNER/REPO`` string.
        pr_number: Pull request number.
        head_sha: The PR's head commit SHA to bind the comment to.
        finding: The finding to post.

    Returns:
        The GitHub comment ID (as string).

    Raises:
        subprocess.CalledProcessError: if the API call fails.
    """
    owner, name = repo.split("/", 1)
    body = _build_inline_comment_body(finding)

    # Iterate every line in the reported range on the model-selected diff side.
    # Trying the opposite side is unsafe: the same numeric line can exist on both
    # sides while referring to unrelated code. Later lines in a range remain safe
    # fallback anchors because they preserve the finding's declared side.
    last_error: subprocess.CalledProcessError | None = None
    for line in _iter_range_lines(finding.line_range):
        try:
            data = _gh_api(
                f"/repos/{owner}/{name}/pulls/{pr_number}/comments",
                method="POST",
                fields={
                    "body": body,
                    "commit_id": head_sha,
                    "path": finding.file_path,
                    "line": line,
                    "side": finding.line_side,
                },
            )
            return str(data.get("id", ""))
        except subprocess.CalledProcessError as exc:
            last_error = exc
    raise last_error  # type: ignore[misc]  # unreachable when loop body always sets it


def post_summary_comment(
    repo: str,
    pr_number: int,
    artifact: ReviewArtifact,
    posted_count: int,
    skipped_count: int,
    min_confidence: float = _MIN_CONFIDENCE,
) -> str:
    """Post a top-level summary comment on the PR.

    Returns:
        The GitHub comment ID (as string).
    """
    owner, name = repo.split("/", 1)
    body = _build_summary_comment_body(
        artifact, posted_count, skipped_count, min_confidence
    )
    data = _gh_api(
        f"/repos/{owner}/{name}/issues/{pr_number}/comments",
        method="POST",
        fields={"body": body},
    )
    return str(data.get("id", ""))


def publish_artifact(
    artifact: ReviewArtifact,
    *,
    repo: str,
    pr_number: int,
    shadow: bool = True,
    min_confidence: float = _MIN_CONFIDENCE,
) -> tuple[int, int]:
    """Publish review findings to a GitHub PR (or shadow-run without posting).

    Args:
        artifact: Completed ReviewArtifact from run_review().
        repo: ``OWNER/REPO`` string.
        pr_number: Pull request number.
        shadow: If True (default), do not post anything to GitHub.
        min_confidence: Skip findings below this confidence.

    Returns:
        (posted_count, skipped_count) — number of findings posted and skipped.
    """
    findings = [f for f in artifact.findings if f.confidence >= min_confidence]

    if shadow:
        # Shadow mode: return counts as if we would publish, but post nothing.
        would_post = [f for f in findings if f.disposition != Disposition.dropped]
        return len(would_post), len(artifact.findings) - len(would_post)

    # Fetch the head SHA from the artifact target for the commit_id.
    # Only ReviewTarget (hosted PR) has head_sha; LocalReviewTarget has head_sha
    # as Optional. Both are handled below via the discriminated union.
    target = artifact.target
    if not isinstance(target, ReviewTarget) or not target.head_sha:
        raise ValueError(
            "publish_artifact requires a hosted ReviewTarget with a non-empty head_sha"
        )
    head_sha: str = target.head_sha

    # Idempotency: scan already-posted fingerprints
    already_posted = get_posted_fingerprints(repo, pr_number)

    posted = 0
    skipped = 0

    for finding in findings:
        if finding.disposition == Disposition.dropped:
            skipped += 1
            continue
        if finding.id in already_posted:
            skipped += 1
            continue
        try:
            post_inline_finding(repo, pr_number, head_sha, finding)
            posted += 1
        except subprocess.CalledProcessError:
            # Silently skip on individual comment failure (line may not be in diff)
            skipped += 1

    # Post summary comment only if we actually published something
    if posted > 0:
        post_summary_comment(repo, pr_number, artifact, posted, skipped, min_confidence)

    return posted, skipped


def run_github_review(
    repo: str,
    pr_number: int,
    *,
    model: str | None = None,
    verifier_model: str | None = None,
    checkout: Path | None = None,
    shadow: bool = True,
    output_path: Path | None = None,
    min_confidence: float = _MIN_CONFIDENCE,
    verify: bool = False,
    shadow_ledger: Path | None = None,
    verbose: bool = False,
) -> tuple[ReviewArtifact, int, int]:
    """End-to-end GitHub PR review: fetch → run → [verify] → publish (or shadow).

    Args:
        repo: ``OWNER/REPO`` string.
        pr_number: Pull request number.
        model: gptme model spec for Stage 1 generation. If None, uses gptme default.
        verifier_model: gptme model spec for Stage 2 adversarial verification.
                        Defaults to ``model`` when not set.
        checkout: Optional local repo checkout for AGENTS.md/CLAUDE.md context.
        shadow: If True (default), do not post anything to GitHub.
        output_path: If set, write artifact JSON here.
        min_confidence: Skip findings below this confidence threshold.
        verify: If True, run the Stage 2 adversarial verifier before publishing.
                Dropped findings go to ``shadow_ledger``; only confirmed findings post.
        shadow_ledger: Path to the JSONL shadow ledger for dropped findings.
                       Defaults to ``state/ai-review-suppressed.jsonl`` relative to
                       ``checkout`` (or cwd when checkout is not set).
        verbose: If True, print per-finding verifier outcomes to stdout.

    Returns:
        (artifact, posted_count, skipped_count)
    """
    from .reviewer import run_review
    from .verifier import verify_artifact

    meta = fetch_pr_metadata(repo, pr_number)
    base_sha = meta["base_sha"]
    head_sha = meta["head_sha"]

    diff = fetch_pr_diff(repo, pr_number)
    target = build_review_target(repo, pr_number, base_sha, head_sha)

    # Use the provided checkout, or a temporary placeholder path for repo instructions
    effective_checkout = checkout or Path.cwd()

    artifact = run_review(
        effective_checkout,
        target,
        diff,
        model=model,
        output_path=output_path,
    )

    if verify:
        artifact = verify_artifact(
            artifact,
            diff,
            effective_checkout,
            model=verifier_model or model,
            shadow_ledger=shadow_ledger,
            verbose=verbose,
            min_confidence=min_confidence,
        )
        # Persist the verified artifact back to output_path if given
        if output_path is not None:
            output_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")

    posted, skipped = publish_artifact(
        artifact,
        repo=repo,
        pr_number=pr_number,
        shadow=shadow,
        min_confidence=min_confidence,
    )

    return artifact, posted, skipped
