#!/usr/bin/env bash
# Mass-deletion guard for git hooks.
# Blocks operations that would delete a dangerous number of tracked files.
# Shared by pre-commit and pre-push hooks.
#
# Usage:
#   source scripts/git/guard-mass-delete.sh
#   guard_mass_delete_staged        # For pre-commit: checks staged deletions
#   guard_mass_delete_commit <sha>  # For pre-push: checks a specific commit
#
# Bypass: ALLOW_MASS_DELETE=1 git commit ...
# Thresholds: MASS_DELETE_THRESHOLD (default 50)
#
# Why this exists: An agent accidentally staged 11,632 file deletions (1.3M lines)
# due to a prek stash/restore bug that corrupted the working tree. The inline
# "50% of tracked files" check in the pre-commit hook didn't catch it because
# the threshold was too high. This library uses a low absolute threshold (50 files)
# and filters submodule paths to avoid false positives.

# Submodule paths to exclude (prek stash/restore can corrupt submodule entries,
# causing phantom "mass deletions" of entire submodule trees).
_guard_get_submodule_filter() {
    local repo_root="${1:-.}"
    local submodule_paths
    submodule_paths=$(git config --file "$repo_root/.gitmodules" --get-regexp path 2>/dev/null | awk '{print $2}' || true)
    if [ -n "$submodule_paths" ]; then
        echo "$submodule_paths" | sed 's|^|^|; s|$|/|' | paste -sd'|'
    fi
}

# guard_mass_delete_staged: Check staged deletions (for pre-commit hook).
# Returns 0 on pass, exits 1 on block.
guard_mass_delete_staged() {
    # Allow explicit bypass
    if [ "${ALLOW_MASS_DELETE:-0}" = "1" ]; then
        return 0
    fi

    local threshold="${MASS_DELETE_THRESHOLD:-50}"
    local repo_root
    repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0

    local deleted_files
    deleted_files=$(git diff --cached --name-only --diff-filter=D)

    # Filter out submodule paths
    local submodule_filter
    submodule_filter=$(_guard_get_submodule_filter "$repo_root")
    if [ -n "$submodule_filter" ]; then
        deleted_files=$(echo "$deleted_files" | grep -Ev "$submodule_filter" || true)
    fi

    local deleted_count
    deleted_count=$(echo "$deleted_files" | grep -c '^' || true)

    if [ "$deleted_count" -gt "$threshold" ]; then
        local total_tracked
        total_tracked=$(git ls-files | wc -l)
        local delete_pct=0
        if [ "$total_tracked" -gt 0 ]; then
            delete_pct=$((deleted_count * 100 / total_tracked))
        fi

        echo "" >&2
        echo "============================================================" >&2
        echo "  MASS DELETION BLOCKED ($deleted_count files, ${delete_pct}% of repo)" >&2
        echo "============================================================" >&2
        echo "" >&2
        echo "  This commit would delete $deleted_count of $total_tracked tracked files." >&2
        echo "  This is almost certainly accidental." >&2
        echo "" >&2
        echo "  Sample deleted files:" >&2
        echo "$deleted_files" | head -10 | sed 's/^/    /' >&2
        if [ "$deleted_count" -gt 10 ]; then
            echo "    ... and $((deleted_count - 10)) more" >&2
        fi
        echo "" >&2
        echo "  To recover:" >&2
        echo "    git reset HEAD              # unstage everything" >&2
        echo "    git checkout -- .           # restore working tree" >&2
        echo "" >&2
        echo "  If intentional: ALLOW_MASS_DELETE=1 git commit ..." >&2
        echo "============================================================" >&2
        exit 1
    fi

    return 0
}

# guard_mass_delete_commit: Check a specific commit for mass deletions (for pre-push hook).
# Usage: guard_mass_delete_commit <commit_sha>
# Returns 0 on pass, exits 1 on block.
guard_mass_delete_commit() {
    # Allow explicit bypass
    if [ "${ALLOW_MASS_DELETE:-0}" = "1" ]; then
        return 0
    fi

    local commit="$1"
    local threshold="${MASS_DELETE_THRESHOLD:-50}"
    local repo_root
    repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || return 0

    local deleted_files
    deleted_files=$(git diff-tree --no-commit-id -r --diff-filter=D "$commit" 2>/dev/null | awk '{print $NF}')

    # Filter out submodule paths
    local submodule_filter
    submodule_filter=$(_guard_get_submodule_filter "$repo_root")
    if [ -n "$submodule_filter" ]; then
        deleted_files=$(echo "$deleted_files" | grep -Ev "$submodule_filter" || true)
    fi

    local deleted_count
    deleted_count=$(echo "$deleted_files" | grep -c '^' || true)

    if [ "$deleted_count" -gt "$threshold" ]; then
        local commit_msg
        commit_msg=$(git log -1 --format='%s' "$commit" 2>/dev/null || echo "unknown")
        local commit_short
        commit_short=$(git rev-parse --short "$commit" 2>/dev/null || echo "$commit")

        echo "" >&2
        echo "============================================================" >&2
        echo "  PUSH BLOCKED: commit $commit_short deletes $deleted_count files" >&2
        echo "============================================================" >&2
        echo "" >&2
        echo "  Commit: $commit_short $commit_msg" >&2
        echo "  This commit deletes $deleted_count tracked files." >&2
        echo "  Pushing this would be catastrophic." >&2
        echo "" >&2
        echo "  Sample deleted files:" >&2
        echo "$deleted_files" | head -10 | sed 's/^/    /' >&2
        if [ "$deleted_count" -gt 10 ]; then
            echo "    ... and $((deleted_count - 10)) more" >&2
        fi
        echo "" >&2
        echo "  To fix:" >&2
        echo "    git reset --soft HEAD~1     # undo the commit, keep changes staged" >&2
        echo "    git reset HEAD              # unstage everything" >&2
        echo "    git checkout -- .           # restore working tree" >&2
        echo "" >&2
        echo "  If intentional: ALLOW_MASS_DELETE=1 git push ..." >&2
        echo "============================================================" >&2
        exit 1
    fi

    return 0
}
