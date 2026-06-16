#!/usr/bin/env python3
"""Match gptme-style lessons against conversation context and inject relevant ones.

Claude Code hook for two events:
- UserPromptSubmit: matches against user's prompt text
- PreToolUse: matches against tool input (file paths, commands, search patterns)
  AND recent transcript context (tool outputs, assistant responses)

The PreToolUse transcript context means lessons fire on *what happened* (e.g.
"merge conflicts" in Bash output → conflict-resolution lesson), not only on
*what's being requested* in the tool call. UserPromptSubmit fires once at
session start, so PreToolUse is the primary trigger for autonomous runs.

Both events inject relevant lessons as additionalContext for Claude Code.
This replicates gptme's keyword-based lesson injection for Claude Code sessions.

Lesson dirs are read from gptme.toml [lessons] dirs (single source of truth).
Matching uses the same keyword/wildcard logic as gptme's LessonMatcher.
Already-injected lessons are tracked via a session state file to avoid duplicates.

## Installation

Copy or symlink this file into your agent workspace:
    cp match-lessons.py /path/to/workspace/.claude/hooks/match-lessons.py

Then register it in your workspace Claude Code settings
(.claude/settings.json in the workspace root):

    {
      "hooks": {
        "UserPromptSubmit": [{
          "hooks": [{
            "type": "command",
            "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/match-lessons.py",
            "timeout": 10
          }]
        }],
        "PreToolUse": [{
          "matcher": "Read|Bash|Grep|WebFetch|WebSearch",
          "hooks": [{
            "type": "command",
            "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/match-lessons.py",
            "timeout": 10
          }]
        }]
      }
    }

The hook auto-discovers your workspace root (the directory containing gptme.toml)
and reads lesson dirs from [lessons] dirs in that config file.

State directories (Thompson sampling, predictions, trajectories) are stored
under workspace/state/ and created automatically on first use.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, cast

# PreToolUse throttle: minimum seconds between lesson matches
PRETOOL_COOLDOWN_SECONDS = 15
# Thompson sampling: weight of posterior mean in final score (additive)
TS_WEIGHT = 1.0
# Maximum lessons to inject per PreToolUse event
MAX_PRETOOL_LESSONS = 3
# Maximum lessons to inject per UserPromptSubmit event
MAX_PROMPT_LESSONS = 5
# Maximum predicted lessons to inject per event (on top of keyword matches)
MAX_PREDICTED_LESSONS = 2
# Minimum lift for a prediction to be injected (from model, but also enforced here)
MIN_PREDICTION_LIFT = 2.0
# Minimum TS posterior mean for predicted lessons (deprioritize known-noise lessons)
MIN_PREDICTION_TS = 0.30
# State directory for cross-invocation dedup (in /tmp, not workspace)
STATE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "claude-lesson-match"


# --- Workspace discovery (all state paths derived from here) ---

_workspace: Path | None = None


def find_workspace() -> Path:
    """Find the workspace root (where gptme.toml lives).

    Walks up from the script location, then falls back to cwd.
    Works correctly whether the script lives in .claude/hooks/ (inside the
    workspace) or in gptme-contrib/scripts/claude-code-hooks/ (linked from
    an agent workspace).
    """
    script_dir = Path(__file__).resolve().parent
    for p in [script_dir, *script_dir.parents]:
        if (p / "gptme.toml").exists():
            return p
    # Also try cwd (useful when invoked from workspace root)
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "gptme.toml").exists():
            return p
    # Fallback: current working directory
    return Path.cwd()


def get_workspace() -> Path:
    """Return cached workspace root."""
    global _workspace
    if _workspace is None:
        _workspace = find_workspace()
    return _workspace


def _ts_state_dir() -> Path:
    """Thompson sampling state directory (workspace-relative)."""
    return get_workspace() / "state" / "lesson-thompson"


def _prediction_model_file() -> Path:
    """Prediction model file (workspace-relative)."""
    return get_workspace() / "state" / "lesson-predictions" / "prediction-model.json"


def _trajectory_log_dir() -> Path:
    """Trajectory log directory (workspace-relative)."""
    return get_workspace() / "state" / "lesson-trajectories"


def detect_harness() -> str:
    """Detect the current runtime harness from environment variables."""
    if os.environ.get("CLAUDECODE"):
        return "claude-code"
    if os.environ.get("CODEX") or os.environ.get("CODEX_INSTALLED"):
        return "codex"
    return "gptme"


def filter_by_harness(
    lessons: list[dict[str, Any]], current_harness: str
) -> list[dict[str, Any]]:
    """Keep only lessons whose harness restriction includes this harness."""
    filtered: list[dict[str, Any]] = []
    for lesson in lessons:
        restrict = lesson.get("harness_restrict", [])
        if not restrict or current_harness in restrict:
            filtered.append(lesson)
    return filtered


def _load_shared_prompt_lessons():
    """Import the shared prompt-lessons resolver from the workspace package."""
    workspace = get_workspace()
    context_src = workspace / "packages" / "context" / "src"
    if str(context_src) not in sys.path:
        sys.path.insert(0, str(context_src))

    from context import prompt_lessons  # type: ignore[attr-defined]

    return prompt_lessons


# --- Thompson sampling ---


def load_ts_means(lesson_paths: list[str]) -> dict[str, float]:
    """Load Thompson sampling posterior means for scored lesson re-ranking.

    Returns dict mapping lesson_path → posterior mean effectiveness [0, 1].
    Uses deterministic expected value (alpha / (alpha + beta)) for stable ranking.

    Confounding correction: lessons marked confounded=true get a floor of 0.5.
    These lessons fire in inherently hard sessions (CI failures, conflicts, blocked
    tasks), so low E[p] reflects session difficulty, not lesson quality.
    """
    state_file = _ts_state_dir() / "bandit-state.json"
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text())
        arms = data.get("arms", {})
        means: dict[str, float] = {}
        for path in lesson_paths:
            if path in arms:
                arm = arms[path]
                alpha = arm.get("alpha", 1.0)
                beta_val = arm.get("beta", 1.0)
                ep = alpha / (alpha + beta_val)
                # Apply floor for confounded lessons (fire in hard session types)
                if arm.get("confounded", False):
                    ep = max(ep, 0.5)
                means[path] = ep
        return means
    except Exception:
        return {}


# --- Prediction model ---


def load_prediction_model() -> "dict | None":
    """Load the co-occurrence prediction model for early lesson injection.

    Returns the model dict or None if not available.
    Model is built by scripts/build-lesson-predictions.py.
    """
    model_file = _prediction_model_file()
    if not model_file.exists():
        return None
    try:
        data: dict = json.loads(model_file.read_text())
        if data.get("model_version") != 1:
            return None
        return data
    except Exception:
        return None


def get_predicted_lessons(
    matched_paths: list[str],
    already_injected: set[str],
    all_lessons: list[dict],
    max_predictions: int = MAX_PREDICTED_LESSONS,
) -> list[dict]:
    """Get lessons predicted by co-occurrence with already-matched lessons.

    When lesson A fires via keyword match, this checks the prediction model
    for lessons B, C that historically co-occur with A at high lift. Returns
    lesson dicts for predicted lessons that haven't been injected yet.

    Prediction confidence is additionally gated by Thompson sampling posterior
    means so known-noise lessons get deprioritized/filtered over time.
    """
    model = load_prediction_model()
    if not model:
        return []

    co_preds = model.get("co_occurrence", {})
    temporal = model.get("temporal", {})
    titles = model.get("titles", {})

    # Build reverse lookup: title → ALL model paths (handles duplicate titles
    # across lessons/ and gptme-contrib/lessons/ — both paths may be co-occurrence
    # triggers, so we need to check all of them)
    title_to_model_paths: dict[str, list[str]] = {}
    for model_path, title in titles.items():
        title_to_model_paths.setdefault(title, []).append(model_path)

    # Find matching lesson objects from the scanned lessons
    lesson_by_path: dict[str, dict] = {les["path"]: les for les in all_lessons}
    lesson_by_title: dict[str, dict] = {
        les.get("title", ""): les for les in all_lessons if les.get("title")
    }

    # Collect all predicted paths with their best lift score
    predicted: dict[str, float] = {}  # path → best lift

    for matched_path in matched_paths:
        # Try direct path lookup first, then title-based fallback
        lookup_paths = [matched_path]
        # Find title for this matched path
        matched_title = titles.get(matched_path, "")
        if not matched_title:
            # Path not in model's titles — try scanning all_lessons for title
            for les in all_lessons:
                if les.get("path") == matched_path:
                    matched_title = les.get("title", "")
                    break
        # Add ALL model paths with the same title (handles cross-path duplicates)
        if matched_title and matched_title in title_to_model_paths:
            for model_path in title_to_model_paths[matched_title]:
                if model_path != matched_path:
                    lookup_paths.append(model_path)

        for lp in lookup_paths:
            # Check co-occurrence predictions
            for pred in co_preds.get(lp, []):
                path = pred["path"]
                lift = pred.get("lift", 0)
                if lift >= MIN_PREDICTION_LIFT and path not in already_injected:
                    if path not in matched_paths:  # Don't predict already-matched
                        predicted[path] = max(predicted.get(path, 0), lift)

            # Check temporal predictions (early→late)
            for pred in temporal.get(lp, []):
                path = pred["path"]
                lift = pred.get("lift", 0)
                if lift >= MIN_PREDICTION_LIFT and path not in already_injected:
                    if path not in matched_paths:
                        predicted[path] = max(predicted.get(path, 0), lift)

    if not predicted:
        return []

    # Resolve path/title candidates to scanned lesson paths so we can apply
    # Thompson means even when model path and runtime lesson path differ.
    candidate_paths: set[str] = set()
    path_candidates: dict[str, list[str]] = {}
    for path in predicted:
        candidates = [path]
        pred_title = titles.get(path, "")
        if pred_title:
            title_match = lesson_by_title.get(pred_title)
            if title_match:
                candidates.append(title_match["path"])
        # Dedup while preserving order
        deduped: list[str] = []
        seen = set()
        for c in candidates:
            if c not in seen:
                seen.add(c)
                deduped.append(c)
                candidate_paths.add(c)
        path_candidates[path] = deduped

    ts_means = load_ts_means(list(candidate_paths))

    # Sort by lift first; TS threshold then filters out low-confidence predictions
    sorted_preds = sorted(predicted.items(), key=lambda x: -x[1])

    results = []
    for path, lift in sorted_preds:
        lesson = lesson_by_path.get(path)
        # Fallback: find by title if path doesn't match (cross-path dedup)
        if not lesson:
            pred_title = titles.get(path, "")
            if pred_title:
                lesson = lesson_by_title.get(pred_title)
        if not lesson:
            continue

        # Choose best available TS mean across model path + resolved runtime path.
        # Default 0.5 keeps behavior neutral when no TS state exists.
        candidates = path_candidates.get(path, [path])
        ts_mean = max((ts_means.get(c, 0.5) for c in candidates), default=0.5)
        if ts_mean < MIN_PREDICTION_TS:
            continue

        results.append(
            {
                **lesson,
                "predicted": True,
                "prediction_lift": lift,
                "prediction_ts_mean": ts_mean,
                "matched_by": [f"predicted (lift={lift:.1f}x, ts={ts_mean:.2f})"],
            }
        )
        if len(results) >= max_predictions:
            break

    return results


_PROMPT_LESSONS = _load_shared_prompt_lessons()
load_lesson_dirs = _PROMPT_LESSONS.load_lesson_dirs
scan_lessons = _PROMPT_LESSONS.scan_lessons
parse_holdout_lessons_env = _PROMPT_LESSONS.parse_holdout_lessons_env
is_held_out_lesson = _PROMPT_LESSONS.is_held_out_lesson
filter_held_out_lessons = _PROMPT_LESSONS.filter_held_out_lessons
parse_dropout_epsilon = _PROMPT_LESSONS.parse_dropout_epsilon
select_randomized_dropout = _PROMPT_LESSONS.select_randomized_dropout


def score_lessons(
    lessons: list[dict[str, Any]], prompt: str, max_results: int = 5
) -> list[dict[str, Any]]:
    """Match lessons via the shared prompt-lessons resolver."""
    return cast(
        list[dict[str, Any]],
        _PROMPT_LESSONS.score_lessons(
            get_workspace(), lessons, prompt, max_results=max_results
        ),
    )


# --- Session state for cross-invocation dedup ---


def _state_file(session_id: str) -> Path:
    """Get the state file path for a session."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize session_id for filesystem
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    return STATE_DIR / f"{safe_id}.json"


