#!/usr/bin/env bash
# Install the `end` skill as a slash command in every harness on this machine.
#
#   bash install.sh            # ~/.claude/skills/end + ~/.codex/skills/end
#   bash install.sh --project  # also <git toplevel>/.claude/skills/end
#   bash install.sh --uninstall
#
# Idempotent: symlinks are (re)pointed at this directory; nothing is copied.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
NAME="$(basename "$SKILL_DIR")"
PROJECT=0
UNINSTALL=0
for a in "$@"; do
    case "$a" in
        --project) PROJECT=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown arg: $a" >&2; exit 2 ;;
    esac
done

targets=(
    "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/$NAME"
    "${CODEX_HOME:-$HOME/.codex}/skills/$NAME"
)
if [ "$PROJECT" = 1 ]; then
    top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -z "$top" ]; then
        echo "--project: not inside a git repo" >&2
        exit 1
    fi
    targets+=("$top/.claude/skills/$NAME")
fi

link_one() {
    local dst="$1"
    if [ "$UNINSTALL" = 1 ]; then
        if [ -L "$dst" ]; then rm "$dst"; echo "removed  $dst"; fi
        return
    fi
    mkdir -p "$(dirname "$dst")"
    if [ -e "$dst" ] && [ ! -L "$dst" ]; then
        echo "skip     $dst (exists and is not a symlink)" >&2
        return
    fi
    ln -sfn "$SKILL_DIR" "$dst"
    echo "linked   $dst -> $SKILL_DIR"
}

for t in "${targets[@]}"; do link_one "$t"; done

[ "$UNINSTALL" = 1 ] && exit 0
cat <<EOF

Claude Code: /end            (restart the session to pick up the new skill)
Codex:       /end  or  \$end  (skills dir: ${CODEX_HOME:-$HOME/.codex}/skills)
gptme:       /skill:end      (needs gptme with skill slash-commands; otherwise say "wrap up the session")
EOF
