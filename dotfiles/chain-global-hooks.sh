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
RECEIVE_HOOKS_DIR=""
RECEIVE_GLOBAL_DIR=""

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
        (cd "$base/$configured" && pwd -L)
        return
    fi
    mkdir -p "$base/$configured"
    (cd "$base/$configured" && pwd -L)
}

if [ -z "$HOOKS_DIR" ] || [ -z "$GLOBAL_DIR" ]; then
    REPO="${REPO:-.}"
    if [ -z "$HOOKS_DIR" ]; then
        local_path="$(git -C "$REPO" config --local --path --get core.hooksPath 2>/dev/null || true)"
        if [ -z "$local_path" ]; then
            echo "no repo-local core.hooksPath in $REPO — global hooks already run" >&2
            exit 0
        fi
        if [ "$(git -C "$REPO" rev-parse --is-bare-repository)" = "true" ]; then
            hook_base="$(git -C "$REPO" rev-parse --absolute-git-dir)"
        else
            hook_base="$(git -C "$REPO" rev-parse --show-toplevel)"
        fi
        HOOKS_DIR="$(resolve_dir "$hook_base" "$local_path")"
        RECEIVE_HOOKS_DIR="$(resolve_dir "$(git -C "$REPO" rev-parse --absolute-git-dir)" "$local_path")"
    fi
    if [ -z "$GLOBAL_DIR" ]; then
        GLOBAL_DIR="$(git config --global --path --get core.hooksPath 2>/dev/null || true)"
        if [ -z "$GLOBAL_DIR" ]; then
            echo "git config --global core.hooksPath is unset" >&2
            exit 1
        fi
        if [ "$(git -C "$REPO" rev-parse --is-bare-repository)" = "true" ]; then
            hook_base="$(git -C "$REPO" rev-parse --absolute-git-dir)"
        else
            hook_base="$(git -C "$REPO" rev-parse --show-toplevel)"
        fi
        RECEIVE_GLOBAL_DIR="$(resolve_dir "$(git -C "$REPO" rev-parse --absolute-git-dir)" "$GLOBAL_DIR")"
        GLOBAL_DIR="$(resolve_dir "$hook_base" "$GLOBAL_DIR")"
    fi
fi

mkdir -p "$HOOKS_DIR"

# Compare physical paths to avoid wrapping a directory into itself, but invoke
# global hooks through the installed path: they read agent config beside it.
GLOBAL_DIR="$(cd "$GLOBAL_DIR" && pwd -L)"
HOOKS_DIR="$(cd "$HOOKS_DIR" && pwd -P)"

# Receive-pack runs these hooks from GIT_DIR even for non-bare repositories.
# Explicit --*-dir overrides are already concrete directories for both groups.
RECEIVE_HOOKS_DIR="$(cd "${RECEIVE_HOOKS_DIR:-$HOOKS_DIR}" && pwd -P)"
RECEIVE_GLOBAL_DIR="$(cd "${RECEIVE_GLOBAL_DIR:-$GLOBAL_DIR}" && pwd -L)"

if [ "$HOOKS_DIR" = "$(cd "$GLOBAL_DIR" && pwd -P)" ] && \
   [ "$RECEIVE_HOOKS_DIR" = "$(cd "$RECEIVE_GLOBAL_DIR" && pwd -P)" ]; then
    echo "local hooksPath is already the global dir — nothing to chain" >&2
    exit 0
fi

# proc-receive has a bidirectional pkt-line exchange, not replayable input.
# Refuse dual handlers before changing any hook, including on repeated installs.
saved_proc_receive="$RECEIVE_HOOKS_DIR/proc-receive"
if grep -qF "$MARKER" "$saved_proc_receive" 2>/dev/null; then
    saved_proc_receive="$saved_proc_receive.local"
fi
if [ "$RECEIVE_HOOKS_DIR" != "$(cd "$RECEIVE_GLOBAL_DIR" && pwd -P)" ] && \
   [ -x "$RECEIVE_GLOBAL_DIR/proc-receive" ] && [ -x "$saved_proc_receive" ]; then
    echo "cannot chain two proc-receive handlers; explicit protocol integration required; hooks unchanged" >&2
    exit 1
fi

# fsmonitor returns one opaque token and a NUL-delimited path list. Two handlers
# cannot share a token or concatenate their responses; keep a single live owner.
saved_fsmonitor="$HOOKS_DIR/fsmonitor-watchman"
if grep -qF "$MARKER" "$saved_fsmonitor" 2>/dev/null; then
    saved_fsmonitor="$saved_fsmonitor.local"
fi
if [ "$HOOKS_DIR" != "$(cd "$GLOBAL_DIR" && pwd -P)" ] && \
   [ -x "$GLOBAL_DIR/fsmonitor-watchman" ] && [ -x "$saved_fsmonitor" ]; then
    echo "cannot chain two fsmonitor-watchman handlers; explicit protocol integration required; hooks unchanged" >&2
    exit 1
