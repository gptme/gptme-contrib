# shellcheck shell=bash
# Trajectory sources — the single source of truth for "what must never be lost".
#
# Sourced by trajectory-backup.sh (hardlink backup) and
# trajectory-health-check.sh (per-harness alarms) so the two can never disagree
# about which harnesses are covered.
#
# WHY THIS FILE EXISTS
# --------------------
# Agent trajectories (the full record of what a session did and why) are the
# substrate for self-improvement, debugging and behavioural analysis. They are
# small and cheap; losing them is irreversible.
#
# The failure this guards against is subtle: a backup path hardcoded to ONE
# harness. If you back up `~/.claude/projects/...` but not `~/.local/share/gptme/
# logs/`, then the day you fail over to the gptme harness you are running on the
# one lane nobody is preserving. A hardcoded per-harness path is a latent data-
# loss incident — it does not lose data today, it loses whichever harness you
# adopt next. So harness coverage is DATA (this table), and both the backup and
# the health check walk it. Adding a harness is one line here, nothing else.
#
# ADDING A HARNESS: append one line to TRAJECTORY_SOURCES (or set
# TRAJECTORY_EXTRA_SOURCES in the environment — same `name|src|dest|spec` format,
# newline-separated). Sources that do not exist are reported and skipped, never
# alarmed — listing a harness before it is in use is deliberate, so its first
# session is backed up rather than discovered missing by a later incident.
#
# Format: name|source dir|destination dir|unit spec
#   name       short harness label, used in output and as the alarm subject
#   source     live directory the harness writes trajectories into
#   dest       hardlink-backup destination (under TRAJECTORY_BACKUP_ROOT)
#   unit spec  what counts as one trajectory, as <type>:<glob>:
#                file:<glob>   files directly in source      (Claude Code .jsonl)
#                dir:<glob>    directories directly in source (gptme conv dirs)
#                rfile:<glob>  files anywhere under source    (Codex date tree)

TRAJECTORY_BACKUP_ROOT="${TRAJECTORY_BACKUP_ROOT:-$HOME/data/trajectories}"

# Primary Claude Code project dir. Claude Code encodes a workspace path into a
# projects dir by replacing every "/" with "-". If WORKSPACE is set and the
# encoded dir exists we use it; otherwise we fall back to the largest projects
# dir (the one with the most trajectories), or empty if Claude Code is unused.
# Other phases/checks (session-dir count) key off this single "primary" dir.
_encode_cc_project() { printf '%s' "$1" | sed 's#/#-#g'; }
if [[ -z "${CC_PROJECT_DIR:-}" ]]; then
    _cc_root="$HOME/.claude/projects"
    if [[ -n "${WORKSPACE:-}" && -d "$_cc_root/$(_encode_cc_project "$WORKSPACE")" ]]; then
        CC_PROJECT_DIR="$_cc_root/$(_encode_cc_project "$WORKSPACE")"
    elif [[ -d "$_cc_root" ]]; then
        CC_PROJECT_DIR="$(find "$_cc_root" -maxdepth 1 -mindepth 1 -type d \
            -exec sh -c 'printf "%s %s\n" "$(find "$1" -maxdepth 1 -name "*.jsonl" | wc -l)" "$1"' _ {} \; \
            2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"
    fi
    CC_PROJECT_DIR="${CC_PROJECT_DIR:-$HOME/.claude/projects/none}"
fi

# Build the source table. Every Claude Code project dir is its own source (a
# workspace, a submodule opened as its own project, the home dir, ...), so none
# is silently unprotected. gptme and codex are added at fixed, harness-defined
# paths. Missing dirs stay in the table and are skipped, not alarmed.
# shellcheck disable=SC2034  # consumed by the scripts that source this file
TRAJECTORY_SOURCES=()
if [[ -d "$HOME/.claude/projects" ]]; then
    while IFS= read -r _proj; do
        [[ -z "$_proj" ]] && continue
        _name="claude-code:$(basename "$_proj")"
        [[ "$_proj" == "$CC_PROJECT_DIR" ]] && _name="claude-code"
        TRAJECTORY_SOURCES+=("$_name|$_proj|$TRAJECTORY_BACKUP_ROOT/$(basename "$_proj")|file:*.jsonl")
    done < <(find "$HOME/.claude/projects" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort)
fi
# gptme conversation dirs (each holds a conversation.jsonl + workspace symlink).
TRAJECTORY_SOURCES+=("gptme|$HOME/.local/share/gptme/logs|$TRAJECTORY_BACKUP_ROOT/gptme|dir:*")
# codex sessions live in a date tree; listed even if absent so the first is kept.
TRAJECTORY_SOURCES+=("codex|$HOME/.codex/sessions|$TRAJECTORY_BACKUP_ROOT/codex|rfile:*.jsonl")

# Extra harnesses from the environment (newline-separated name|src|dest|spec).
if [[ -n "${TRAJECTORY_EXTRA_SOURCES:-}" ]]; then
    while IFS= read -r _extra; do
        [[ -z "$_extra" ]] && continue
        TRAJECTORY_SOURCES+=("$_extra")
    done <<< "$TRAJECTORY_EXTRA_SOURCES"