def load_session_state(session_id: str) -> dict:
    """Load session state (injected lessons, last pretool time)."""
    try:
        sf = _state_file(session_id)
        if sf.exists():
            data = json.loads(sf.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"injected": [], "last_pretool": 0}


def save_session_state(session_id: str, state: dict) -> None:
    """Save session state atomically (write-then-rename for POSIX safety)."""
    try:
        sf = _state_file(session_id)
        tmp = sf.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(sf)  # atomic on POSIX; avoids partial reads under concurrent hooks
    except Exception:
        pass


def get_already_injected(
    session_id: str, transcript_path: str | None = None
) -> set[str]:
    """Get set of lesson paths already injected in this session.

    Uses session state file as primary source, with transcript fallback.
    """
    injected: set[str] = set()

    # From state file
    state = load_session_state(session_id)
    injected.update(state.get("injected", []))

    # From transcript (catches lessons from before state tracking)
    if transcript_path:
        try:
            with open(transcript_path, encoding="utf-8") as f:
                for line in f:
                    for m in re.finditer(r"\*Source: ([^*]+)\*", line):
                        injected.add(m.group(1).strip())
        except Exception:
            pass

    return injected


# --- Match text extraction ---


def build_pretool_match_text(tool_name: str, tool_input: dict) -> str:
    """Build match text from PreToolUse tool name and input fields."""
    parts = []

    # Extract relevant fields from tool input
    for key in (
        "file_path",
        "command",
        "pattern",
        "prompt",
        "query",
        "url",
        "description",
    ):
        val = tool_input.get(key)
        if val and isinstance(val, str):
            parts.append(val)

    return " ".join(parts)


