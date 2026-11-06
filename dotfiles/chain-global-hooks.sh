#!/usr/bin/env bash
# Install wrappers so a repo-local core.hooksPath still runs the global hooks.
#
# Git's core.hooksPath is exclusive. prek (and similar) set it to .git/hooks,
# which silently disables ~/.config/git/hooks — including prepare-commit-msg,
# so Git-Session-Id trailers never land. This helper wraps each git hook name
# in the local dir: global hook first, then the original local hook (if any).
#
# Usage:
#   chain-global-hooks.sh [REPO]
#   chain-global-hooks.sh --hooks-dir DIR --global-dir DIR
#
# Idempotent. Re-running refreshes wrappers without stacking them.
set -euo pipefail

HOOK_NAMES=(
    applypatch-msg pre-applypatch post-applypatch
    pre-commit pre-merge-commit prepare-commit-msg commit-msg post-commit
    pre-rebase post-checkout post-merge pre-push
    pre-receive update proc-receive post-receive post-update
    push-to-checkout pre-auto-gc post-rewrite sendemail-validate
    fsmonitor-watchman post-index-change
)

MARKER="gptme-contrib chain-global-hooks wrapper"

usage() {
    sed -n '2,16p' "$0"
    exit 2
}

HOOKS_DIR=""
GLOBAL_DIR=""
REPO=""

while [ $# -gt 0 ]; do
    case "$1" in
        --hooks-dir) HOOKS_DIR="${2:?}"; shift 2 ;;
        --global-dir) GLOBAL_DIR="${2:?}"; shift 2 ;;
        -h|--help) usage ;;
        --) shift; break ;;
        -*) echo "unknown argument: $1" >&2; usage ;;
        *)
            if [ -n "$REPO" ]; then
                echo "unexpected extra argument: $1" >&2
                usage
            fi
            REPO="$1"
            shift
            ;;
    esac
done

resolve_dir() {
    local base="$1" configured="$2"
    case "$configured" in
        /*) printf '%s\n' "$configured"; return ;;
    esac
    if [ -d "$base/$configured" ] || [ -e "$base/$configured" ]; then
        (cd "$base/$configured" && pwd -P)
        return
    fi
    mkdir -p "$base/$configured"
    (cd "$base/$configured" && pwd -P)
}

if [ -z "$HOOKS_DIR" ] || [ -z "$GLOBAL_DIR" ]; then
    REPO="${REPO:-.}"
    if [ -z "$HOOKS_DIR" ]; then
        local_path="$(git -C "$REPO" config --local --get core.hooksPath 2>/dev/null || true)"
        if [ -z "$local_path" ]; then
            echo "no repo-local core.hooksPath in $REPO — global hooks already run" >&2
            exit 0
        fi
        repo_abs="$(cd "$REPO" && pwd -P)"
        git_dir="$(git -C "$REPO" rev-parse --absolute-git-dir)"
        if [ -d "$repo_abs/$local_path" ] || [ -e "$repo_abs/$local_path" ]; then
            HOOKS_DIR="$(cd "$repo_abs/$local_path" && pwd -P)"
        else
            HOOKS_DIR="$(resolve_dir "$git_dir" "$local_path")"
        fi
    fi
    if [ -z "$GLOBAL_DIR" ]; then
        GLOBAL_DIR="$(git config --global --get core.hooksPath 2>/dev/null || true)"
        if [ -z "$GLOBAL_DIR" ]; then
            echo "git config --global core.hooksPath is unset" >&2
            exit 1
        fi
        GLOBAL_DIR="$(resolve_dir "$HOME" "$GLOBAL_DIR")"
    fi
fi

mkdir -p "$HOOKS_DIR"

# Physical paths so a symlink of the global dir onto the local dir is detected
# as the same directory (logical pwd would wrap a dir into itself).
GLOBAL_DIR="$(cd "$GLOBAL_DIR" && pwd -P)"
HOOKS_DIR="$(cd "$HOOKS_DIR" && pwd -P)"

if [ "$HOOKS_DIR" = "$GLOBAL_DIR" ]; then
    echo "local hooksPath is already the global dir — nothing to chain" >&2
    exit 0
fi

write_wrapper() {
    local name="$1"
    local dest="$HOOKS_DIR/$name"
    local global_hook="$GLOBAL_DIR/$name"
    local local_hook="$HOOKS_DIR/${name}.local"
    cat >"$dest" <<EOF
#!/usr/bin/env bash
# ${MARKER}. Do not edit.
set -euo pipefail
GLOBAL_HOOK=$(printf '%q' "$global_hook")
LOCAL_HOOK=$(printf '%q' "$local_hook")
INPUT=\$(mktemp)
trap 'rm -f "\$INPUT"' EXIT
cat >"\$INPUT"
status=0
if [ -x "\$GLOBAL_HOOK" ]; then
    "\$GLOBAL_HOOK" "\$@" <"\$INPUT" || status=\$?
    [ "\$status" -eq 0 ] || exit "\$status"
fi
if [ -x "\$LOCAL_HOOK" ]; then
    "\$LOCAL_HOOK" "\$@" <"\$INPUT" || status=\$?
fi
exit "\$status"
EOF
    chmod +x "$dest"
}

chained=0
for name in "${HOOK_NAMES[@]}"; do
    global_hook="$GLOBAL_DIR/$name"
    dest="$HOOKS_DIR/$name"
    local_hook="$HOOKS_DIR/${name}.local"
    [ -x "$global_hook" ] || [ -e "$dest" ] || continue

    if [ -e "$dest" ]; then
        if grep -qF "$MARKER" "$dest" 2>/dev/null; then
            write_wrapper "$name"
            chained=$((chained + 1))
            continue
        fi
        # dest is a real local hook: first install, or prek overwrote the wrapper.
        mv "$dest" "$local_hook"
    fi
    write_wrapper "$name"
    chained=$((chained + 1))
done

echo "chained $chained hook(s): local=$HOOKS_DIR global=$GLOBAL_DIR"
exit 0
