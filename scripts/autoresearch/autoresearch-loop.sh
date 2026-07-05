#!/usr/bin/env bash
# Budget-gated autoresearch outer loop.
#
# Runs like a ralph-loop: no gaps between iterations while budget is available.
# When the daily iteration budget is exhausted, sleeps until midnight UTC and resumes.
# If total_budget is set in the experiment config, the loop exits when the all-time
# total is reached (experiment is considered complete — not just paused until tomorrow).
#
# Usage:
#   autoresearch-loop.sh EXPERIMENT
#   Or set EXPERIMENT env var before calling.
#
# Environment overrides:
#   CONFIG_DIR   Directory containing YAML experiment configs (default: ./experiments/)
#   ARTIFACT_DIR Directory for build artifacts (default: current working directory)
#
# As a persistent service (Type=simple), this loops forever. systemd restart policy
# handles crashes. The experiment name is passed as the first argument (or via env).

set -euo pipefail

EXPERIMENT="${1:-${EXPERIMENT:-}}"
if [[ -z "${EXPERIMENT}" ]]; then
    echo "ERROR: EXPERIMENT name required. Usage: $(basename "$0") EXPERIMENT" >&2
    echo "  Example: $(basename "$0") gptme-eval" >&2
    echo "  See experiments/ for available configs." >&2
    exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "${SCRIPT_DIR}/../.." rev-parse --show-toplevel)"
STATE_DIR="${REPO_ROOT}/state/autoresearch"
# CONFIG_DIR can be overridden via env var to support thin wrapper deployments
# where experiment configs live outside the gptme-contrib repo (e.g. agent workspaces)
CONFIG_DIR="${CONFIG_DIR:-${SCRIPT_DIR}/experiments}"
BUDGET_DIR="${STATE_DIR}/budget"
GLOBAL_CONFIG_FILE="${SCRIPT_DIR}/config.yaml"

CONFIG_FILE="${CONFIG_DIR}/${EXPERIMENT}.yaml"
if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "ERROR: Experiment config not found: ${CONFIG_FILE}" >&2
    exit 1
fi

# Parse simple key: value YAML
get_config() {
    local key="$1"
    local default="${2:-}"
    local val
    # Strip inline YAML comments (e.g. "0.05  # explanation") before xargs
    val="$(grep "^${key}:" "${CONFIG_FILE}" | head -1 | awk '{$1=""; print $0}' | sed 's/[[:space:]]*#.*//' | xargs)"
    echo "${val:-${default}}"
}

SUITE="$(get_config suite)"
PERIOD_BUDGET="$(get_config period_budget 15)"
TOTAL_BUDGET="$(get_config total_budget "")"  # Optional: exit when all-time total reached
MODEL_CANDIDATES="$(get_config model_candidates)"
EVAL_MODEL_CANDIDATES="$(get_config eval_model_candidates)"
ENABLED="$(get_config enabled true)"

# General autoresearch fields — support any artifact, eval, harness, and program spec.
# These replace gptme-specific hardcoded defaults and allow running arbitrary experiments.
ARTIFACT_DIR="${ARTIFACT_DIR:-$(get_config artifact_dir "$(pwd)")}"
AGENT_HARNESS="$(get_config agent_harness gptme)"
EVAL_CMD="$(get_config eval_cmd "")"           # Optional: custom eval command (stdout → float)
PROGRAM_SPEC="$(get_config program_spec "")"   # Optional: path to agent program spec
PUBLISH_THRESHOLD="$(get_config publish_threshold 0.05)"  # Min score delta to auto-create PR
DIAGNOSIS_STUCK_ITERS="$(get_config diagnosis_after_stuck_iters 5)"  # Self-diagnose after N consecutive rejections
USE_WORKTREE="$(get_config use_worktree "1")"  # Set false for artifacts with broken symlinks in worktrees
SATURATION_THRESHOLD="$(get_config saturation_threshold 1.0)"  # Auto-disable when baseline >= this
SATURATION_MAX_CONSECUTIVE="$(get_config saturation_max_consecutive 2)"  # Consecutive saturated runs before auto-disable