def extract_recent_transcript_text(
    transcript_path: str | None,
    max_messages: int = 1,
    max_chars_per_message: int = 800,
    max_total_chars: int = 1500,
) -> str:
    """Extract text from the most recent tool result in the transcript.

    Broadens lesson matching beyond tool input — lessons can be triggered by
    keywords appearing in tool outputs (e.g. "merge conflicts" in a Bash
    output triggers the PR conflict lesson even if the user prompt is just
    "fix it").

    Only includes tool_result content (actual command outputs, errors, file
    contents). Assistant text blocks are excluded — they contain incidental
    keyword mentions from reasoning/discussion that cause ~90% false positive
    rate (measured via LLM-as-judge in session 260).

    Window reduced from 6→1 message (session 269): matching against 6 recent
    tool outputs caused 95% noop rate because old outputs contain incidental
    keywords. Only the MOST RECENT tool output is relevant for "what just
    happened" lesson triggers.

    Skips: system prompt, assistant text blocks, tool_use inputs.
    Includes: tool_result content strings only (most recent).
    """
    if not transcript_path:
        return ""
    try:
        texts: list[str] = []
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")
                message = entry.get("message", {})
                role = message.get("role", "")
                content = message.get("content", "")

                # Tool result content only (skip assistant text — too noisy)
                if entry_type == "user" and role == "user":
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "tool_result"
                            ):
                                tool_content = block.get("content", "")
                                if tool_content and isinstance(tool_content, str):
                                    texts.append(tool_content[:max_chars_per_message])

        recent = [t for t in texts if t.strip()][-max_messages:]
        combined = "\n".join(recent)
        # Strip system-reminder blocks (contain previously injected lessons)
        # to prevent self-referential keyword matches (gptme-contrib#341)
        combined = re.sub(
            r"<system-reminder>.*?</system-reminder>",
            "",
            combined,
            flags=re.DOTALL,
        )
        return combined[:max_total_chars]
    except Exception:
        return ""


