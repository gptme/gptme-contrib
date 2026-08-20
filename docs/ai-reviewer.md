# The AI code reviewer

The `## 🤖 AI code review` comment that [@TimeToBuildBob](https://github.com/TimeToBuildBob)
posts on pull requests in the gptme org comes from a self-hosted reviewer that Bob
(an autonomous agent) runs. This page explains what the comment is, what it is not,
and how to drive it.

## What the comment contains

1. **A one-paragraph description of the diff** — what the reviewer understood the
   change to be. If it does not match what you changed, the findings were built on a
   misreading; treat them accordingly.
2. **Confidence Score** — derived purely from the surviving findings:
   5/5 no findings · 4/5 P2 only · 3/5 one P1 · 2/5 two or more P1 · 1/5 any P0.
   It states the finding state, not a merge decision.
3. **Findings** — each posted inline on the diff where GitHub allows it, collapsed
   under one heading. Findings the reviewer could not anchor to a diff line appear
   in full with a permalink. Findings in files the PR did not touch are shown but
   do not affect the score.
4. **Files changed** — the reviewer's per-file reading, for checking its coverage.
5. **Footer** — reviewed SHA, model, engine, wall time. The comment is edited in
   place on every new head; `Previous review passes` keeps the history.

Every comment carries a `<!-- bob-ai-review {...} -->` marker with the machine-readable
state (sha, score, finding fingerprints, history) that the merge gate reads.

## Maintainer commands

Post either trigger **on its own line** in a PR comment. Only OWNER / MEMBER /
COLLABORATOR accounts can fire them; prose that merely mentions the phrase does not.

| Comment | Effect |
|---|---|
| `@TimeToBuildBob review` | Fresh review of the current head, even if it was already reviewed. |
| `@TimeToBuildBob fix` | A worker session acts on the findings already posted (bot-authored PRs). |

Each trigger fires **once per comment**. A 👀 reaction on your comment is the receipt
that it was picked up; no reaction after a while means it was not seen (wrong
association, not on its own line, or the reviewer is down).

## How it relates to merging

The reviewer reports; it does not merge. The self-merge gate
([`scripts/github/self-merge-check.py`](../scripts/github/self-merge-check.py))
treats surviving **P0/P1** findings as blocking until each thread is disposed
(fixed, or replied to and resolved). P2s never block on their own. Resolving a
thread with no reply is not a disposition.

## Source

- Reviewer and comment renderer: `scripts/github/ai-review.py` + `ai_review_lib.py`
  in Bob's workspace (private repo; the reviewer is being extracted, see
  [`packages/gptme-runloops/src/gptme_runloops/pr_review/`](../packages/gptme-runloops/src/gptme_runloops/pr_review/)).
- `@TimeToBuildBob fix` trigger: [`scripts/github/activity-gate.sh`](../scripts/github/activity-gate.sh).
- Merge gate: [`scripts/github/self-merge-check.py`](../scripts/github/self-merge-check.py).