if [[ "${ENABLED}" == "false" ]]; then
    echo "Experiment ${EXPERIMENT} is disabled — exiting."
    exit 0
fi

# Read global budget config (shared across all experiments)
# Falls back to unlimited if no global config exists.
get_global_config() {
    local key="$1"
    local default="${2:-}"
    if [[ ! -f "${GLOBAL_CONFIG_FILE}" ]]; then
        echo "${default}"
        return
    fi
    local val
    val="$(grep "^${key}:" "${GLOBAL_CONFIG_FILE}" | head -1 | awk '{$1=""; print $0}' | sed 's/[[:space:]]*#.*//' | xargs)"
    echo "${val:-${default}}"
}
GLOBAL_DAILY_BUDGET="$(get_global_config global_daily_budget "")"

mkdir -p "${BUDGET_DIR}"

# Returns remaining budget for today (UTC) — per-experiment
get_remaining_budget() {
    local today
    today="$(date -u +%Y-%m-%d)"
    local budget_file="${BUDGET_DIR}/${EXPERIMENT}-${today}.json"

    if [[ ! -f "${budget_file}" ]]; then
        echo "${PERIOD_BUDGET}"
        return
    fi

    python3 -c "
import json
with open('${budget_file}') as f:
    d = json.load(f)
used = d.get('iterations', 0)
print(max(0, ${PERIOD_BUDGET} - used))
"
}

# Consume N iterations from today's budget
consume_budget() {
    local count="$1"
    local today
    today="$(date -u +%Y-%m-%d)"
    local budget_file="${BUDGET_DIR}/${EXPERIMENT}-${today}.json"

    python3 -c "
import json, os
f = '${budget_file}'
d = json.load(open(f)) if os.path.exists(f) else {'iterations': 0}
d['iterations'] = d.get('iterations', 0) + ${count}
json.dump(d, open(f, 'w'))
print(f'Budget: {d[\"iterations\"]}/${PERIOD_BUDGET} iterations used today')
"
}

# Returns remaining global budget for today (UTC).
# If no global config, returns a large number (unlimited).
get_remaining_global_budget() {
    if [[ -z "${GLOBAL_DAILY_BUDGET}" ]]; then
        echo "99999"
        return
    fi
    local today
    today="$(date -u +%Y-%m-%d)"
    local global_file="${BUDGET_DIR}/global-${today}.json"
    python3 -c "
import json, os
f = '${global_file}'
d = json.load(open(f)) if os.path.exists(f) else {'iterations': 0}
used = d.get('iterations', 0)
print(max(0, ${GLOBAL_DAILY_BUDGET} - used))
"
}

# Consume N iterations from global budget (no-op if unlimited)
consume_global_budget() {
    local count="$1"
    if [[ -z "${GLOBAL_DAILY_BUDGET}" ]]; then
        return
    fi
    local today
    today="$(date -u +%Y-%m-%d)"
    local global_file="${BUDGET_DIR}/global-${today}.json"
    python3 -c "
import json, os
f = '${global_file}'
d = json.load(open(f)) if os.path.exists(f) else {'iterations': 0, 'limit': ${GLOBAL_DAILY_BUDGET}}
d['iterations'] = d.get('iterations', 0) + ${count}
d['limit'] = ${GLOBAL_DAILY_BUDGET}
json.dump(d, open(f, 'w'))
print(f'Global budget: {d[\"iterations\"]}/${GLOBAL_DAILY_BUDGET} iterations used today')
"
}

# Returns all-time total iterations consumed for this experiment (across all days).
# This is the "total budget" counter that counts towards experiment completion.
get_total_iterations() {
    local total_file="${BUDGET_DIR}/${EXPERIMENT}-all-time.json"
    if [[ ! -f "${total_file}" ]]; then
        echo "0"
        return
    fi
    python3 -c "
import json
with open('${total_file}') as f:
    d = json.load(f)
print(d.get('total_iterations', 0))
"
}

