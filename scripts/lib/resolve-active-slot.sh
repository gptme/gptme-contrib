#!/usr/bin/env bash
# shellcheck disable=SC2317  # sourced library; functions run in caller contexts
# Shared resolution of the active Claude Code subscription slot.
#
# The live credential path is normally a symlink:
#   ~/.claude/.credentials.json -> .credentials.json.{slot}
# so the slot name is the final dot-suffix of the link target.
#
# During an operator `/login`, Claude Code replaces that symlink with a REGULAR
# FILE. `readlink` then returns empty and the slot is genuinely unknowable from
# the filesystem alone. Callers must treat "unknown" as "do not write a
# fleet-global block marker": a 401 inside the login window is expected
# collateral of the credential swap, not evidence of stale stored auth.
#
# Writing an unscoped marker in that window blocks every slot for up to the
# marker TTL — at exactly the moment a freshly re-authed quota is coming online.
#
# Single-slot mode (one account per agent/container) is the common case for a
# solo or forked agent that does no slot juggling. Enable it with
# `AUTH_SINGLE_SLOT=1` and, optionally, `AUTH_SLOT_NAME=<name>`. The account
# identity is then configuration, not something inferred from the credential
# file shape — so a regular live credential (and even a temporarily missing one
# during recovery) still maps auth/rate-limit markers to the one account owned
# by this agent. With `AUTH_SINGLE_SLOT=1` and no name set, the slot resolves to
# "unknown" (the safe "do not write a fleet-global marker" state); set
# AUTH_SLOT_NAME to get scoped markers.
#
# Back-compat: the legacy `BOB_SINGLE_SLOT` / `BOB_SINGLE_SLOT_NAME` env vars are
# still honoured (checked when the canonical vars are unset). The legacy default
# slot name was "bob" when BOB_SINGLE_SLOT drove the branch with no name; that is
# preserved so an existing Bob-style deployment keeps working without an edit.

# Echo the active slot name, or the literal string "unknown" when it cannot be
# resolved (regular-file drift during /login, or a missing credential path).
resolve_active_slot() {
    local home=${1:-$HOME}
    local link slot

    # This branch deliberately runs before readlink so single-slot mode does not
    # depend on the credential file's on-disk shape.
    if [ "${AUTH_SINGLE_SLOT:-0}" = "1" ] || [ "${BOB_SINGLE_SLOT:-0}" = "1" ]; then
        # Canonical AUTH_SLOT_NAME wins; legacy BOB_SINGLE_SLOT_NAME is the fallback.
        slot="${AUTH_SLOT_NAME:-${BOB_SINGLE_SLOT_NAME:-}}"
        if [ -z "$slot" ]; then
            # Legacy BOB_SINGLE_SLOT with no name defaulted to "bob"; the canonical
            # AUTH_SINGLE_SLOT with no name resolves to "unknown" (safe no-marker).
            if [ "${BOB_SINGLE_SLOT:-0}" = "1" ]; then
                slot="bob"
            else
                echo "unknown"
                return 0
            fi
        fi
        case "$slot" in
            *[!A-Za-z0-9_-]*) echo "unknown"; return 0 ;;
            *) echo "$slot"; return 0 ;;
        esac
    fi

    link="$(readlink "$home/.claude/.credentials.json" 2>/dev/null || true)"
    slot="${link##*.}"

    # Empty link  => not a symlink (regular-file drift) or path missing.
    # slot == link => target had no dot-suffix to strip, so it names no slot.
    if [ -z "$slot" ] || [ "$slot" = "$link" ]; then
        echo "unknown"
        return 0
    fi

    echo "$slot"
}
