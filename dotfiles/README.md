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

## MCP Server Configuration

The dotfiles include a template for MCP (Model Context Protocol) server configuration.

### Setup

MCP configuration is **not** installed automatically by `install.sh`. To set up MCP servers:

1. Copy the template to your config directory:
   ```bash
   mkdir -p ~/.config/mcp
   cp .config/mcp/mcp_servers.json.template ~/.config/mcp/mcp_servers.json
   ```

2. Edit `~/.config/mcp/mcp_servers.json` and replace placeholder values:
   - Replace `ntn_your_token_here` with your actual Notion API token
   - Add additional MCP servers as needed

3. Use with `mcp-cli`:
   ```bash
   mcp-cli              # List available servers
   mcp-cli notion       # List tools for Notion server
   mcp-cli notion/search '{"query": "meeting notes"}'  # Call a tool
   ```

### Template Structure

The template uses underscore-prefixed keys (`_comment`, `_description`, `_docs`, `_requires`) for documentation. These are ignored by MCP parsers and help document the configuration.

**Note**: The placeholder `ntn_your_token_here` in the template is a literal value that must be replaced with your actual token. It is not shell-substituted.

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