# Consume N iterations into the all-time total counter.
# No-op if total_budget is not configured for this experiment.
consume_total_budget() {
    local count="$1"
    if [[ -z "${TOTAL_BUDGET}" ]]; then
        return
    fi
    local total_file="${BUDGET_DIR}/${EXPERIMENT}-all-time.json"
    python3 -c "
import json, os
f = '${total_file}'
d = json.load(open(f)) if os.path.exists(f) else {'total_iterations': 0, 'limit': ${TOTAL_BUDGET}}
d['total_iterations'] = d.get('total_iterations', 0) + ${count}
d['limit'] = ${TOTAL_BUDGET}
json.dump(d, open(f, 'w'))
print(f'Total budget: {d[\"total_iterations\"]}/${TOTAL_BUDGET} all-time iterations consumed')
"
}

# Sleep until midnight UTC + small buffer
sleep_until_next_period() {
    local now
    now="$(date -u +%s)"
    # Next midnight UTC
    local midnight
    midnight="$(python3 -c "
import datetime
import time
now = datetime.datetime.now(datetime.timezone.utc)
tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
print(int(tomorrow.timestamp()))
")"
    local sleep_secs=$(( midnight - now ))
    if [[ ${sleep_secs} -lt 1 ]]; then sleep_secs=60; fi
    echo "Daily budget exhausted for ${EXPERIMENT}. Sleeping ${sleep_secs}s until next period..."
    sleep "${sleep_secs}"
}

# Filter model candidates by quota availability.
# Calls check-quota.py and removes candidates whose underlying quota is exhausted.
# Fails open: returns original candidates on any error to avoid blocking runs.
filter_model_candidates_by_quota() {
    local candidates="$1"
    local harness="${2:-gptme}"

    python3 - "${candidates}" "${harness}" "${REPO_ROOT}/scripts/check-quota.py" <<'PYEOF'
import sys
import json
import subprocess

candidates_str = sys.argv[1]
harness = sys.argv[2]
quota_script = sys.argv[3]
candidates = [c.strip() for c in candidates_str.split(',') if c.strip()]

try:
    result = subprocess.run(
        ['uv', 'run', 'python3', quota_script],
        capture_output=True, text=True, timeout=20,
    )
    # returncode 1 means "no backends available" — still valid JSON output
    if result.returncode not in (0, 1):
        print(candidates_str, end='')
        sys.exit(0)
    quota_data = json.loads(result.stdout)
except Exception:
    print(candidates_str, end='')
    sys.exit(0)

# Build availability index: (backend, model) -> bool
availability: dict[tuple[str, str], bool] = {}
for b in quota_data.get('backends', []):
    availability[(b['backend'], b['model'])] = b.get('available', True)

def is_available(candidate: str, harness: str) -> bool:
    if harness in ('claude-code', 'cc'):
        # Short name (sonnet, opus) or full model ID — extract short key
        short = candidate.split('/')[-1]
        short = short.split('-')[0].lower() if '-' in short else short.lower()
        for key in [candidate, short]:
            avail = availability.get(('claude-code', key))
            if avail is not None:
                return avail
        return True  # Unknown = assume available
    else:
        # gptme harness
        if candidate.startswith('openai-subscription/'):
            # ChatGPT subscription models share gptme:gpt-5.4 quota
            avail = availability.get(('gptme', 'gpt-5.4'))
            return avail if avail is not None else True
        # openrouter/* and others: check openrouter availability
        # OR models share a single API key — any one's status represents all
        or_quota = next(
            (v for k, v in availability.items() if k[0] == 'gptme' and k[1] not in ('gpt-5.4',)),
            None
        )
        return or_quota if or_quota is not None else True

filtered = [c for c in candidates if is_available(c, harness)]
if not filtered:
    # All filtered out — return original to avoid fully blocking runs
    print(candidates_str, end='')
else:
    if filtered != candidates:
        skipped = [c for c in candidates if c not in filtered]
        print(f'Quota filter: skipping {skipped} (quota exhausted)', file=sys.stderr)
    print(','.join(filtered), end='')
PYEOF
}