# --- Output helpers ---


def emit_empty(event_name: str) -> None:
    """Emit empty hook output (no context to inject)."""
    # For PreToolUse, exit 0 with no output is cleanest — doesn't interfere
    # with permissions. For UserPromptSubmit, we emit the standard structure.
    if event_name == "UserPromptSubmit":
        json.dump(
            {"hookSpecificOutput": {"hookEventName": event_name}},
            sys.stdout,
        )


def format_lessons(
    matches: list[dict],
    already_injected: set[str],
    predicted: list[dict] | None = None,
) -> str:
    """Format matched + predicted lessons as markdown context."""
    parts: list[str] = []

    for m in matches:
        if m["path"] in already_injected:
            continue

        # Don't inject keyword count metadata alongside content — it leaks
        # matching internals and contributes to self-referential corpus matches
        # when analysis tools grep session transcripts (gptme-contrib#341)
        parts.append(f"### {m['title']}")
        parts.append(f"*Source: {m['path']}*\n")
        parts.append(m["body"])
        parts.append("")

    # Add predicted lessons (from co-occurrence model)
    if predicted:
        for p in predicted:
            if p["path"] in already_injected:
                continue
            # Simplified header — no lift score metadata
            parts.append(f"### {p['title']} (predicted)")
            parts.append(f"*Source: {p['path']}*\n")
            parts.append(p["body"])
            parts.append("")

    if not parts:
        return ""

    return "## Matched Lessons (auto-injected)\n\n" + "\n".join(parts)


