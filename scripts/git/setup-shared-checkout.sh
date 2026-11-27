#!/usr/bin/env bash
# Prepare a checkout that several agent sessions (or hosts) commit to.
#
# Usage: setup-shared-checkout.sh [--check] [REPO]   (REPO defaults to cwd)
#
# What it does, all local to REPO (nothing global, nothing pushed):
#   1. git config safety.sharedCheckout true   -> opts the repo into the Claude
#      Code guard (scripts/claude-code-hooks/shared-checkout-git-guard.py).
#   2. git config pull.ff only, rebase.autoStash false, merge.autoStash false
#      -> a pull in a dirty tree refuses instead of silently stashing other
#      sessions' work (an orphaned or conflicting autostash loses it).
#   3. Installs a pre-commit hook that execs scripts/git/pre-commit-auto-stage:
#      commits are serialized on .git/commit.lock and hooks run with
#      --files <staged paths>, so they never stash unstaged changes the way a
#      plain `prek install` / `pre-commit install` hook does.
#
# The hook locates gptme-contrib through $HOME when this checkout lives under
# $HOME, so a .git synced between hosts with different home paths keeps working.
# An existing different hook is kept as pre-commit.bak.<timestamp>.
#
# --check reports the current state and exits 1 if anything is missing.

set -euo pipefail

CHECK=0
REPO="."
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    -h | --help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "setup-shared-checkout: unknown option $arg" >&2
      exit 2
      ;;
    *) REPO="$arg" ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
AUTO_STAGE="$SCRIPT_DIR/pre-commit-auto-stage"
MARKER="setup-shared-checkout.sh"

if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  echo "setup-shared-checkout: $REPO is not a git repository" >&2
  exit 2
fi
# --git-common-dir may be relative to REPO, not to our cwd.
COMMON_DIR="$(cd "$REPO" && cd "$(git rev-parse --git-common-dir)" && pwd -P)"

if [ -n "$(git -C "$REPO" config --get core.hooksPath || true)" ]; then
  HOOKS_PATH_SET=1
else
  HOOKS_PATH_SET=0
fi
HOOK="$COMMON_DIR/hooks/pre-commit"

# Location of pre-commit-auto-stage as written into the hook.
case "$AUTO_STAGE" in
  "$HOME"/*) HOOK_TARGET="\$HOME/${AUTO_STAGE#"$HOME"/}" ;;
  *) HOOK_TARGET="$AUTO_STAGE" ;;
esac

hook_body() {
  cat <<EOF
#!/usr/bin/env bash
# Installed by gptme-contrib scripts/git/$MARKER: serialize commits and run
# hooks on staged files only (never stash other sessions' unstaged changes).
HOOK="$HOOK_TARGET"
if [ ! -x "\$HOOK" ]; then
  echo "pre-commit: \$HOOK not found (is gptme-contrib cloned at the same path on this host?)" >&2
  exit 1
fi
command -v flock >/dev/null 2>&1 || { echo "pre-commit: flock not found (macOS: brew install flock; Linux: util-linux)" >&2; exit 1; }
exec "\$HOOK" "\$@"
EOF
}

# key=value pairs (no associative arrays: macOS ships bash 3.2).
WANT="safety.sharedCheckout=true pull.ff=only rebase.autoStash=false merge.autoStash=false"

missing=0
for pair in $WANT; do
  key="${pair%%=*}"
  want="${pair#*=}"
  have="$(git -C "$REPO" config --local --get "$key" || true)"
  if [ "$have" != "$want" ]; then
    missing=1
    if [ "$CHECK" = 1 ]; then
      echo "missing: git config $key $want (have: ${have:-unset})"
    else
      git -C "$REPO" config --local "$key" "$want"
      echo "set: git config $key $want"
    fi
  fi
done

if [ "$HOOKS_PATH_SET" = 1 ]; then
  echo "warning: core.hooksPath is set; install the pre-commit hook there by hand" >&2
  missing=1
elif [ -f "$HOOK" ] && [ "$(cat "$HOOK")" = "$(hook_body)" ]; then
  :
elif [ "$CHECK" = 1 ]; then
  echo "missing: $HOOK (non-stashing pre-commit hook)"
  missing=1
else
  mkdir -p "$COMMON_DIR/hooks"
  if [ -e "$HOOK" ] || [ -L "$HOOK" ]; then
    backup="$HOOK.bak.$(date +%Y%m%d%H%M%S)"
    mv "$HOOK" "$backup"
    echo "kept old hook: $backup"
  fi
  hook_body >"$HOOK"
  chmod +x "$HOOK"
  echo "installed: $HOOK"
fi

if ! command -v flock >/dev/null 2>&1; then
  echo "warning: flock not found; commits will fail until it is installed (macOS: brew install flock)" >&2
  missing=1
fi

if [ "$CHECK" = 1 ]; then
  [ "$missing" = 0 ] && echo "ok: $(cd "$REPO" && pwd -P) is set up as a shared checkout"
  exit "$missing"
fi

# Global settings can reintroduce silent stashing in other repos; point them out.
if [ "$(git config --global --get rebase.autoStash || true)" = "true" ]; then
  echo "note: global rebase.autoStash=true still applies to other repos (overridden here)"
fi
if [ "$HOOKS_PATH_SET" = 1 ]; then
  echo "incomplete: config set, but the pre-commit hook was not installed (core.hooksPath)" >&2
  exit 1
fi
echo "done. Claude Code guard: add scripts/claude-code-hooks/shared-checkout-git-guard.py"
echo "as a PreToolUse Bash hook in ~/.claude/settings.json (see its docstring)."
exit 0
