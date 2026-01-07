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
- **Branch base validation**: Warns if branch isn't based on latest origin/master
- **Pre-commit integration**: Auto-stages files modified by formatters

#### pre-push
- **Master push protection**: Blocks direct pushes to master/main in external repos
- **Worktree tracking**: Validates upstream tracking before push
- Prevents pushing to wrong branches

#### post-checkout
- **Branch base warning**: Shows warning when checking out branch not based on origin/master
- Helps catch branching issues early

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

## Structure

```txt
dotfiles/
├── .config/
│   └── git/
│       ├── allowed-repos.conf           # Repos where master commits/pushes allowed
│       └── hooks/
│           ├── pre-commit               # Main pre-commit hook
│           ├── pre-push                 # Pre-push protection + validation
│           ├── post-checkout            # Post-checkout warnings
│           ├── validate-branch-base.sh  # Branch base checking
│           └── validate-worktree-tracking.sh  # Worktree validation
├── install.sh                           # Installation script
└── README.md                            # This file
```

## How It Works

After installation, git will use `~/.config/git/hooks` as the global hooks path via:
- `core.hooksPath` set to `~/.config/git/hooks`
- `init.templateDir` set for pre-commit integration

These hooks run BEFORE any repo-local hooks, providing a safety net across all repositories.

## Origin

This infrastructure was developed to prevent common git workflow issues:
- Committing directly to master in external repos
- Branching from unmerged local commits
- Pushing to wrong branches due to bad worktree tracking
