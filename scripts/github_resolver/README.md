# gptme Issue Resolver (opt-in GitHub Action)

Opt-in companion to `scripts/github_hygiene/`. When a trusted user applies the
`gptme-resolve` label or comments `/gptme-resolve` on an issue, the reusable
workflow in `.github/workflows/issue-resolver.yml` spins up a fresh checkout,
runs `gptme` against a narrow resolver prompt, and then either:

- opens a **draft** PR attached to the issue, or
- posts a failure comment and preserves any partial work on an attempt branch.

This is distinct from the warning-only issue-hygiene Action: hygiene never
touches code, resolver is authorised to make edits but deliberately never
auto-merges.

## Using in your own repo (reusable workflow)

Call the workflow from any repo without copying any scripts:

```yaml
# .github/workflows/issue-resolver.yml in your repo
name: Issue Resolver
on:
  issues:
    types: [labeled]
  issue_comment:
    types: [created]

jobs:
  resolve:
    uses: gptme/gptme-contrib/.github/workflows/issue-resolver.yml@master
    secrets:
      gptme-provider-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

The reusable workflow fetches its own resolver scripts from gptme-contrib at
runtime (into `$RUNNER_TEMP`, outside your workspace), so your repo stays
clean.

## Design invariants (Phase 1)

- **Opt-in only.** The workflow's `if:` gate only fires on an explicit
  `gptme-resolve` label, a `/gptme-resolve` comment from a trusted
  `author_association`, or an explicit `workflow_call`.
- **Never auto-merges.** PRs are always created as drafts.
- **Idempotent attempt branch.** The branch name is
  `gptme-resolver/issue-<N>`; re-triggers force-push to the same branch with
  `--force-with-lease` so history is never orphaned silently.
- **Failure-preserving.** If gptme declines or errors out with partial
  changes, those changes are pushed as a branch and mentioned in the issue
  comment.
- **Observable.** The gptme stdout log + final status JSON are uploaded as a
  workflow artifact (`gptme-resolver-output`, 30-day retention).
- **Branch-safe execution.** The agent run gets a restricted tool set
  (`read,save,patch,shell`), a shimmed `git`/`gh`, and no GitHub credentials;
  only the orchestrator is allowed to push branches, open PRs, or comment.
- **Fail-closed git invariant.** If the agent still changes HEAD or switches
  branches directly, the orchestrator treats that as an error and preserves the
  result on the attempt branch instead of trusting the run as a clean success.

## How trusted users invoke it

Either:

1. Apply the label `gptme-resolve` to an open issue. (Label creation is
   intentionally left to the repository; if the label does not exist, the
   workflow simply never fires.)
2. Post a comment whose body starts with `/gptme-resolve` from an account with
   `OWNER`, `MEMBER`, or `COLLABORATOR` association.

## Output contract

The resolver prompt instructs gptme to end its run with one of:

```text
RESOLVER_STATUS: changes
RESOLVER_SUMMARY: <one paragraph>
```

or

```text
RESOLVER_STATUS: no_changes
RESOLVER_REASON: <one paragraph>
```

`resolve_issue.py` parses these markers, double-checks against
`git status --porcelain`, and routes to the draft-PR or
failure-comment path accordingly. Missing or malformed markers fall back to an
`error` path that still preserves any dirty worktree as a branch.

## Running locally

```sh
# Dry run against a real issue, no mutations:
python scripts/github_resolver/resolve_issue.py \
    --repo gptme/gptme-contrib \
    --issue 999 \
    --workdir "$PWD" \
    --dry-run
```

## Relationship to other ideas

- `scripts/github_hygiene/` — warning-only triage for freshly opened issues.
- Research note (in Bob's brain repo):
  `knowledge/research/2026-04-23-openhands-resolver-runtime-patterns.md`.
- Idea backlog entry: `knowledge/strategic/idea-backlog.md` #169.
