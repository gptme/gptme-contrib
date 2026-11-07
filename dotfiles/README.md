# Agent Dotfiles

Configuration files for gptme agents, providing global git hooks for safer development workflows.

## Installation

```bash
cd <agent-workspace>/dotfiles
./install.sh
```

## Features

### Global Git Hooks

The dotfiles install global git hooks that apply to ALL repositories:

#### pre-commit
- **Master commit protection**: Blocks direct commits to master/main in external repos
- **Commit identity guard**: Blocks commits whose `user.email` isn't allowlisted
  (see [Allowed Commit Identities](#allowed-commit-identities))
- **Branch base validation**: Warns if branch isn't based on latest origin/master
- **Pre-commit integration**: Auto-stages files modified by formatters

#### pre-push
- **Mass-deletion guard**: Scans pushed commits, exempting history already tracked
  on configured non-destination remotes (an accident-prevention trust assumption,
  not protection against hostile local configuration). Existing branches use
  Git's advertised old SHA when its commit object is available locally. An
  unfetched old tip falls back to scanning local history with only the same
  non-destination exemption, bounded-scan limit and deliberate override. New
  branches never trust cached destination refs, even cached ancestors, because
  the server may have reset them.
- **Bounded scans fail closed**: More than `MASS_DELETE_COMMIT_RANGE_LIMIT`
  commits (default 500) blocks the push. Use `MASS_DELETE_COMMIT_RANGE_LIMIT=0`
  for a full scan, or deliberately set `ALLOW_MASS_DELETE=1` after inspecting
  the history. A first push without usable other-remote history can require
  this explicit choice; the hook never silently skips an over-limit scan.
- **Master push protection**: Blocks direct pushes to master/main in external repos
- **Worktree tracking**: Validates upstream tracking before push
- Prevents pushing to wrong branches

#### post-checkout
- **Branch base warning**: Shows warning when checking out branch not based on origin/master
- Helps catch branching issues early

#### prepare-commit-msg
- **Git-Session-Id trailer**: Appends `Git-Session-Id: <id>` from
  `GIT_COMMITTER_SESSION_ID`, then `AGENT_SESSION_ID` /
  `GIT_SAFE_COMMIT_SESSION_ID` / `BOB_SESSION_ID` (compatibility). If those are
  absent it maps `CC_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` through
  `$TMPDIR/agent-provenance-state/<id>.json` (`agent_session_id` or
  `bob_session_id`), and if the environment was scrubbed it walks same-cgroup
  ancestors. Existing singular or plural trailers are left alone. Opt out with
  `AGENT_SESSION_TRAILER=0`.

## core.hooksPath override (prek)

Git's `core.hooksPath` is exclusive. A repo-local value (prek sets
`.git/hooks`) silently disables `~/.config/git/hooks`, so the session trailer
never runs. `install.sh` warns when the agent workspace has a local override.
Chain the global hooks into that local dir:

```bash
./chain-global-hooks.sh /path/to/agent-workspace
# or re-run install with chaining:
CHAIN_HOOKS=1 ./install.sh
```

The helper wraps each git hook name: global first, then the original local
hook (renamed `*.local`). It is idempotent. Re-running prek may overwrite a
wrapper; run the helper again after that. Relative local paths are resolved
against the worktree root (the Git directory for bare repositories). The
installer discovers both `workspace/dotfiles` and `workspace/contrib/dotfiles`;
`AGENT_WORKSPACE` explicitly selects a different workspace.

Stdin is replayed to both hooks for `pre-push`, `pre-receive`, `post-receive`
and `post-rewrite`; commit hooks do not buffer stdin. A single `proc-receive`
handler keeps its live bidirectional connection. Two such handlers require
explicit protocol integration: installation refuses them before rewriting any
hook, and existing wrappers also refuse a second handler added later.
The same single-handler restriction applies to `fsmonitor-watchman`: its opaque
update token and NUL-delimited response cannot be combined with another handler.
Aliased directories count as one handler and are left untouched. Wrapper refreshes
publish a complete executable by atomic rename, never by truncating a live wrapper.
When the saved local
`pre-commit` is a generated prek/pre-commit runner, the contrib global hook runs
its guards and the check suite once using explicit `run --files`, retaining
formatter auto-staging without the generated runner's stash/restore cycle.
The wrapper skips that generated runner only when the global hook declares
ownership of these checks. Arbitrary local hooks still run after the global
check suite. Generated runners with a custom or missing config are refused
before any hook is rewritten. Supported shapes are a single literal standard-config
`hook-impl` command, the stock prek script-version-4 runner (which defaults to
`.pre-commit-config.yaml`), and the stock pre-commit standard-config runner.
The helper validates the complete non-comment script against these bounded shapes,
without executing it. Extra commands, traps, redirections, substitutions, repeated
options and unknown generator templates are refused, even with a generated header.
All supported shapes require an existing `.pre-commit-config.yaml`; custom runners
need explicit integration rather than silently losing their additional checks.
Wrappers also record the validated runner's content hash and refuse to skip a
saved generated runner whose contents later change. Rerun the helper to validate
the replacement; unsupported configuration changes remain refused.
It does not silently discard custom validation. When using `--hooks-dir` inside
`.git` (including linked-worktree metadata), also pass the repository argument;
the helper refuses ambiguous repository discovery before rewriting any hook.
Because `run --files` validates worktree bytes, the global hook refuses partially
staged files before running checks, leaving both index and worktree unchanged.
Stage only the intended complete file, or use an isolated worktree for a partial
commit. Submodule file edits are ignored here because they cannot enter the
parent commit; a checked-out submodule commit differing from its staged pointer
still blocks checks. Unstaged edits or deletion of `.pre-commit-config.yaml`
also block checks, even when the configuration is absent from the staged diff:
the runner must not validate against a different policy than the index holds.
Unstaged changes in other files do not block checks. No stash/restore is
introduced, so concurrent work is not hidden or restored over other sessions.
Git-configured `~/` hook paths
are expanded before installation; relative global paths use the repository's
hook working directory, like relative local paths. For non-bare repositories,
commit-side hooks resolve from the worktree root, while receive-side hooks
(`pre-receive`, `update`, `proc-receive`, `post-receive`, `post-update` and
`push-to-checkout`) resolve from the Git directory. The helper installs each group
in its own effective directory; absolute paths and explicit `--hooks-dir` /
`--global-dir` overrides name the same concrete directory for both groups.

## Customization

### Adding Allowed Repos

Edit `.config/git/allowed-repos.conf` to add repos where direct master/main commits and pushes are permitted:

```bash
ALLOWED_PATTERNS=(
    "my-agent-workspace"
    "another-agent"
    "your-agent/workspace"  # Add your agent workspaces here
)
```

This file is sourced by both `pre-commit` and `pre-push` hooks.

### Allowed Commit Identities

`pre-commit` refuses to commit unless `git config user.email` is in an allowlist.
The built-in defaults are Bob's two addresses; `install.sh` derives the allowlist
from the installing agent's **global** `user.email` (`git config --global
user.email`) and writes it to `.config/git/allowed-identities.conf` when that
identity is not a built-in:

```bash
IDENTITY_ALLOWLIST=(
    "you@example.com"
)
```

The file overrides (does not extend) the built-in defaults, is sourced next to
`allowed-repos.conf`, and is only written when absent — so it is safe to hand-edit
and re-run `install.sh`. A repo-local-only identity is deliberately *not* used
to derive it: `install.sh` is normally run from the dotfiles checkout, so a
repo-local identity there is not the agent's commit identity, and a conf naming
the wrong identity would block every commit. Set the global identity, or add the
repo-local one to the file by hand.

Forgetting it is the failure mode it exists to prevent: without the file, a
forked agent's every commit aborts with `NOT an allowed identity`.
Bypass a single commit with `ALLOW_GIT_IDENTITY=1 git commit ...`.

## Structure

```txt
dotfiles/
├── .config/
│   └── git/
│       ├── allowed-repos.conf           # Repos where master commits/pushes allowed
│       ├── allowed-identities.conf      # Commit identities allowed by pre-commit
│       │                                #   (written by install.sh)
│       └── hooks/
│           ├── pre-commit               # Main pre-commit hook
│           ├── pre-push                 # Pre-push protection + validation
│           ├── post-checkout            # Post-checkout warnings
│           ├── prepare-commit-msg       # Git-Session-Id trailer (env + cgroup recovery)
│           ├── validate-branch-base.sh  # Branch base checking
│           └── validate-worktree-tracking.sh  # Worktree validation
├── chain-global-hooks.sh                # Wrap local hooksPath so global hooks still run
├── install.sh                           # Installation script
└── README.md                            # This file
```

## How It Works

After installation, git will use `~/.config/git/hooks` as the global hooks path via:
- `core.hooksPath` set to `~/.config/git/hooks`
- `init.templateDir` set for pre-commit integration

These hooks run only when `core.hooksPath` points at them. A repo-local
`core.hooksPath` replaces the global dir rather than chaining; see
[core.hooksPath override](#corehookspath-override-prek).

## Origin

This infrastructure was developed to prevent common git workflow issues:
- Committing directly to master in external repos
- Branching from unmerged local commits
- Pushing to wrong branches due to bad worktree tracking
