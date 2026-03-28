#!/usr/bin/env bash
# Autoresearch merge-reject loop — iteratively improve gptme via eval-gated commits.
#
# Inspired by karpathy/autoresearch. The loop:
#   1. Agent proposes a code change to gptme
#   2. Run eval suite → get scalar score
#   3. If score improved: keep change (git commit)
#   4. If score regressed: discard change (git checkout)
#   5. Repeat for N iterations
#
# Usage:
#   ./scripts/autoresearch/merge-reject-loop.sh [--iterations N] [--suite SUITE]
#     [--model MODEL] [--model-candidates CSV] [--eval-model MODEL]
#     [--eval-model-candidates CSV] [--keep-worktree] [--tool-format FORMAT]
#
# Environment:
#   GPTME_DIR — path to gptme repo (default: current directory)
#   BRANCH    — branch to commit improvements to (default: autoresearch/eval-improvement)

set -euo pipefail

# ARTIFACT_DIR: the repo being improved. Backward-compat: falls back to GPTME_DIR env var.
ARTIFACT_DIR="${ARTIFACT_DIR:-${GPTME_DIR:-$(pwd)}}"
# Expose as GPTME_SOURCE_DIR/GPTME_DIR for backward compatibility with internal references.
GPTME_SOURCE_DIR="${ARTIFACT_DIR}"
GPTME_DIR="${GPTME_SOURCE_DIR}"
SUITE="${SUITE:-practical5}"
MODEL="${MODEL:-}"
# Model candidates: subscription models first (free), reliable OR fallbacks for when quota exhausted.
# Note: glm-5 via OpenRouter is unreliable (auth failures, wrong output format) — do not use.
MODEL_CANDIDATES="${MODEL_CANDIDATES:-openai-subscription/gpt-5.4,openrouter/anthropic/claude-sonnet-4-6}"
EVAL_MODEL="${EVAL_MODEL:-}"
EVAL_MODEL_CANDIDATES="${EVAL_MODEL_CANDIDATES:-openai-subscription/gpt-5.4,openrouter/anthropic/claude-sonnet-4-6}"
# EVAL_CMD: optional custom eval command. When set, eval-score.sh runs it instead of gptme eval.
# The command runs in ARTIFACT_DIR with MODEL env var set; must output a float to stdout.
EVAL_CMD="${EVAL_CMD:-}"
AGENT_TOOL_FORMAT="${AGENT_TOOL_FORMAT:-}"
# AGENT_HARNESS: which agent runner to use. Supported: gptme (default), claude-code.
AGENT_HARNESS="${AGENT_HARNESS:-gptme}"
MAX_ITERATIONS="${MAX_ITERATIONS:-20}"
BRANCH="${BRANCH:-autoresearch/eval-improvement}"
WORKTREE_ROOT="${WORKTREE_ROOT:-/tmp/worktrees}"
USE_WORKTREE="${USE_WORKTREE:-1}"
KEEP_WORKTREE="${KEEP_WORKTREE:-0}"
SESSION_TAG="${SESSION_TAG:-$(date +%Y%m%d-%H%M%S)-$$}"
# EXPERIMENT_NAME: used for worktree naming to allow concurrent experiments.
EXPERIMENT_NAME="${EXPERIMENT_NAME:-gptme}"
WORKTREE_NAME="${WORKTREE_NAME:-${EXPERIMENT_NAME}-autoresearch-${SESSION_TAG}}"
WORKTREE_BRANCH="${WORKTREE_BRANCH:-${BRANCH}-${SESSION_TAG}}"
PUSH_BRANCH="${PUSH_BRANCH:-${WORKTREE_BRANCH}}"
# PUBLISH_THRESHOLD: minimum score delta required to auto-create a PR.
# Set to "" or "0" to always create PRs on improvement. Set high (e.g. "0.1") to batch gains.
PUBLISH_THRESHOLD="${PUBLISH_THRESHOLD:-0.05}"
# DIAGNOSIS_STUCK_ITERS: run self-diagnosis after this many consecutive rejections.
# The diagnosis reads eval conversation logs and uses an LLM to identify root cause
# (infrastructure bug, local optimum, or wrong approach). If an infrastructure bug is
# found, the loop auto-files a GitHub issue and stops early.
DIAGNOSIS_STUCK_ITERS="${DIAGNOSIS_STUCK_ITERS:-5}"
# GITHUB_REPO: the repository where issues and PRs are filed.
# Override for experiments targeting a different repo.
GITHUB_REPO="${GITHUB_REPO:-gptme/gptme}"
WORKTREE_DIR=""
REPO_ROOT="$(git -C "$(dirname "$0")/../.." rev-parse --show-toplevel)"
LOG_DIR="${REPO_ROOT}/state/autoresearch"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# PROGRAM_SPEC: path to the agent's instruction file. Relative paths resolved from SCRIPT_DIR.
# Override via env var or experiment config's program_spec field.
if [[ -n "${PROGRAM_SPEC:-}" ]]; then
    if [[ "${PROGRAM_SPEC}" != /* ]]; then
        PROGRAM_SPEC="${REPO_ROOT}/${PROGRAM_SPEC}"
    fi
else
    PROGRAM_SPEC="${SCRIPT_DIR}/examples/gptme-eval-program.md"
fi

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --iterations) MAX_ITERATIONS="$2"; shift 2 ;;
        --suite) SUITE="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --model-candidates) MODEL_CANDIDATES="$2"; shift 2 ;;
        --eval-model) EVAL_MODEL="$2"; shift 2 ;;
        --eval-model-candidates) EVAL_MODEL_CANDIDATES="$2"; shift 2 ;;
        --tool-format) AGENT_TOOL_FORMAT="$2"; shift 2 ;;
        --keep-worktree) KEEP_WORKTREE=1; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "${value}"
}

sanitize_filename() {
    local value="$1"
    value="${value//\//--}"
    value="${value//:/-}"
    value="${value// /-}"
    value="${value//[^A-Za-z0-9._-]/_}"
    printf '%s' "${value}"
}

summarize_agent_failure() {
    local candidate="$1"
    local log_path="$2"
    python3 - "${candidate}" "${log_path}" << 'PYEOF'
import re
import sys
from pathlib import Path

candidate = sys.argv[1]
log_path = Path(sys.argv[2])
text = log_path.read_text(errors="replace") if log_path.exists() else ""
lines = [line.strip() for line in text.splitlines() if line.strip()]

patterns = [
    ("provider quota", r"(quota|credit balance|insufficient credits|payment required)"),
    ("rate limit", r"(rate limit|too many requests|429)"),
    ("auth failure", r"(authentication|api key|unauthorized|forbidden|401|403)"),
    ("tool syntax error", r"(no tool call detected|error executing tool|assertionerror)"),
    ("timeout", r"(timeout|timed out|timeout reached)"),
    ("context limit", r"(context length|maximum context|too many tokens)"),
]

reason = "unknown failure"
for label, pattern in patterns:
    if re.search(pattern, text, flags=re.I):
        reason = label
        break

snippet = ""
priority = [
    r"ERROR:.*",
    r"System: ❌.*",
    r".*AssertionError.*",
    r".*(quota|rate limit|authentication|unauthorized|forbidden|timeout).*",
]
for pattern in priority:
    for line in lines:
        if re.search(pattern, line, flags=re.I):
            snippet = line
            break
    if snippet:
        break

if not snippet and lines:
    snippet = lines[-1]

print(f"{candidate}: {reason}")
if snippet:
    print(f"  {snippet[:240]}")
PYEOF
}

repo_is_clean() {
    [[ -z "$(git status --porcelain --ignore-submodules=dirty)" ]]
}

discard_agent_changes() {
    git restore --source=HEAD --staged --worktree .
    git clean -fd
}

cleanup_worktree() {
    if [[ "${USE_WORKTREE}" != "1" || "${KEEP_WORKTREE}" == "1" || -z "${WORKTREE_DIR}" ]]; then
        return
    fi

    git -C "${GPTME_SOURCE_DIR}" worktree remove --force "${WORKTREE_DIR}" 2>/dev/null || rm -rf "${WORKTREE_DIR}"
}

prepare_worktree() {
    if [[ "${USE_WORKTREE}" != "1" ]]; then
        return
    fi

    if ! git -C "${GPTME_SOURCE_DIR}" rev-parse --show-toplevel >/dev/null 2>&1; then
        echo "ERROR: ${GPTME_SOURCE_DIR} is not a git repository." >&2
        exit 1
    fi

    mkdir -p "${WORKTREE_ROOT}"
    WORKTREE_DIR="${WORKTREE_ROOT}/${WORKTREE_NAME}"
    git -C "${GPTME_SOURCE_DIR}" worktree remove --force "${WORKTREE_DIR}" 2>/dev/null || rm -rf "${WORKTREE_DIR}"

    git -C "${GPTME_SOURCE_DIR}" fetch origin master 2>/dev/null || true
    local base_ref="origin/master"
    if git -C "${GPTME_SOURCE_DIR}" show-ref --quiet "refs/remotes/origin/${BRANCH}"; then
        base_ref="origin/${BRANCH}"
    fi

    echo "Preparing temp worktree ${WORKTREE_DIR} on ${WORKTREE_BRANCH} from ${base_ref}"
    git -C "${GPTME_SOURCE_DIR}" worktree add --force -B "${WORKTREE_BRANCH}" "${WORKTREE_DIR}" "${base_ref}"

    # Fix submodule dirs in worktrees — git worktree add does NOT init submodules,
    # leaving empty directories. Symlinks like packages/gptmail -> ../gptme-contrib/...
    # resolve relative to their location INSIDE the worktree, so gptme-contrib must
    # exist inside the worktree root. Replace empty submodule dirs with symlinks to
    # the real ones from the source repo.
    for submod in gptme-contrib external/agent-skills; do
        local src="${GPTME_SOURCE_DIR}/${submod}"
        local dst="${WORKTREE_DIR}/${submod}"
        if [[ -d "${src}" && -d "${dst}" && ! -e "${dst}/.git" && -z "$(ls -A "${dst}" 2>/dev/null)" ]]; then
            rm -rf "${dst}"
            ln -sfn "${src}" "${dst}"
            # Hide the submodule→symlink type change from git status
            git -C "${WORKTREE_DIR}" update-index --skip-worktree "${submod}" 2>/dev/null || true
            echo "Replaced empty submodule with symlink: ${submod} → ${src}"
        fi
    done

    # Install Python packages so pre-commit hooks (mypy, tests) work.
    # Without this, `make typecheck` fails with "Can't find package 'X'"
    # because the worktree has no .venv.
    if [[ -f "${WORKTREE_DIR}/pyproject.toml" ]]; then
        local sync_err=""
        echo "Installing packages in worktree..."
        sync_err=$(cd "${WORKTREE_DIR}" && uv sync --all-packages --quiet 2>&1) || {
            echo "Warning: uv sync failed (non-fatal, pre-commit hooks may fail)" >&2
            if [[ -n "${sync_err}" ]]; then
                printf '%s\n' "${sync_err}" >&2
            fi
        }
    fi

    GPTME_DIR="${WORKTREE_DIR}"
}

run_eval_with_logging() {
    local log_ref="$1"
    local model_ref="$2"
    local csv_ref="$3"
    export SUITE EVAL_MODEL EVAL_MODEL_CANDIDATES GPTME_DIR EVAL_CMD
    ARTIFACT_DIR="${GPTME_DIR}" \
    bash "${SCRIPT_DIR}/eval-score.sh" \
        --log-output "${log_ref}" \
        --model-output "${model_ref}" \
        --csv-output "${csv_ref}"
}

summarize_eval_csv() {
    local csv_path="$1"
    python3 - "${csv_path}" << 'PYEOF'
import csv
import sys

# Generic evals (EVAL_CMD) produce no CSV — skip gracefully
if not sys.argv[1]:
    raise SystemExit(0)
rows = list(csv.DictReader(open(sys.argv[1])))
if not rows:
    raise SystemExit(0)

markdown_rows = [r for r in rows if r.get("Tool Format") == "markdown"]
focus_rows = markdown_rows or rows
passed = [r for r in focus_rows if r.get("Passed", "").lower() == "true"]
failed = [r for r in focus_rows if r.get("Passed", "").lower() != "true"]

print("CSV summary:")
print(f"- Focus format: {'markdown' if markdown_rows else 'all'}")
print(f"- Passed cases: {len(passed)}")
print(f"- Failed cases: {len(failed)}")

if failed:
    print("- Failed tests:")
    for row in failed:
        print(
            f"  - {row['Tool Format']} / {row['Test']} | passed={row['Passed']} | "
            f"duration={row['Total Duration']}s | log_dir={row['Log Dir']}"
        )
PYEOF
}

extract_failure_briefs() {
    local csv_path="$1"
    python3 - "${csv_path}" << 'PYEOF'
import csv
import json
import re
import sys
from pathlib import Path

# Generic evals (EVAL_CMD) produce no CSV — skip gracefully
if not sys.argv[1]:
    raise SystemExit(0)


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def squash(text, limit=220):
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S)
    text = re.sub(r"<thinking>.*?</thinking>", " ", text, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def analyze_failure(row):
    workspace = Path(row["Workspace Dir"])
    convo_path = Path(row["Log Dir"]) / "conversation.jsonl"
    details = {
        "task": "",
        "assistant": "",
        "error": "",
        "outside_workspace": "",
        "session_workdir": "",
        "eval_workspace": str(workspace),
        "fix_area": "",
        "score": 0,
    }

    if not convo_path.exists():
        return details

    for line in convo_path.read_text(errors="replace").splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        raw_text = content_text(message.get("content", ""))
        text = squash(raw_text)
        role = message.get("role")

        if role == "system" and not details["session_workdir"]:
            match = re.search(r"\*\*Working Directory:\*\*\s*([^\n]+)", raw_text)
            if match:
                session_workdir = match.group(1).strip()
                details["session_workdir"] = session_workdir
                path = Path(session_workdir)
                if path != workspace and workspace not in path.parents:
                    details["fix_area"] = (
                        "eval/chat workspace propagation: the conversation prompt and cwd "
                        "should use the eval workspace, not the gptme repo root"
                    )
                    details["score"] += 3

        if role == "user" and not details["task"]:
            details["task"] = text
            continue

        if role == "assistant" and text:
            details["assistant"] = text
            continue

        if role != "system" or not text:
            continue

        for match in re.findall(r"(?:Saved to|Patch successfully applied to) `?([^`\n]+)`?", text):
            path = Path(match)
            if path.is_absolute() and path != workspace and workspace not in path.parents:
                details["outside_workspace"] = str(path)
                if not details["fix_area"]:
                    details["fix_area"] = (
                        "workspace/path resolution in save or append tool handling"
                    )
                details["score"] += 2
                break

        if any(token in text for token in ("Return code:", "No module named", "Traceback", "Timeout reached")):
            details["error"] = text
            if "No module named pytest" in text and not details["fix_area"]:
                details["fix_area"] = "shell/tool guidance after successful edits; avoid unnecessary pytest verification"
            elif "No module named" in text and not details["fix_area"]:
                details["fix_area"] = "module import or environment-dependent tool loading around the failing path"
            details["score"] += 1

    return details


rows = list(csv.DictReader(open(sys.argv[1])))
if not rows:
    raise SystemExit(0)

focus_rows = [row for row in rows if row.get("Tool Format") == "markdown"] or rows
failures = [row for row in focus_rows if row.get("Passed", "").lower() != "true"]
ranked = []

for row in failures:
    details = analyze_failure(row)
    ranked.append((details["score"], row, details))

ranked.sort(key=lambda item: (-item[0], item[1]["Test"], item[1]["Tool Format"]))

print("Focused failure briefs:")
for index, (_, row, details) in enumerate(ranked[:2], start=1):
    print(f"- Target {index}: {row['Tool Format']} / {row['Test']} ({row['Total Duration']}s)")
    if details["task"]:
        print(f"  Task: {details['task']}")
    if details["session_workdir"] and details["session_workdir"] != details["eval_workspace"]:
        print(f"  Session prompt cwd: {details['session_workdir']}")
        print(f"  Eval workspace: {details['eval_workspace']}")
        print("  Signal: the eval conversation was primed with the wrong working directory/project context")
    if details["outside_workspace"]:
        print(f"  Signal: assistant wrote outside eval workspace -> {details['outside_workspace']}")
    if details["error"]:
        print(f"  Tool failure: {details['error']}")
    if details["fix_area"]:
        print(f"  Likely fix area: {details['fix_area']}")
    if details["assistant"]:
        print(f"  Last assistant step: {details['assistant']}")
PYEOF
}

run_agent_with_fallback() {
    local prompt="$1"
    local iter_log="$2"
    local raw_candidates=()
    local candidate=""
    local candidate_log=""
    local failure_summary=""
    local index=0
    local exit_code=0
    local gptme_cmd=()

    if [[ -n "${MODEL}" ]]; then
        raw_candidates=("${MODEL}")
    else
        IFS=',' read -r -a raw_candidates <<< "${MODEL_CANDIDATES}"
    fi

    : > "${iter_log}"
    : > "${iter_log}.failure"

    for raw_candidate in "${raw_candidates[@]}"; do
        candidate="$(trim "${raw_candidate}")"
        if [[ -z "${candidate}" ]]; then
            continue
        fi
        index=$((index + 1))
        candidate_log="${iter_log%.log}.candidate_${index}_$(sanitize_filename "${candidate}").log"

        echo "Running agent model: ${candidate} (harness: ${AGENT_HARNESS})"
        printf '=== candidate %s: %s ===\n' "${index}" "${candidate}" | tee -a "${iter_log}"
        case "${AGENT_HARNESS}" in
            gptme)
                gptme_cmd=(
                    gptme
                    --non-interactive
                    -w "${GPTME_DIR}"
                    -m "${candidate}"
                )
                if [[ -n "${AGENT_TOOL_FORMAT}" ]]; then
                    gptme_cmd+=(--tool-format "${AGENT_TOOL_FORMAT}")
                fi
                gptme_cmd+=("${prompt}")
                ;;
            claude-code|cc)
                # Claude Code headless mode. Disable persistence for nested runs
                # and clear inherited session env to prevent silent empty output.
                # See: gptme/gptme-contrib#585 for the root cause.
                gptme_cmd=(
                    env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CC_SESSION_ID -u CC_MODEL
                    claude -p --no-session-persistence "${prompt}"
                    --cwd "${GPTME_DIR}"
                )
                if [[ -n "${candidate}" ]]; then
                    gptme_cmd+=(--model "${candidate}")
                fi
                ;;
            *)
                echo "Unknown AGENT_HARNESS: ${AGENT_HARNESS} (supported: gptme, claude-code)" >&2
                return 1
                ;;
        esac
        set +e
        "${gptme_cmd[@]}" 2>&1 | tee "${candidate_log}"
        exit_code=${PIPESTATUS[0]}
        set -e
        cat "${candidate_log}" >> "${iter_log}"
        printf '\n' >> "${iter_log}"

        if [[ ${exit_code} -eq 0 ]]; then
            printf '%s\n' "${candidate}" > "${iter_log}.model"
            return 0
        fi

        echo "Agent model failed: ${candidate}"
        failure_summary="$(summarize_agent_failure "${candidate}" "${candidate_log}")"
        printf '%s\n' "${failure_summary}" | tee -a "${iter_log}.failure"
        discard_agent_changes
    done

    return 1
}

# Self-diagnosis function: runs when the loop is stuck for DIAGNOSIS_STUCK_ITERS consecutive
# rejections. Reads eval conversation logs and uses an LLM to classify the root cause.
# Outputs the suggested NEXT_FOCUS to stdout (captured by caller).
# If an infrastructure bug is detected, auto-files a GitHub issue and signals via log file.
run_stuck_diagnosis() {
    local stuck_at_iter="$1"
    local diagnosis_log="${LOG_DIR}/session_${SESSION_ID}_diagnosis_iter${stuck_at_iter}.txt"

    echo "=== Self-diagnosis: stuck for ${CONSECUTIVE_REJECTIONS} consecutive rejections ===" >&2

    # Collect eval conversation logs from currently failing tests.
    # Passes the last ~80 lines of each failing test's conversation.jsonl.
    local eval_conversations
    eval_conversations="$(python3 - "${CURRENT_CSV}" 2>/dev/null << 'PYEOF'
import csv, json, sys
from pathlib import Path

# Generic evals produce no CSV
if not sys.argv[1]:
    raise SystemExit(0)
rows = list(csv.DictReader(open(sys.argv[1])))
focus = [r for r in rows if r.get("Tool Format") == "markdown"] or rows
failures = [r for r in focus if r.get("Passed", "").lower() != "true"]
output = []
for row in failures[:2]:
    log_dir = Path(row.get("Log Dir", ""))
    convo = log_dir / "conversation.jsonl"
    if not convo.exists():
        output.append(f"=== {row.get('Test','?')} — no conversation log ===")
        continue
    output.append(f"=== Failing: {row.get('Test','?')} ===")
    for line in convo.read_text(errors="replace").splitlines()[-80:]:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                (p.get("text", "") if isinstance(p, dict) else str(p)) for p in content
            )
        if role in ("system", "assistant", "user") and content.strip():
            output.append(f"[{role[:3]}] {str(content)[:400]}")
    output.append("")
print("\n".join(output))
PYEOF
)"

    local diag_goal
    if [[ -n "${EVAL_CMD}" ]]; then
        diag_goal="improve score for experiment '${EXPERIMENT_NAME}' from ${BASELINE_SCORE} to higher (artifact: ${GPTME_DIR})"
    else
        diag_goal="improve gptme eval suite '${SUITE}' pass rate from ${BASELINE_SCORE} to higher"
    fi

    local diag_prompt
    diag_prompt="Diagnose a stuck autoresearch loop.

Goal: ${diag_goal}.
Current best score: ${BEST_SCORE} — no improvement for ${CONSECUTIVE_REJECTIONS} consecutive iterations.

Failing eval conversations (last messages from eval agent runs):
${eval_conversations}

Current CSV summary:
${CSV_SUMMARY}

Analyze what is blocking improvement. The root cause is one of:
- infrastructure_bug: a bug in the eval or tool infrastructure prevents the task from running correctly. The agent's code changes CANNOT fix this — it requires a fix to the infrastructure (e.g. a parsing bug, wrong file paths, incorrect tool output handling, broken eval script).
- local_optimum: easy gains are exhausted; need qualitatively different improvements.
- wrong_approach: the agent keeps trying similar ineffective changes.

Respond with ONLY these 3 lines, nothing else:
CAUSE: <infrastructure_bug|local_optimum|wrong_approach>
BUG_DETAIL: <file:function and specific symptom if infrastructure_bug, else NONE>
NEXT_FOCUS: <one concrete specific action the next iteration should take, different from previous attempts>"

    # Use first model candidate for the cheap diagnosis task
    local diag_model
    IFS=',' read -ra _cands <<< "${MODEL_CANDIDATES}"
    diag_model="$(trim "${_cands[0]}")"

    set +e
    case "${AGENT_HARNESS}" in
        gptme)
            gptme --non-interactive -m "${diag_model}" "${diag_prompt}" > "${diagnosis_log}" 2>&1
            ;;
        claude-code|cc)
            env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CC_SESSION_ID -u CC_MODEL \
                claude -p --no-session-persistence "${diag_prompt}" --model "${diag_model}" > "${diagnosis_log}" 2>&1
            ;;
    esac
    set -e

    echo "--- Diagnosis output (iter ${stuck_at_iter}) ---" >&2
    cat "${diagnosis_log}" >&2
    echo "---" >&2

    local cause bug_detail next_focus
    cause="$(grep -oE 'CAUSE: [a-z_]+' "${diagnosis_log}" 2>/dev/null | head -1 | cut -d' ' -f2 || echo 'unknown')"
    bug_detail="$(grep 'BUG_DETAIL:' "${diagnosis_log}" 2>/dev/null | head -1 | sed 's/BUG_DETAIL: //' || echo 'NONE')"
    next_focus="$(grep 'NEXT_FOCUS:' "${diagnosis_log}" 2>/dev/null | head -1 | sed 's/NEXT_FOCUS: //' || echo '')"

    echo "Diagnosis result: cause=${cause}" >&2

    # If infrastructure bug: auto-file a GitHub issue so a human can investigate
    if [[ "${cause}" == "infrastructure_bug" && "${bug_detail}" != "NONE" ]]; then
        echo "Infrastructure bug detected: ${bug_detail}" >&2
        echo "Filing GitHub issue..." >&2
        gh issue create --repo "${GITHUB_REPO}" \
            --title "autoresearch(${EXPERIMENT_NAME:-gptme}): self-diagnosis found infra bug blocking ${SUITE} improvement" \
            --body "## Autoresearch Self-Diagnosis: Infrastructure Bug

The autoresearch loop ran **${CONSECUTIVE_REJECTIONS} consecutive rejections** without improvement and self-diagnosed an infrastructure bug.

| Field | Value |
|-------|-------|
| Suite | \`${SUITE}\` |
| Score | stuck at ${BEST_SCORE} (baseline ${BASELINE_SCORE}) |
| Bug location | \`${bug_detail}\` |
| Session | \`${SESSION_ID}\` |

See: \`state/autoresearch/session_${SESSION_ID}_diagnosis_iter${stuck_at_iter}.txt\`

*Auto-filed by autoresearch self-diagnosis — operator review requested*" \
            --label "bug" >&2 2>&1 || echo "Warning: failed to file GitHub issue" >&2
    fi

    # Return only the next_focus suggestion to stdout (captured by caller)
    printf '%s' "${next_focus}"
}

mkdir -p "${LOG_DIR}"
SESSION_ID="$(date +%Y%m%d_%H%M%S)"
SESSION_LOG="${LOG_DIR}/session_${SESSION_ID}.jsonl"
CURRENT_LOG_REF="${LOG_DIR}/session_${SESSION_ID}_current-log.txt"
CURRENT_MODEL_REF="${LOG_DIR}/session_${SESSION_ID}_current-eval-model.txt"
CURRENT_CSV_REF="${LOG_DIR}/session_${SESSION_ID}_current-csv.txt"
trap cleanup_worktree EXIT

echo "=== Autoresearch Merge-Reject Loop ==="
echo "Artifact dir: ${ARTIFACT_DIR}"
echo "Agent harness: ${AGENT_HARNESS}"
echo "Program spec: ${PROGRAM_SPEC}"
if [[ -n "${EVAL_CMD}" ]]; then
    echo "Eval cmd: ${EVAL_CMD}"
fi
echo "Target branch prefix: ${BRANCH}"
echo "Worktree branch: ${WORKTREE_BRANCH}"
if [[ -n "${EVAL_MODEL}" ]]; then
    echo "Suite: ${SUITE} | Eval model: ${EVAL_MODEL}"
else
    echo "Suite: ${SUITE} | Eval model candidates: ${EVAL_MODEL_CANDIDATES}"
fi
if [[ -n "${MODEL}" ]]; then
    echo "Agent model: ${MODEL}"
else
    echo "Agent model candidates: ${MODEL_CANDIDATES}"
fi
echo "Max iterations: ${MAX_ITERATIONS}"
echo "Session log: ${SESSION_LOG}"
if [[ -n "${AGENT_TOOL_FORMAT}" ]]; then
    echo "Agent tool format: ${AGENT_TOOL_FORMAT}"
fi
echo ""

prepare_worktree

# Ensure branch exists in gptme worktree
cd "${GPTME_DIR}"
if [[ "${USE_WORKTREE}" != "1" ]]; then
    git fetch origin master 2>/dev/null || true
    if ! git show-ref --quiet "refs/heads/${BRANCH}"; then
        echo "Creating branch ${BRANCH} from origin/master..."
        git branch "${BRANCH}" origin/master
    fi
    git checkout "${BRANCH}"
fi
CURRENT_BRANCH="$(git branch --show-current)"
echo "Working tree: ${GPTME_DIR}"

if ! repo_is_clean; then
    echo "ERROR: ${GPTME_DIR} is not clean. Refusing to run autoresearch on a dirty repo." >&2
    git status --short >&2
    exit 1
fi

# Get baseline score
echo "--- Baseline eval ---"
BASELINE_SCORE="$(run_eval_with_logging "${CURRENT_LOG_REF}" "${CURRENT_MODEL_REF}" "${CURRENT_CSV_REF}")"
# shellcheck disable=SC2034  # CURRENT_LOG tracked for symmetry with CURRENT_CSV
CURRENT_LOG="$(cat "${CURRENT_LOG_REF}")"
CURRENT_EVAL_MODEL="$(cat "${CURRENT_MODEL_REF}")"
CURRENT_CSV="$(cat "${CURRENT_CSV_REF}")"
git clean -fd
echo "Baseline pass rate: ${BASELINE_SCORE} (${CURRENT_EVAL_MODEL})"

BEST_SCORE="${BASELINE_SCORE}"
IMPROVED=0
REJECTED=0
CONSECUTIVE_REJECTIONS=0
NEXT_ITER_FOCUS=""  # Injected from stuck-run self-diagnosis into next iteration's prompt
PROGRAM_TEXT="$(cat "${PROGRAM_SPEC}")"

# Cross-attempt memory: track what was tried across iterations and sessions.
# Prevents the agent from repeatedly attempting the same failing approaches.
ATTEMPT_HISTORY_FILE="${LOG_DIR}/${EXPERIMENT_NAME}-attempt-history.jsonl"
WITHIN_SESSION_HISTORY=""  # Accumulates per-iteration one-liners for prompt injection

# Load recent cross-session history (last 8 entries) for context
CROSS_SESSION_HISTORY=""
if [[ -f "${ATTEMPT_HISTORY_FILE}" ]]; then
    CROSS_SESSION_HISTORY="$(python3 - "${ATTEMPT_HISTORY_FILE}" << 'PYEOF'
import json, sys
from pathlib import Path
lines = Path(sys.argv[1]).read_text().splitlines()
entries = []
for line in lines[-8:]:
    try:
        r = json.loads(line)
        files = r.get('files', '')[:60]
        entries.append(
            f"  prev-session iter{r['iteration']} [{r['status']}] "
            f"files: {files} | {r['before_score']}→{r['new_score']}"
        )
    except Exception:
        continue
print('\n'.join(entries))
PYEOF
)" || CROSS_SESSION_HISTORY=""
fi

for i in $(seq 1 "${MAX_ITERATIONS}"); do
    echo ""
    echo "=== Iteration ${i}/${MAX_ITERATIONS} | Best: ${BEST_SCORE} ==="

    # Save current state
    BEFORE_COMMIT="$(git rev-parse HEAD)"

    # Reuse the best-known eval log instead of rerunning the suite before every iteration.
    CSV_SUMMARY="$(summarize_eval_csv "${CURRENT_CSV}")"
    FAILURE_BRIEFS="$(extract_failure_briefs "${CURRENT_CSV}")"
    PREV_BEST_SCORE="${BEST_SCORE}"

    if [[ -n "${EVAL_CMD}" ]]; then
        # Generic eval experiment: use PROGRAM_TEXT as primary instruction.
        # The harness handles eval; agent just needs to make one improvement.
        PROMPT="Experiment: ${EXPERIMENT_NAME}
Artifact: ${GPTME_DIR}
Current branch: ${CURRENT_BRANCH}
Current best score: ${BEST_SCORE}

Follow this program strictly:
${PROGRAM_TEXT}

Stage your change with 'git add' but do NOT commit — the harness will commit if the score improves."
    else
        # gptme-eval-specific prompt with failure briefs and suite-specific guidance.
        PROMPT="CRITICAL:
- The harness already ran '${SUITE}'. Do NOT rerun evals, 'make eval', pytest, or broad repo exploration.
- Treat Target 1 below as the only task for this iteration.
- Start from the likely fix area in Target 1. If you cannot find a concrete fix quickly, stop without changes.

Failure briefs (Target 1 is the primary target):
${FAILURE_BRIEFS}

Current score context:
${CSV_SUMMARY}

You are improving the gptme eval pass rate on the '${SUITE}' suite in ${GPTME_DIR}.
Current branch: ${CURRENT_BRANCH}
Current best pass rate: ${BEST_SCORE}
Current eval model: ${CURRENT_EVAL_MODEL}

Follow this program strictly:
${PROGRAM_TEXT}

Make ONE focused, targeted improvement to the gptme codebase that could improve eval performance.
Target ONLY Target 1 above during this iteration.
Focus on the failed markdown cases first, since that's the only format currently showing any passes.
Focus on: fixing a bug visible in the failure brief, improving workspace/path handling, improving tool output parsing, or fixing an import/API call.
If Target 1 shows that the conversation prompt cwd differs from the eval workspace, fix eval workspace propagation or prompt context generation first; do NOT patch save/append internals unless the failure brief specifically points there.
Do NOT spend this iteration on broad repo exploration.
Do NOT run extra verification commands unless they directly support the targeted failure.
Do NOT refactor, do NOT change tests. Make a minimal change.
Stage your change with 'git add' but do NOT commit — the harness will commit if the score improves."
    fi

    # Inject self-diagnosis context when the loop was previously stuck
    if [[ -n "${NEXT_ITER_FOCUS}" ]]; then
        PROMPT="${PROMPT}

SELF-DIAGNOSIS FROM PREVIOUS STUCK ANALYSIS:
${NEXT_ITER_FOCUS}
This is a DIFFERENT angle from what was tried before — previous approaches did not improve the score. Prioritize this direction."
    fi

    # Inject cross-attempt memory (within-session + recent prior sessions)
    if [[ -n "${WITHIN_SESSION_HISTORY}" || -n "${CROSS_SESSION_HISTORY}" ]]; then
        PROMPT="${PROMPT}

ATTEMPT HISTORY — do NOT repeat these approaches:
${CROSS_SESSION_HISTORY}${WITHIN_SESSION_HISTORY}"
    fi

    # Run gptme agent (non-interactive)
    ITER_LOG="${LOG_DIR}/session_${SESSION_ID}_iter_${i}.log"
    if ! run_agent_with_fallback "${PROMPT}" "${ITER_LOG}"; then
        echo "Agent failed for all candidate models — stopping session"
        break
    fi

    # Check if agent made any changes
    if repo_is_clean; then
        echo "No changes made by agent — skipping eval"
        continue
    fi

    # Stage agent changes before eval so cleanup only removes eval-generated junk.
    git add -A
    # Capture what was changed (for cross-attempt memory logging)
    CHANGED_FILES="$(git diff --cached --name-only HEAD | tr '\n' ' ' | xargs 2>/dev/null || true)"

    # Run eval to measure impact
    echo "--- Evaluating change ---"
    set +e
    NEW_SCORE="$(run_eval_with_logging "${CURRENT_LOG_REF}.new" "${CURRENT_MODEL_REF}.new" "${CURRENT_CSV_REF}.new")"
    SCORE_EXIT=$?
    set -e

    if [[ ${SCORE_EXIT} -ne 0 ]]; then
        echo "Eval failed — discarding change"
        discard_agent_changes
        rm -f "${CURRENT_LOG_REF}.new" "${CURRENT_MODEL_REF}.new"
        continue
    fi

    NEW_EVAL_MODEL="$(cat "${CURRENT_MODEL_REF}.new")"
    NEW_LOG="$(cat "${CURRENT_LOG_REF}.new")"
    NEW_CSV="$(cat "${CURRENT_CSV_REF}.new")"
    git clean -fd
    echo "Score: ${PREV_BEST_SCORE} → ${NEW_SCORE} (${NEW_EVAL_MODEL})"

    # Compare scores (python for float comparison)
    IMPROVED_FLAG="$(python3 -c "print('yes' if float('${NEW_SCORE}') > float('${PREV_BEST_SCORE}') else 'no')")"

    if [[ "${IMPROVED_FLAG}" == "yes" ]]; then
        echo "✅ IMPROVED — committing change"
        # Pre-format staged Python files so ruff-format hook doesn't fail (it exits 1
        # when it modifies files, aborting the commit). Running ruff before commit
        # makes the hook a no-op. Re-stage after formatting to include changes.
        staged_py="$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$' || true)"
        if [[ -n "${staged_py}" ]]; then
            echo "${staged_py}" | xargs ruff format --quiet 2>/dev/null || true
            git add -u
        fi
        git commit -m "autoresearch(iter${i}): improve ${SUITE} pass rate ${PREV_BEST_SCORE} → ${NEW_SCORE}"
        BEST_SCORE="${NEW_SCORE}"
        CURRENT_EVAL_MODEL="${NEW_EVAL_MODEL}"
        # shellcheck disable=SC2034
        CURRENT_LOG="${NEW_LOG}"
        CURRENT_CSV="${NEW_CSV}"
        mv "${CURRENT_LOG_REF}.new" "${CURRENT_LOG_REF}"
        mv "${CURRENT_MODEL_REF}.new" "${CURRENT_MODEL_REF}"
        mv "${CURRENT_CSV_REF}.new" "${CURRENT_CSV_REF}"
        IMPROVED=$((IMPROVED + 1))
        STATUS="improved"
        CONSECUTIVE_REJECTIONS=0
        NEXT_ITER_FOCUS=""
        WITHIN_SESSION_HISTORY="${WITHIN_SESSION_HISTORY}
  iter${i} [improved] files: ${CHANGED_FILES}| ${PREV_BEST_SCORE}→${NEW_SCORE}"
    else
        echo "❌ REJECTED — discarding change"
        discard_agent_changes
        REJECTED=$((REJECTED + 1))
        STATUS="rejected"
        WITHIN_SESSION_HISTORY="${WITHIN_SESSION_HISTORY}
  iter${i} [rejected] files: ${CHANGED_FILES}| ${PREV_BEST_SCORE}→${NEW_SCORE}"
        rm -f "${CURRENT_LOG_REF}.new" "${CURRENT_MODEL_REF}.new" "${CURRENT_CSV_REF}.new"

        # Self-diagnosis: if stuck for DIAGNOSIS_STUCK_ITERS consecutive rejections,
        # read eval logs and use an LLM to identify root cause.
        CONSECUTIVE_REJECTIONS=$((CONSECUTIVE_REJECTIONS + 1))
        if [[ "${CONSECUTIVE_REJECTIONS}" -ge "${DIAGNOSIS_STUCK_ITERS}" ]]; then
            echo "Stuck for ${CONSECUTIVE_REJECTIONS} rejections — running self-diagnosis..."
            NEXT_ITER_FOCUS="$(run_stuck_diagnosis "${i}")"
            CONSECUTIVE_REJECTIONS=0
            # Check if diagnosis flagged an infrastructure bug (loop should stop early)
            LATEST_DIAG_LOG="${LOG_DIR}/session_${SESSION_ID}_diagnosis_iter${i}.txt"
            if [[ -f "${LATEST_DIAG_LOG}" ]]; then
                DIAG_CAUSE="$(grep -oE 'CAUSE: [a-z_]+' "${LATEST_DIAG_LOG}" | head -1 | cut -d' ' -f2 || echo '')"
                if [[ "${DIAG_CAUSE}" == "infrastructure_bug" ]]; then
                    echo "Stopping session early: infrastructure bug detected by self-diagnosis"
                    break
                fi
            fi
        fi
    fi

    # Log iteration result (session log + cross-session history)
    # Use env vars to avoid single-quote injection from filenames in CHANGED_FILES
    _iter_record="$(
        _AR_SESSION="${SESSION_ID}" \
        _AR_ITER="${i}" \
        _AR_BEFORE="${PREV_BEST_SCORE}" \
        _AR_NEW="${NEW_SCORE}" \
        _AR_STATUS="${STATUS}" \
        _AR_BEFORE_COMMIT="${BEFORE_COMMIT}" \
        _AR_COMMIT="$(git rev-parse HEAD)" \
        _AR_EVAL_MODEL="${NEW_EVAL_MODEL}" \
        _AR_AGENT_MODEL="$(cat "${ITER_LOG}.model" 2>/dev/null || true)" \
        _AR_FILES="${CHANGED_FILES}" \
        python3 -c "
import json, os
print(json.dumps({
    'session': os.environ['_AR_SESSION'],
    'iteration': int(os.environ['_AR_ITER']),
    'before_score': os.environ['_AR_BEFORE'],
    'new_score': os.environ['_AR_NEW'],
    'status': os.environ['_AR_STATUS'],
    'before_commit': os.environ['_AR_BEFORE_COMMIT'],
    'commit': os.environ['_AR_COMMIT'],
    'eval_model': os.environ['_AR_EVAL_MODEL'],
    'agent_model': os.environ['_AR_AGENT_MODEL'],
    'files': os.environ['_AR_FILES'],
}))"
    )"
    echo "${_iter_record}" >> "${SESSION_LOG}"
    echo "${_iter_record}" >> "${ATTEMPT_HISTORY_FILE}" 2>/dev/null || true
done

echo ""
echo "=== Session Summary ==="
echo "Baseline: ${BASELINE_SCORE} → Best: ${BEST_SCORE}"
echo "Improved: ${IMPROVED} | Rejected: ${REJECTED}"
echo ""

# If we improved, push branch and optionally create PR
if [[ ${IMPROVED} -gt 0 ]]; then
    echo "Pushing improvements to origin/${PUSH_BRANCH}..."
    git push -u origin "HEAD:${PUSH_BRANCH}" 2>/dev/null || git push --force-with-lease origin "HEAD:${PUSH_BRANCH}"
    echo "Branch pushed: ${PUSH_BRANCH}"

    # Auto-create PR if score delta meets publish threshold
    SCORE_DELTA="$(python3 -c "print(f'{float(\"${BEST_SCORE}\") - float(\"${BASELINE_SCORE}\"):.3f}')")"
    _threshold="${PUBLISH_THRESHOLD:-0}"
    THRESHOLD_MET="$(python3 -c "print('yes' if float('${SCORE_DELTA}') >= float('${_threshold}') else 'no')")"

    if [[ "${THRESHOLD_MET}" == "yes" ]]; then
        # Check if PR already exists for this branch
        EXISTING_PR="$(gh pr list --repo "${GITHUB_REPO}" --head "${PUSH_BRANCH}" --json number --jq '.[0].number' 2>/dev/null || true)"
        if [[ -n "${EXISTING_PR}" ]]; then
            echo "PR already exists: ${GITHUB_REPO}#${EXISTING_PR} — skipping PR creation"
        else
            echo "Score delta ${SCORE_DELTA} >= threshold ${PUBLISH_THRESHOLD} — creating PR..."
            PR_TITLE="autoresearch(${EXPERIMENT_NAME:-gptme}): improve ${SUITE} pass rate ${BASELINE_SCORE} → ${BEST_SCORE}"
            PR_BODY="## Autoresearch Session: ${SESSION_ID}

Automated improvement via merge-reject loop.

| Metric | Value |
|--------|-------|
| Eval suite | \`${SUITE}\` |
| Baseline score | ${BASELINE_SCORE} |
| Best score | ${BEST_SCORE} |
| Score delta | **+${SCORE_DELTA}** |
| Iterations accepted | ${IMPROVED} |
| Iterations rejected | ${REJECTED} |
| Experiment | ${EXPERIMENT_NAME:-gptme} |

## What changed

Each commit in this branch was accepted by the merge-reject gate (eval score must improve).
Rejected attempts were discarded; only improvements survived.

See session log: \`state/autoresearch/session_${SESSION_ID}.jsonl\`

---
*Generated by autoresearch loop — do not merge until manually reviewed.*"
            PR_URL="$(gh pr create --repo "${GITHUB_REPO}" --base master --head "${PUSH_BRANCH}" \
                --title "${PR_TITLE}" --body "${PR_BODY}" 2>&1)"
            echo "PR created: ${PR_URL}"
        fi
    else
        echo "Score delta ${SCORE_DELTA} < threshold ${PUBLISH_THRESHOLD} — not creating PR yet (waiting for more gains)"
        echo "To create PR manually: gh pr create --repo ${GITHUB_REPO} --base master --head ${PUSH_BRANCH}"
    fi
fi