echo "=== Autoresearch loop starting ==="
echo "Experiment: ${EXPERIMENT}"
echo "Suite: ${SUITE}"
echo "Per-experiment daily budget: ${PERIOD_BUDGET} iterations"
if [[ -n "${GLOBAL_DAILY_BUDGET}" ]]; then
    echo "Global daily budget: ${GLOBAL_DAILY_BUDGET} iterations (shared across all experiments)"
else
    echo "Global daily budget: unlimited (no config.yaml)"
fi
if [[ -n "${TOTAL_BUDGET}" ]]; then
    total_used="$(get_total_iterations)"
    echo "Total budget: ${total_used}/${TOTAL_BUDGET} all-time iterations (experiment exits when reached)"
else
    echo "Total budget: unlimited (experiment runs indefinitely)"
fi
echo ""

# Saturation tracking: consecutive runs where baseline is already at ceiling
# Persisted to disk so service restarts don't reset the counter.
SATURATION_STATE_FILE="${STATE_DIR}/${EXPERIMENT}-consecutive-saturated.txt"
if [[ -f "${SATURATION_STATE_FILE}" ]]; then
    CONSECUTIVE_SATURATED="$(cat "${SATURATION_STATE_FILE}")"
else
    CONSECUTIVE_SATURATED=0
fi

