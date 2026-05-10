# Read Full GitHub Context (Companion)

Full reference for `lessons/workflow/read-full-github-context.md`.

The primary lesson carries the runtime rule: do not truncate GitHub context.
This companion doc explains why the rule exists, what GitHub surfaces are easy
to miss, and how to read the full thread without wasting context.

## Why This Failure Happens

GitHub spreads relevant context across multiple views:
- `gh issue view <number>` shows metadata and body, not the full discussion
- `gh issue view <number> --comments` shows top-level issue comments
- `gh pr view <number>` shows PR metadata and description
- `gh pr view <number> --comments` shows top-level PR comments and reviews
- Inline review comments live in a separate REST or GraphQL surface

The common failure mode is reading only one of those surfaces, or reading one of
them through `head`, `tail`, or another truncating pipeline. That produces stale
responses because the newer clarifying comment or inline review never got read.

## Minimal Read Patterns

### Issues

```bash
gh issue view <number>
gh issue view <number> --comments
```

### Pull requests

```bash
gh pr view <number> --repo <owner>/<repo>
gh pr view <number> --repo <owner>/<repo> --comments
gh api repos/<owner>/<repo>/pulls/<number>/comments \
  --jq '.[] | {id, path, user: .user.login, body: (.body | split("\n")[0])}'
```

Use jq to shrink noisy API output. Do not use jq as an excuse to read a subset.
The goal is compact full context, not selective context.

## Reading Order

1. Read the issue or PR body to understand the original request.
2. Read top-level comments chronologically.
3. Read review submissions and inline review comments if it is a PR.
4. Identify the latest actionable state before replying or editing code.

If a later comment overturns an earlier one, the later comment wins.

## Anti-Patterns

- `gh issue view 123 --comments | head -50`
- `gh pr view 456 --comments | tail -20`
- Reading only `gh pr view --comments` and missing inline review threads
- Replying after the first plausible interpretation without checking later comments

## Verification

Before responding, ask:
- Did I read both the body view and the comments view?
- If this is a PR, did I also read inline review comments?
- Am I responding to the latest request instead of an earlier one?

## Related

- Primary lesson: `lessons/workflow/read-full-github-context.md`
- Related lesson: `lessons/workflow/read-pr-reviews-comprehensively.md`
