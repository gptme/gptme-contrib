#!/bin/bash
# Template validation for gptme-agent-template and forks
set -e

MODE="${1:-auto}"
EXCLUDES=":!scripts/check-names.sh :!Makefile :!fork.sh :!scripts/fork.py"

# Auto-detect mode if not specified
if [ "$MODE" = "auto" ]; then
    if git config --get remote.origin.url | grep -q "gptme-agent-template"; then
        MODE="template"
    else
        MODE="fork"
    fi
fi

case "$MODE" in
    template)
        echo "Checking template mode: no instance names allowed..."
        # Template should not have instance-specific names
        # Exclude dotfiles/install.sh (legitimately references bob/alice for env detection)
        if git grep -i "bob\|alice" -- $EXCLUDES ':!dotfiles/install.sh' 2>/dev/null; then
            echo "❌ Found instance names (bob/alice) in template"
            exit 1
        fi
        echo "✓ Template validation passed"
        ;;
    
    fork)
        echo "Checking fork mode: no template references in code..."
        # Fork should not reference "gptme-agent-template"
        # Automatically exclude:
        # - Documentation directories (docs/, knowledge/, journal/, lessons/, skills/)
        # - Markdown files (*.md)
        # - Git hooks (dotfiles/.config/git/hooks/)
        # - Monitoring scripts that track template repo (scripts/github/)
        FORK_EXCLUDES="$EXCLUDES :!docs/ :!knowledge/ :!journal/ :!lessons/ :!skills/ :!*.md"
        FORK_EXCLUDES="$FORK_EXCLUDES :!dotfiles/.config/git/hooks/ :!scripts/github/"
        
        if git grep -i "gptme-agent-template" -- $FORK_EXCLUDES 2>/dev/null; then
            echo "❌ Found 'gptme-agent-template' references in code"
            exit 1
        fi
        echo "✓ Fork validation passed"
        ;;
    
    *)
        echo "Usage: $0 [template|fork|auto]"
        echo ""
        echo "Validates naming patterns in gptme-agent-template and forks."
        echo ""
        echo "Modes:"
        echo "  template - Check template has no instance names (bob/alice)"
        echo "  fork     - Check fork has no 'gptme-agent-template' references in code"
        echo "  auto     - Detect mode from git remote (default)"
        exit 1
        ;;
esac