fi

# Print one line per trajectory unit in a source dir, per its unit spec.
# Usage: trajectory_units <dir> <unit spec>
trajectory_units() {
    local dir="$1" spec="$2"
    local kind="${spec%%:*}" glob="${spec#*:}"
    [[ -d "$dir" ]] || return 0
    case "$kind" in
        file)  find "$dir" -maxdepth 1 -type f -name "$glob" 2>/dev/null ;;
        dir)   find "$dir" -maxdepth 1 -mindepth 1 -type d -name "$glob" 2>/dev/null ;;
        rfile) find "$dir" -type f -name "$glob" 2>/dev/null ;;
        *)     echo "trajectory_units: unknown unit kind '$kind'" >&2; return 1 ;;
    esac
}

# Null-delimited variant of trajectory_units — safe for paths with spaces,
# quotes, and backslashes (Claude Code project paths inherit these from the
# workspace name). Pipe to xargs -0 or while IFS= read -r -d '' for safe use.
trajectory_units_0() {
    local dir="$1" spec="$2"
    local kind="${spec%%:*}" glob="${spec#*:}"
    [[ -d "$dir" ]] || return 0
    case "$kind" in
        file)  find "$dir" -maxdepth 1 -type f -name "$glob" -print0 2>/dev/null ;;
        dir)   find "$dir" -maxdepth 1 -mindepth 1 -type d -name "$glob" -print0 2>/dev/null ;;
        rfile) find "$dir" -type f -name "$glob" -print0 2>/dev/null ;;
        *)     echo "trajectory_units_0: unknown unit kind '$kind'" >&2; return 1 ;;
    esac
}

# Count trajectory units in a source dir. Usage: trajectory_count <dir> <spec>
trajectory_count() {
    trajectory_units "$1" "$2" | wc -l
}

# Newest unit mtime (epoch seconds), or empty if none.
# For dir-type harnesses, derives freshness from content inside each trajectory
# dir — the dir mtime itself does not update when a resumed conversation appends
# to conversation.jsonl without touching the parent directory.
# Usage: trajectory_newest_mtime <dir> <spec>
trajectory_newest_mtime() {
    local dir="$1" spec="$2"
    local kind="${spec%%:*}"
    if [[ "$kind" == "dir" ]]; then
        {
            while IFS= read -r -d '' traj_dir; do
                find "$traj_dir" -type f -print0 2>/dev/null
            done < <(trajectory_units_0 "$dir" "$spec")
        } | xargs -0 -r stat -c %Y 2>/dev/null | sort -rn | head -1
    else
        trajectory_units_0 "$dir" "$spec" | xargs -0 -r stat -c %Y 2>/dev/null | sort -rn | head -1
    fi
}

# Oldest unit mtime (epoch seconds), or empty if none.
# Usage: trajectory_oldest_mtime <dir> <spec>
trajectory_oldest_mtime() {
    trajectory_units_0 "$1" "$2" | xargs -0 -r stat -c %Y 2>/dev/null | sort -n | head -1
}

# Count units last modified before <cutoff epoch>. Used for backup-coverage
# checks: units younger than the cutoff may legitimately post-date the last
# backup run, so they are not yet expected to be present in the backup.
# Usage: trajectory_count_older_than <dir> <spec> <cutoff epoch>
trajectory_count_older_than() {
    trajectory_units_0 "$1" "$2" | xargs -0 -r stat -c %Y 2>/dev/null \
        | awk -v cutoff="$3" '$1 < cutoff' | wc -l
}

# Count settled live units (older than cutoff) that are present in the backup.
# Checks identity by name, not just aggregate counts — retained-but-deleted
# entries in the backup cannot mask newly missing trajectories.
# Usage: trajectory_count_covered <src> <dest> <spec> <cutoff epoch>
trajectory_count_covered() {
    local src="$1" dest="$2" spec="$3" cutoff="$4"
    local kind="${spec%%:*}"
    local count=0
    while IFS= read -r -d '' unit; do
        local mtime
        mtime=$(stat -c %Y "$unit" 2>/dev/null) || continue
        if [[ "$mtime" -ge "$cutoff" ]]; then continue; fi
        case "$kind" in
            file)
                if [[ -e "$dest/$(basename "$unit")" ]]; then count=$(( count + 1 )); fi ;;
            dir)
                local bdir
                bdir="$dest/$(basename "$unit")"
                # Existence alone is not enough: a stopped backup can leave an
                # empty directory placeholder. Verify at least one file is
                # present so a hollow backup dir is not counted as covered.
                if [[ -d "$bdir" ]] && [[ -n "$(find "$bdir" -maxdepth 1 -type f -print -quit 2>/dev/null)" ]]; then
                    count=$(( count + 1 ))
                fi ;;
            rfile)
                local rel="${unit#$src/}"
                if [[ -e "$dest/$rel" ]]; then count=$(( count + 1 )); fi ;;
        esac
    done < <(trajectory_units_0 "$src" "$spec")
    echo "$count"
}
