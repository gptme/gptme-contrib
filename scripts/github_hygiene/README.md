# GitHub Hygiene Action (warning-only)

Prototype of a gptme-powered GitHub Action that posts at most **one
warning-only comment** on newly-opened issues covering:

- **Likely duplicates** — scanned against recent open issues
- **Missing info** — version, OS, repro steps, expected vs actual
- **Routing suggestion** — a single label hint

It never closes issues, never edits labels, never edits the issue body. If the
model finds nothing to say it emits the sentinel `NO_ISSUES` and the
orchestrator skips commenting.

Inspired by OpenCode's `duplicate-issues.yml`/`triage.yml` pattern, with the
aggressive 2-hour auto-close policy deliberately left out. See the Bob research
note linked from the PR for the full peer analysis.

## Layout

```
scripts/github_hygiene/
├── issue_hygiene.py          # Orchestrator (idempotency + prompt render)
├── prompts/
│   └── issue-hygiene.md      # Prompt template (uses str.format placeholders)
├── test_issue_hygiene.py     # Marker + prompt-render tests (no I/O)
└── README.md                 # This file
```

## Idempotency

Every comment posted by this orchestrator leads with an HTML marker:

```
<!-- gptme-issue-hygiene: v1 -->
```

Before calling gptme, the orchestrator fetches the issue's comments and skips
if any comment already carries the marker. The marker is versioned on purpose:
a schema-breaking change (new sections, different output contract) must bump
`v1` to `v2` so already-processed issues can be re-evaluated.

## Local dry-run

```shell
# Requires gh authenticated against the target repo and gptme in PATH
uv run python scripts/github_hygiene/issue_hygiene.py \
  --repo gptme/gptme-contrib \
  --issue 123 \
  --dry-run
```

`--dry-run` renders the prompt, calls gptme, and prints the comment that
would be posted without touching GitHub. Useful for calibrating the prompt
against real recent issues.

## Tests

```shell
uv run pytest scripts/github_hygiene/test_issue_hygiene.py -v
```

Tests cover:

- Marker is versioned, present exactly once, and hidden as an HTML comment.
- Every `{…}` placeholder in the prompt template is substituted.
- Empty issue bodies and empty label lists render sensibly.
- Issue bodies containing `{` / `}` (e.g. JSON snippets) do not trip `format`.
- `NO_ISSUES` skip token matches the literal string in the prompt template.

No network or subprocess calls are exercised — the orchestrator's `gh` and
`gptme` wrappers are thin and will be exercised in the staged rollout.

## Using the reusable workflow

In a downstream repo's `.github/workflows/issue-hygiene.yml`:

```yaml
name: Issue Hygiene
on:
  issues:
    types: [opened]

jobs:
  hygiene:
    uses: gptme/gptme-contrib/.github/workflows/issue-hygiene.yml@master
    secrets:
      gptme-provider-key: ${{ secrets.OPENAI_API_KEY }}
```

## Staged rollout

1. **Land the prototype behind manual dispatch** on `gptme-contrib` only.
2. **One week warning-only** on `gptme-contrib`; audit comments for
   false-positive rate.
3. If FP rate is low, promote to `gptme/gptme`.
4. Only then consider expanding beyond hygiene (e.g. label application).

Do not copy OpenCode's 2-hour auto-close window until the false-positive rate
is demonstrated low on a small repo first.
