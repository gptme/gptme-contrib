#!/usr/bin/env bash
# Autoresearch eval scorer — runs gptme practical eval suite, returns scalar pass rate.
#
# Usage:
#   ./scripts/autoresearch/eval-score.sh [--suite SUITE] [--eval-model MODEL] [--gptme-dir DIR]
#
# Output (stdout):
#   <pass_rate>   e.g. "0.750"
#
# Exit codes:
#   0 — scoring succeeded (pass_rate on stdout)
#   1 — eval failed / no results

set -euo pipefail

GPTME_DIR="${GPTME_DIR:-$(pwd)}"
ARTIFACT_DIR="${ARTIFACT_DIR:-${GPTME_DIR}}"
SUITE="${SUITE:-practical5}"
EVAL_MODEL="${EVAL_MODEL:-}"
EVAL_MODEL_CANDIDATES="${EVAL_MODEL_CANDIDATES:-openai-subscription/gpt-5.4,openrouter/anthropic/claude-sonnet-4-6}"
# EVAL_CMD: optional custom eval command for non-gptme experiments.
# When set, runs in ARTIFACT_DIR with MODEL env var injected; must output a float (0.0-1.0) to stdout.
# When unset, uses gptme eval (suite + CSV scoring).
EVAL_CMD="${EVAL_CMD:-}"
TIMEOUT="${TIMEOUT:-90}"
PARALLEL="${PARALLEL:-3}"
TMPDIR_BASE="/tmp/autoresearch-eval"
LOG_OUTPUT=""  # Optional: if set, write eval log path to this file
MODEL_OUTPUT=""  # Optional: if set, write the successful model to this file
CSV_OUTPUT=""  # Optional: if set, write the latest eval_results.csv path to this file

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --suite) SUITE="$2"; shift 2 ;;
        --eval-model) EVAL_MODEL="$2"; shift 2 ;;
        --gptme-dir) GPTME_DIR="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --log-output) LOG_OUTPUT="$2"; shift 2 ;;
        --model-output) MODEL_OUTPUT="$2"; shift 2 ;;
        --csv-output) CSV_OUTPUT="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "${value}"
}

write_optional_file() {
    local path="$1"
    local value="$2"
    if [[ -n "${path}" ]]; then
        printf '%s\n' "${value}" > "${path}"
    fi
}

find_latest_csv() {
    local newer_than="$1"
    local search_roots=(
        "${GPTME_DIR}/eval_results"
        "${ARTIFACT_DIR}/eval_results"
    )
    local root=""

    for root in "${search_roots[@]}"; do
        if [[ -d "${root}" ]]; then
            find "${root}" -name 'eval_results.csv' -newer "${newer_than}" 2>/dev/null
        fi
    done | sort | tail -1
}

score_csv() {
    local csv_path="$1"
    python3 - "${csv_path}" << 'PYEOF'
import csv, sys

path = sys.argv[1]
rows = list(csv.DictReader(open(path)))
if not rows:
    print("ERROR: Empty results CSV", file=sys.stderr)
    sys.exit(1)

# Column name is "Passed" (capital P) in gptme eval CSV output
passed = sum(1 for r in rows if r.get('Passed', r.get('passed', '')).lower() == 'true')
total = len(rows)
rate = passed / total if total > 0 else 0.0
print(f"{rate:.3f}")
PYEOF
}

if [[ -n "${EVAL_MODEL}" ]]; then
    CANDIDATES=("${EVAL_MODEL}")
else
    IFS=',' read -r -a CANDIDATES <<< "${EVAL_MODEL_CANDIDATES}"
fi

# Generic eval path: EVAL_CMD is set — run it directly in ARTIFACT_DIR.
# The command receives MODEL as an env var and must output a float (0.0-1.0) to stdout.
# Model candidates are tried in order; first successful result wins.
if [[ -n "${EVAL_CMD}" ]]; then
    TMPDIR="${TMPDIR_BASE}/$(date +%s)-$$"
    mkdir -p "${TMPDIR}"

    for raw_model in "${CANDIDATES[@]}"; do
        model="$(trim "${raw_model}")"
        if [[ -z "${model}" ]]; then
            continue
        fi
        LOG="${TMPDIR}/eval-${model//\//_}.log"
        echo "Running generic eval (model: ${model}): ${EVAL_CMD}" >&2

        set +e
        result="$(cd "${ARTIFACT_DIR}" && MODEL="${model}" bash -c "${EVAL_CMD}" 2>"${LOG}")"
        eval_exit=$?
        set -e

        if [[ ${eval_exit} -eq 0 ]] && [[ "${result}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
            write_optional_file "${LOG_OUTPUT}" "${LOG}"
            write_optional_file "${MODEL_OUTPUT}" "${model}"
            # No CSV for generic evals — write empty sentinel if requested
            if [[ -n "${CSV_OUTPUT}" ]]; then
                printf '' > "${CSV_OUTPUT}"
            fi
            printf '%s\n' "${result}"
            exit 0
        fi

        echo "Generic eval failed (exit ${eval_exit}, result='${result}')" >&2
        if [[ -f "${LOG}" ]]; then
            tail -5 "${LOG}" >&2
        fi
    done

    echo "ERROR: Generic eval failed for all candidates." >&2
    exit 1
fi

# gptme eval path (default): run gptme eval suite, find CSV, score it.
LAST_LOG=""
for raw_model in "${CANDIDATES[@]}"; do
    model="$(trim "${raw_model}")"
    if [[ -z "${model}" ]]; then
        continue
    fi

    TMPDIR="${TMPDIR_BASE}/$(date +%s)-$$"
    mkdir -p "${TMPDIR}"
    LOG="${TMPDIR}/eval.log"
    LAST_LOG="${LOG}"

    echo "Running ${SUITE} with eval model ${model}" >&2

    cd "${GPTME_DIR}"
    set +e
    uv run python3 -m gptme.eval \
        -m "${model}" \
        -t "${TIMEOUT}" \
        -p "${PARALLEL}" \
        "${SUITE}" > "${LOG}" 2>&1
    EVAL_EXIT=$?
    set -e

    LATEST_CSV="$(find_latest_csv "${TMPDIR}")"

    if [[ -n "${LATEST_CSV}" ]]; then
        write_optional_file "${LOG_OUTPUT}" "${LOG}"
        write_optional_file "${MODEL_OUTPUT}" "${model}"
        write_optional_file "${CSV_OUTPUT}" "${LATEST_CSV}"
        score_csv "${LATEST_CSV}"
        exit 0
    fi

    echo "Eval failed with model ${model} (exit ${EVAL_EXIT})" >&2
done

echo "ERROR: No eval results CSV found after running ${SUITE}." >&2
if [[ -n "${LAST_LOG}" && -f "${LAST_LOG}" ]]; then
    cat "${LAST_LOG}" >&2
fi

exit 1