# Main ralph-style loop: run continuously while budget available, sleep when exhausted.
# If total_budget is configured and reached, EXIT (experiment complete).
while true; do
    # Check total budget first — if exhausted, experiment is done (not just paused).
    if [[ -n "${TOTAL_BUDGET}" ]]; then
        total_used="$(get_total_iterations)"
        if [[ "${total_used}" -ge "${TOTAL_BUDGET}" ]]; then
            echo "Total budget exhausted for ${EXPERIMENT}: ${total_used}/${TOTAL_BUDGET} all-time iterations consumed."
            echo "Experiment complete — exiting service."
            exit 0
        fi
        total_remaining=$(( TOTAL_BUDGET - total_used ))
        echo "All-time total: ${total_used}/${TOTAL_BUDGET} (${total_remaining} remaining in lifetime budget)"
    fi

    remaining="$(get_remaining_budget)"
    global_remaining="$(get_remaining_global_budget)"
    # Use the tighter of the two budgets (and also total_remaining if applicable)
    effective_remaining="$(( remaining < global_remaining ? remaining : global_remaining ))"
    if [[ -n "${TOTAL_BUDGET}" ]]; then
        total_remaining=$(( TOTAL_BUDGET - $(get_total_iterations) ))
        effective_remaining="$(( effective_remaining < total_remaining ? effective_remaining : total_remaining ))"
    fi

    if [[ "${remaining}" -le 0 ]]; then
        echo "Per-experiment budget exhausted (${EXPERIMENT}: 0/${PERIOD_BUDGET})."
        sleep_until_next_period
        continue
    fi
    if [[ "${global_remaining}" -le 0 ]]; then
        echo "Global budget exhausted (${EXPERIMENT} has ${remaining} remaining, but global=0/${GLOBAL_DAILY_BUDGET})."
        sleep_until_next_period
        continue
    fi

    echo "Remaining budget: ${effective_remaining} (per-exp: ${remaining}/${PERIOD_BUDGET}, global: ${global_remaining}/${GLOBAL_DAILY_BUDGET:-unlimited})"

    # Filter model candidates by quota availability before each batch run.
    # This skips models we know are exhausted, saving time on predictable failures.
    EFFECTIVE_MODEL_CANDIDATES="$(filter_model_candidates_by_quota "${MODEL_CANDIDATES}" "${AGENT_HARNESS}")"
    EFFECTIVE_EVAL_CANDIDATES="$(filter_model_candidates_by_quota "${EVAL_MODEL_CANDIDATES}" "${AGENT_HARNESS}")"

    # Reserve budget BEFORE starting the loop — crash-safe reservation.
    # If the service is killed mid-run, the next restart sees the reservation and
    # won't double-run. The loop may exit early (model failures, etc.) but we still
    # consume the full effective amount conservatively to avoid runaway retries.
    consume_budget "${effective_remaining}"
    consume_global_budget "${effective_remaining}"
    consume_total_budget "${effective_remaining}"

    # Run the merge-reject loop with effective budget — no artificial gaps
    echo "--- Starting autoresearch run: ${effective_remaining} iterations ---"
    set +e
    SUITE="${SUITE}" \
    MAX_ITERATIONS="${effective_remaining}" \
    MODEL_CANDIDATES="${EFFECTIVE_MODEL_CANDIDATES}" \
    EVAL_MODEL_CANDIDATES="${EFFECTIVE_EVAL_CANDIDATES}" \
    ARTIFACT_DIR="${ARTIFACT_DIR}" \
    AGENT_HARNESS="${AGENT_HARNESS}" \
    EVAL_CMD="${EVAL_CMD}" \
    PROGRAM_SPEC="${PROGRAM_SPEC}" \
    EXPERIMENT_NAME="${EXPERIMENT}" \
    LOG_DIR="${STATE_DIR}" \
    PUBLISH_THRESHOLD="${PUBLISH_THRESHOLD}" \
    DIAGNOSIS_STUCK_ITERS="${DIAGNOSIS_STUCK_ITERS}" \
    USE_WORKTREE="${USE_WORKTREE}" \
    SATURATION_THRESHOLD="${SATURATION_THRESHOLD}" \
        "${SCRIPT_DIR}/merge-reject-loop.sh"
    exit_code=$?
    set -e

    if [[ ${exit_code} -eq 42 ]]; then
        # Saturated: baseline score already at ceiling
        CONSECUTIVE_SATURATED=$(( CONSECUTIVE_SATURATED + 1 ))
        echo "${CONSECUTIVE_SATURATED}" > "${SATURATION_STATE_FILE}"
        echo "Saturation detected (${CONSECUTIVE_SATURATED}/${SATURATION_MAX_CONSECUTIVE} consecutive)."
        if [[ "${CONSECUTIVE_SATURATED}" -ge "${SATURATION_MAX_CONSECUTIVE}" ]]; then
            echo "EXPERIMENT SATURATED: ${EXPERIMENT} hit baseline ceiling ${SATURATION_MAX_CONSECUTIVE} consecutive times."
            echo "Auto-disabling experiment config: ${CONFIG_FILE}"
            # Robustly disable: replace existing enabled line or append if absent
            if grep -q '^enabled:' "${CONFIG_FILE}"; then
                sed -i 's/^enabled:.*/enabled: false  # auto-disabled: saturated (baseline at ceiling)/' "${CONFIG_FILE}"
            else
                printf '\nenabled: false  # auto-disabled: saturated (baseline at ceiling)\n' >> "${CONFIG_FILE}"
            fi
            echo "Experiment ${EXPERIMENT} auto-disabled. Create a harder benchmark or adjust saturation_threshold."
            exit 0
        fi
        echo "Sleeping until next period (score may vary with different eval models)..."
        sleep_until_next_period
        continue
    elif [[ ${exit_code} -ne 0 ]]; then
        CONSECUTIVE_SATURATED=0
        echo "0" > "${SATURATION_STATE_FILE}"
        echo "merge-reject-loop.sh exited with code ${exit_code} — waiting 5 minutes before retry"
        sleep 300
    else
        CONSECUTIVE_SATURATED=0
        echo "0" > "${SATURATION_STATE_FILE}"
    fi
done