def extract_tool_sequence(
    transcript_path: str | None, max_tools: int = 50
) -> list[str]:
    """Extract the sequence of tool names used so far in the session.

    Reads the JSONL transcript and collects tool_use block names from assistant
    messages. Returns the last `max_tools` tools to keep the sequence bounded.
    """
    if not transcript_path:
        return []
    try:
        tools: list[str] = []
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tools.append(block.get("name", "?"))
        return tools[-max_tools:]
    except Exception:
        return []


def log_trajectory_match(
    session_id: str,
    event_type: str,
    tool_sequence: list[str],
    current_tool: str,
    matched_lessons: list[dict],
    already_injected: set[str],
) -> None:
    """Append a trajectory-match record to the daily log for predict-early analysis.

    Each record captures: when a lesson was matched, what tool sequence preceded it,
    and which lesson fired. This builds the dataset for n-gram/co-occurrence analysis.

    Log format: one JSON object per line in a daily file.
    """
    newly_matched = [m for m in matched_lessons if m["path"] not in already_injected]
    if not newly_matched:
        return

    try:
        log_dir = _trajectory_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

        # Build compact n-gram context (last 10 tools)
        recent_tools = tool_sequence[-10:]

        record = {
            "ts": time.time(),
            "session_id": session_id,
            "event": event_type,
            "current_tool": current_tool,
            "tool_seq": recent_tools,
            "tool_count": len(tool_sequence),
            "lessons": [
                {
                    "path": m["path"],
                    "title": m["title"],
                    "score": m.get("score", 0),
                    "matched_by": m.get("matched_by", []),
                    **(
                        {
                            "predicted": True,
                            "prediction_lift": m.get("prediction_lift", 0),
                        }
                        if m.get("predicted")
                        else {}
                    ),
                }
                for m in newly_matched
            ],
        }

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # Never fail the hook for logging