fi

# Fail before rewriting any hooks if a generated runner cannot be replaced by
# our standard-config --files invocation. Never execute/source a runner to parse
# its config. Nonstandard installations need explicit integration, not a bypass.
validated_precommit_hash=""
saved_precommit="$HOOKS_DIR/pre-commit"
if grep -qF "$MARKER" "$saved_precommit" 2>/dev/null; then
    saved_precommit="$saved_precommit.local"
fi
if [ -x "$saved_precommit" ] && \
   grep -qE '^# File generated by (prek|pre-commit)([ :]|$)' "$saved_precommit" && \
   grep -qF '# gptme-contrib: owns-generated-precommit-checks' "$GLOBAL_DIR/pre-commit" 2>/dev/null; then
    repo_root="$(git -C "${REPO:-$HOOKS_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -z "$repo_root" ]; then
        echo "cannot safely replace generated pre-commit runner: pass REPO explicitly with --hooks-dir inside a Git directory; hooks unchanged" >&2
        exit 1
    fi
    if [ ! -f "$repo_root/.pre-commit-config.yaml" ] || \
       ! python3 - "$saved_precommit" <<'PY'
import re
import shlex
import sys

# Recognize entire supported runner shapes, not a header plus selected words.
# This is deliberately not a shell parser: custom commands, traps, redirections,
# substitutions and unknown generator versions require explicit integration.
# Never execute/source a runner while deciding whether it can be skipped.
try:
    with open(sys.argv[1]) as runner:
        source = runner.read()
    # Comments/blank lines have no shell effect in these exact line templates.
    lines = [line for line in source.splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    manager = re.search(r"^# File generated by (prek|pre-commit)(?:[ :]|$)",
                        source, re.MULTILINE)
    if manager is None:
        sys.exit(1)
    manager = manager.group(1)

    def simple_runner(line):
        # Restrict spelling before shlex discards quotes around shell operators.
        literal = line.removesuffix(' -- "$@"')
        if not re.fullmatch(r'''[a-zA-Z0-9_./= '\"\-]+''', literal):
            return False
        words = shlex.split(line)
        if words[0] == "exec":
            words.pop(0)
        if words[:2] != [manager, "hook-impl"]:
            return False
        args = words[2:]
        if args[-2:] == ["--", "$@"]:
            args = args[:-2]
        if args[:1] == ["--config=.pre-commit-config.yaml"]:
            args = args[1:]
        elif args[:2] in (["--config", ".pre-commit-config.yaml"],
                          ["-c", ".pre-commit-config.yaml"]):
            args = args[2:]
        else:
            return False
        return args in ([], ["--hook-type=pre-commit"], ["--hook-type", "pre-commit"])

    here = 'HERE="$(cd "$(dirname "$0")" && pwd)"'
    if len(lines) == 1 and simple_runner(lines[0]):
        sys.exit(0)

    # Current stock prek/pre-commit templates. Only the installed executable
    # path is variable; its assignment must be a literal, not shell code.
    def literal_assignment(line, name):
        if not line.startswith(name + "="):
            return False
        value = line[len(name) + 1:]
        # Exactly one attached assignment word. shlex.split(value) alone
        # would accept `INSTALL_PYTHON= ./custom-check`, which runs a command.
        path_chars = r"[a-zA-Z0-9_./:+\-]"
        return bool(re.fullmatch(
            path_chars + r"+|'[a-zA-Z0-9_./ :+\-]*'|\"[a-zA-Z0-9_./ :+\-]*\"",
            value,
        ))

    if manager == "prek" and len(lines) == 6:
        valid = (lines[0] == here and literal_assignment(lines[1], "PREK")
                 and lines[2:5] == ['if [ ! -x "$PREK" ]; then',
                                    '    PREK="prek"', 'fi']
                 and lines[5] == 'exec "$PREK" hook-impl --hook-dir "$HERE" --script-version 4 --hook-type=pre-commit -- "$@"')
        sys.exit(0 if valid else 1)
    if manager == "pre-commit" and len(lines) == 12:
        valid = (literal_assignment(lines[0], "INSTALL_PYTHON")
                 and lines[1:] == [
                     'ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit)',
                     here, 'ARGS+=(--hook-dir "$HERE" -- "$@")',
                     'if [ -x "$INSTALL_PYTHON" ]; then',
                     '    exec "$INSTALL_PYTHON" -mpre_commit "${ARGS[@]}"',
                     'elif command -v pre-commit > /dev/null; then',
                     '    exec pre-commit "${ARGS[@]}"', 'else',
                     "    echo '`pre-commit` not found.  Did you forget to activate your virtualenv?' 1>&2",
                     '    exit 1', 'fi'])
        sys.exit(0 if valid else 1)
    sys.exit(1)
except (OSError, ValueError, IndexError):
    sys.exit(1)
PY
    then
        echo "cannot safely replace generated pre-commit runner: requires an existing .pre-commit-config.yaml and a supported unmodified standard-config runner; hooks unchanged" >&2
        exit 1
    fi
    validated_precommit_hash="$(git -C "$repo_root" hash-object --no-filters -- "$saved_precommit")"
fi

write_wrapper() {
    local name="$1"
    local dest="$HOOKS_DIR/$name"
    local global_hook="$GLOBAL_DIR/$name"
    local local_hook="$HOOKS_DIR/${name}.local"
    local temp
    temp="$(mktemp "$HOOKS_DIR/.${name}.XXXXXX")"
    if ! cat >"$temp" <<EOF
#!/usr/bin/env bash
# ${MARKER}. Do not edit.
set -euo pipefail
GLOBAL_HOOK=$(printf '%q' "$global_hook")
LOCAL_HOOK=$(printf '%q' "$local_hook")
VALIDATED_PRECOMMIT_HASH=$(printf '%q' "$validated_precommit_hash")
# Only these hooks receive a finite stdin protocol. Reading stdin for e.g.
# prepare-commit-msg can block forever on an inherited interactive terminal.
case $(printf '%q' "$name") in
    proc-receive|fsmonitor-watchman)
        # Recheck at runtime: a second handler may be installed later.
        if [ -x "\$GLOBAL_HOOK" ] && [ -x "\$LOCAL_HOOK" ]; then
            echo 'cannot chain two ${name} handlers; explicit protocol integration required' >&2
            exit 1
        fi
        if [ -x "\$GLOBAL_HOOK" ]; then
            exec "\$GLOBAL_HOOK" "\$@"
        fi
        if [ -x "\$LOCAL_HOOK" ]; then
            exec "\$LOCAL_HOOK" "\$@"
        fi
        ;;
    pre-push|pre-receive|post-receive|post-rewrite)
        INPUT=\$(mktemp)
        trap 'rm -f "\$INPUT"' EXIT
        cat >"\$INPUT"
        if [ -x "\$GLOBAL_HOOK" ]; then
            "\$GLOBAL_HOOK" "\$@" <"\$INPUT" || exit \$?
        fi
        if [ -x "\$LOCAL_HOOK" ]; then
            "\$LOCAL_HOOK" "\$@" <"\$INPUT" || exit \$?
        fi
        ;;
    *)
        if [ -x "\$GLOBAL_HOOK" ]; then
            "\$GLOBAL_HOOK" "\$@" || exit \$?
        fi
        if [ -x "\$LOCAL_HOOK" ]; then
            # The contrib global pre-commit runs the suite with --files, never
            # hook-impl (which stashes concurrent work). Skip only recognized
            # generated runners, and only with a global hook that owns checks.
            if [ $(printf '%q' "$name") = pre-commit ] && \\
               [ -x "\$GLOBAL_HOOK" ] && \\
               grep -qF '# gptme-contrib: owns-generated-precommit-checks' "\$GLOBAL_HOOK" && \\
               grep -qE '^# File generated by (prek|pre-commit)([ :]|$)' "\$LOCAL_HOOK"; then
                if [ -z "\$VALIDATED_PRECOMMIT_HASH" ] || \\
                   [ "\$(git hash-object --no-filters -- "\$LOCAL_HOOK")" != "\$VALIDATED_PRECOMMIT_HASH" ]; then
                    echo 'saved generated pre-commit runner changed; rerun chain-global-hooks.sh to validate it' >&2
                    exit 1
                fi
                if [ ! -f .pre-commit-config.yaml ] || \\
                   { ! command -v prek >/dev/null && ! command -v pre-commit >/dev/null; }; then
                    echo 'chained pre-commit config or runner unavailable; refusing to skip checks' >&2
                    exit 1
                fi
                exit 0
            fi
            exec "\$LOCAL_HOOK" "\$@"
        fi
        ;;
esac
EOF
    then
        rm -f "$temp"
        return 1
    fi
    if ! chmod +x "$temp" || ! mv -f "$temp" "$dest"; then
        rm -f "$temp"
        return 1
    fi
}

chained=0
ordinary_hooks_dir="$HOOKS_DIR"
ordinary_global_dir="$GLOBAL_DIR"
for name in "${HOOK_NAMES[@]}"; do
    case "$name" in
        pre-receive|update|proc-receive|post-receive|post-update|push-to-checkout)
            HOOKS_DIR="$RECEIVE_HOOKS_DIR"
            GLOBAL_DIR="$RECEIVE_GLOBAL_DIR"
            ;;
        *)
            HOOKS_DIR="$ordinary_hooks_dir"
            GLOBAL_DIR="$ordinary_global_dir"
            ;;
    esac
    # One context can alias the global directory while the other does not.
    [ "$HOOKS_DIR" != "$(cd "$GLOBAL_DIR" && pwd -P)" ] || continue
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
