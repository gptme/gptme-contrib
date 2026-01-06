#!/bin/bash
# check-names.sh - Template validation for gptme-agent-template and forks
#
# This script validates naming patterns to ensure:
# - Template stays clean of instance names (bob, alice)
# - Forks properly replace template patterns with agent names
#
# Auto-detects mode from git remote unless explicitly specified.
# See --help for usage details.

set -e

# Display help message
help() {
    cat << 'EOF'
check-names.sh - Naming validation for gptme-agent-template

USAGE:
    check-names.sh [MODE]
    check-names.sh --help

MODES:
    template    Check template has no instance names (bob, alice)
    fork        Check fork has no 'gptme-agent-template' references in code
    auto        Auto-detect from git remote (default)

DESCRIPTION:
    Validates naming patterns in template and fork repositories.

    Template mode ensures the template stays clean for forking by catching
    any instance-specific names (bob, alice) that should remain generic.

    Fork mode ensures forked agents have replaced template references while
    allowing documentation to reference the template for context.

EXAMPLES:
    # Let script auto-detect mode
    check-names.sh

    # Explicit template validation
    check-names.sh template

    # Explicit fork validation
    check-names.sh fork

PRE-COMMIT INTEGRATION:
    # In .pre-commit-config.yaml
    - repo: local
      hooks:
        - id: check-names
          name: Check naming patterns
          entry: bash scripts/precommit/check-names.sh
          language: system
          pass_filenames: false

EXIT CODES:
    0    Validation passed
    1    Validation failed or invalid usage
EOF
}

# Parse arguments
MODE="${1:-auto}"
if [ "$MODE" = "--help" ] || [ "$MODE" = "-h" ]; then
    help
    exit 0
fi

# Exclusions common to both modes (exclude validation scripts themselves)
EXCLUDES=":!scripts/precommit/check-names.sh :!Makefile :!fork.sh :!scripts/fork.py"

# Auto-detect mode from git remote if not specified
if [ "$MODE" = "auto" ]; then
    if git config --get remote.origin.url | grep -q "gptme-agent-template"; then
        MODE="template"
    else
        MODE="fork"
    fi
fi

# Run validation based on mode
case "$MODE" in
    template)
        echo "Checking template mode: no instance names allowed..."

        # Template validation: catch any bob/alice references
        # Exclude dotfiles/install.sh which legitimately uses these for env detection
        if git grep -i "bob\|alice" -- $EXCLUDES ':!dotfiles/install.sh' 2>/dev/null; then
            echo "❌ Found instance names (bob/alice) in template"
            echo "   These should remain as generic placeholders like 'agent'"
            exit 1
        fi

        echo "✓ Template validation passed"
        ;;

    fork)
        echo "Checking fork mode: no template references in code..."

        # Fork validation: ensure template name replaced in code
        # Auto-exclude documentation areas where template references provide context
        FORK_EXCLUDES="$EXCLUDES"
        FORK_EXCLUDES="$FORK_EXCLUDES :!docs/ :!knowledge/ :!journal/ :!lessons/ :!skills/"
        FORK_EXCLUDES="$FORK_EXCLUDES :!*.md"  # Markdown docs can reference template
        FORK_EXCLUDES="$FORK_EXCLUDES :!dotfiles/.config/git/hooks/"
        FORK_EXCLUDES="$FORK_EXCLUDES :!scripts/github/"  # Monitoring scripts may track template

        if git grep -i "gptme-agent-template" -- $FORK_EXCLUDES 2>/dev/null; then
            echo "❌ Found 'gptme-agent-template' references in code"
            echo "   These should be replaced with your agent's name"
            exit 1
        fi

        echo "✓ Fork validation passed"
        ;;

    *)
        echo "Error: Invalid mode '$MODE'"
        echo ""
        help
        exit 1
        ;;
esac