def main():
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    # Detect event type from hook_event_name (available in all hook inputs)
    event_type = hook_input.get("hook_event_name", "UserPromptSubmit")
    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path")

    # --- Build match text based on event type ---
    if event_type == "UserPromptSubmit":
        match_text = hook_input.get("prompt", "")
        max_results = MAX_PROMPT_LESSONS

    elif event_type == "PreToolUse":
        # Throttle: skip if we matched recently
        state = load_session_state(session_id)
        elapsed = time.time() - state.get("last_pretool", 0)
        if elapsed < PRETOOL_COOLDOWN_SECONDS:
            sys.exit(0)

        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input", {})
        if not isinstance(tool_input, dict):
            sys.exit(0)

        tool_match_text = build_pretool_match_text(tool_name, tool_input)
        # Also match against most recent tool output (not assistant text)
        # so lessons fire on *what just happened* (e.g. merge conflict in
        # last Bash output → inject conflict lesson for next tool call).
        transcript_text = extract_recent_transcript_text(transcript_path)
        match_text = (
            f"{tool_match_text}\n{transcript_text}"
            if transcript_text
            else tool_match_text
        )
        max_results = MAX_PRETOOL_LESSONS

    else:
        # Unknown event type — no-op
        sys.exit(0)

    # Strip system-reminder blocks from match text — these contain previously
    # injected lesson content whose keywords would self-referentially re-match
    # (gptme-contrib#341). Filtering here covers both event types.
    match_text = re.sub(
        r"<system-reminder>.*?</system-reminder>",
        "",
        match_text,
        flags=re.DOTALL,
    )

    if not match_text.strip():
        emit_empty(event_type)
        sys.exit(0)

    # --- Scan and match lessons ---
    workspace = get_workspace()
    lesson_dirs = load_lesson_dirs(workspace)
    lessons = scan_lessons(lesson_dirs)
    current_harness = detect_harness()
    lessons = filter_by_harness(lessons, current_harness)
    holdout_lessons = parse_holdout_lessons_env()

    if not lessons:
        emit_empty(event_type)
        sys.exit(0)

    raw_matches = score_lessons(lessons, match_text, max_results=max_results)
    if not raw_matches:
        emit_empty(event_type)
        sys.exit(0)

    # --- Dedup: skip already-injected lessons ---
    already_injected = get_already_injected(session_id, transcript_path)

    # --- Prediction: inject co-occurring lessons proactively ---
    matched_paths = [
        m["path"] for m in raw_matches if m["path"] not in already_injected
    ]
    predicted = get_predicted_lessons(
        matched_paths, already_injected, lessons, MAX_PREDICTED_LESSONS
    )

    # --- Holdout filtering (A/B testing via HOLDOUT_LESSONS env var) ---
    matches = filter_held_out_lessons(raw_matches, holdout_lessons)
    predicted = filter_held_out_lessons(predicted, holdout_lessons)

    context = format_lessons(matches, already_injected, predicted)

    if not context:
        emit_empty(event_type)
        sys.exit(0)

    # --- Update session state ---
    state = load_session_state(session_id)
    newly_injected = [m["path"] for m in matches if m["path"] not in already_injected]
    predicted_injected = [
        p["path"] for p in predicted if p["path"] not in already_injected
    ]
    existing = set(state.get("injected", []))
    existing.update(newly_injected)
    existing.update(predicted_injected)
    state["injected"] = list(existing)
    if event_type == "PreToolUse":
        state["last_pretool"] = time.time()
    save_session_state(session_id, state)

    # --- Log trajectory data for predict-early analysis ---
    tool_sequence = extract_tool_sequence(transcript_path)
    current_tool = ""
    if event_type == "PreToolUse":
        current_tool = hook_input.get("tool_name", "")
    # Include predicted lessons in trajectory logging (marked with "predicted" flag)
    all_logged = matches + [
        {**p, "predicted": True} for p in predicted if p["path"] not in already_injected
    ]
    log_trajectory_match(
        session_id,
        event_type,
        tool_sequence,
        current_tool,
        all_logged,
        already_injected,
    )

    # --- Emit result ---
    result = {
        "hookSpecificOutput": {
            "hookEventName": event_type,
            "additionalContext": context,
        }
    }
    json.dump(result, sys.stdout)
    sys.exit(0)


if __name__ == "__main__":
    main()
