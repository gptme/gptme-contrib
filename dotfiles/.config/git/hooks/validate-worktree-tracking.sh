#!/bin/bash
# Validate worktree has correct upstream tracking before push
# Fails if no upstream or upstream not on origin

set -e

# Get current branch
current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ -z "$current_branch" ]; then
    exit 0  # Not in a git repo
fi

# Skip check on detached HEAD (common in submodules, rebases, branch deletions)
if [ "$current_branch" = "HEAD" ]; then
    exit 0
fi

# Skip check on master/main branches (usually tracked correctly)
if [ "$current_branch" = "master" ] || [ "$current_branch" = "main" ]; then
    exit 0
fi

# Read push refspecs from stdin early (stdin can only be read once).
# Pre-push hook receives: local_ref local_sha remote_ref remote_sha
ZERO="0000000000000000000000000000000000000000"
push_remote_refs=()
all_deletions=true
any_pushed=false
# The `|| [ -n "$_local_ref" ]` handles a final line with no trailing newline
# (e.g. stdin piped from a Python subprocess or `printf` without `\n`). Without
# it, `read` returns non-zero on that last line and the loop drops it — which
# silently skips validation for the only ref being pushed rather than failing
# loudly. pre-push uses the same guard when it reads git's stdin.
while read -r _local_ref local_sha remote_ref _remote_sha || [ -n "${_local_ref:-}" ]; do
    # Skip blank lines. An up-to-date push ("Everything up-to-date") makes git
    # invoke the hook with empty stdin; the caller may forward a single empty
    # line. Treating that as a real ref produced a false "no upstream" error.
    [ -z "$local_sha$remote_ref" ] && continue
    push_remote_refs+=("$remote_ref")
    any_pushed=true
    if [ "$local_sha" != "$ZERO" ]; then
        all_deletions=false
    fi
done

# Nothing actually being pushed (empty stdin / up-to-date) — nothing to validate.
if [ "$any_pushed" = false ]; then
    exit 0
fi

# Skip if push contains only branch deletions (e.g. `git push origin --delete BRANCH`).
# The current branch's upstream is irrelevant when we're not pushing it.
if [ "$all_deletions" = true ]; then
    exit 0
fi

# Get upstream tracking branch
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "")

if [ -z "$upstream" ]; then
    # No upstream tracking branch.
    #
    # The hazard this guard exists for is the `git worktree add -b feat origin/master`
    # + `push.default=upstream` trap, where a *plain* `git push` silently lands on
    # master. That hazard is entirely about the push DESTINATION — and git resolves
    # refspecs before invoking this hook, so every destination is already on stdin.
    # Checking the destination is therefore both necessary and sufficient;
    # additionally demanding an upstream adds no protection over that check.
    #
    # It does produce false blocks. An explicit refspec cannot go somewhere
    # unintended, because the destination is written out in the command:
    #     git push origin pr-3468:fix-3440-server-e2e
    # The old rule only accepted destinations matching the *current branch name*,
    # so it rejected every PR worker that checks a PR out under a local name
    # differing from the PR's remote branch — teaching `--no-verify` and local
    # branch renames as the workaround, which is the habit this hook exists to
    # prevent. See gptme/gptme#3468 for the case that surfaced it.
    #
    # So: block only when a destination actually is master/main.
    pushing_to_default=false
    for remote_ref in "${push_remote_refs[@]}"; do
        if [ "$remote_ref" = "refs/heads/master" ] || [ "$remote_ref" = "refs/heads/main" ]; then
            pushing_to_default=true
            break
        fi
    done

    if [ "$pushing_to_default" = true ]; then
        echo "❌ Error: Branch '$current_branch' has no upstream tracking branch"
        echo "   and this push targets master/main — the wrong-destination case"
        echo "   this guard exists to catch."
        echo "   Fix with: git branch --set-upstream-to=origin/$current_branch"
        echo ""
        exit 1
    fi

    # Never downgrade a safety check silently — name what was allowed, so an
    # unexpected destination is visible in the push output rather than implied.
    echo "ℹ️  No upstream set - allowing push to explicit destination(s): ${push_remote_refs[*]}"
    exit 0
fi

# Verify upstream is on origin (not a local branch)
if [[ ! "$upstream" =~ ^origin/ ]]; then
    echo "⚠️  Warning: Branch '$current_branch' upstream is '$upstream' (not on origin)"
    echo "   Expected: origin/$current_branch"
    echo "   Fix with: git branch --set-upstream-to=origin/$current_branch"
    echo ""
    # Don't fail, just warn - might be intentional
fi

# CRITICAL: Block feature branches that track origin/master or origin/main.
# This happens when git worktree add -b <branch> origin/master sets the upstream
# to origin/master. A subsequent `git push` then pushes to master, bypassing PR review.
#
# Only block when the actual push destination is master/main — explicit pushes like
# `git push origin feature:feature` are safe even with a misconfigured upstream.
if [ "$upstream" = "origin/master" ] || [ "$upstream" = "origin/main" ]; then
    # Check if any push destination is master or main
    pushing_to_default=false
    for remote_ref in "${push_remote_refs[@]}"; do
        if [ "$remote_ref" = "refs/heads/master" ] || [ "$remote_ref" = "refs/heads/main" ]; then
            pushing_to_default=true
            break
        fi
    done
    if [ "$pushing_to_default" = true ]; then
        echo "🚫 ERROR: Branch '$current_branch' tracks '$upstream'!"
        echo ""
        echo "   This will push your feature branch directly to master/main."
        echo "   This is almost always a mistake from worktree creation."
        echo ""
        echo "   Fix with:"
        echo "     git branch --unset-upstream"
        echo "     git push -u origin $current_branch"
        echo ""
        exit 1
    fi
fi

exit 0
