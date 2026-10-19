#!/usr/bin/env python3
"""Match gptme-style lessons against conversation context and inject relevant ones.

Claude Code hook for two events:
- UserPromptSubmit: matches against user's prompt text
- PreToolUse: matches against tool input (file paths, commands, search patterns)
  AND recent transcript context (tool outputs, assistant responses)

Matching (scan, keyword/wildcard, session_categories, BM25) is delegated to
``gptme_rag.lesson_matcher``. This file is the harness wrapper: workspace
discovery, Thompson sampling, co-occurrence prediction, holdout/dropout,
session dedup, trajectory logging, and the Claude Code I/O envelope.
Keyworded memories use the shared ``gptme.memory.match`` API and the same
injection bounds, without lesson BM25 or skill-name matching.

Already-injected lessons are tracked via a session state file to avoid duplicates.

## Installation

Register in `.claude/settings.json`. Prefer the workspace venv interpreter
(this hook re-execs into `.venv/bin/python3` if invoked as system python3):

    python3 $CLAUDE_PROJECT_DIR/.claude/hooks/match-lessons.py

The hook auto-discovers your workspace root (the directory containing gptme.toml)
and reads lesson dirs from [lessons] dirs in that config file.

State directories (Thompson sampling, predictions, trajectories) are stored
under workspace/state/ and created automatically on first use.
"""

from __future__ import annotations

import inspect
import json
import math
import os
import random
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Collection

# Re-exec under the workspace venv BEFORE importing gptme-rag. Claude Code
# invokes this hook as system `python3`, which does not have gptme-rag.
# Tests import this module (not as __main__) and already run under the venv.
if __name__ == "__main__":
    _reexec_flag = "MATCH_LESSONS_VENV_REEXEC"
    if os.environ.get(_reexec_flag) != "1":
        _here = Path(__file__).resolve()
        _ws: Path | None = None
        for _p in [_here.parent, *_here.parents]:
            if (_p / "gptme.toml").exists():
                _ws = _p
                break
        if _ws is None:
            for _p in [Path.cwd(), *Path.cwd().parents]:
                if (_p / "gptme.toml").exists():
                    _ws = _p
                    break
        _venv_root = (_ws / ".venv") if _ws is not None else None
        _venv_py = (_venv_root / "bin" / "python3") if _venv_root is not None else None
        # Compare sys.prefix, not sys.executable: .venv/bin/python3 is often a
        # symlink to system python3, so resolve() would skip the re-exec and
        # the hook would run without gptme-rag on sys.path.
        _already_in_venv = (
            _venv_root is not None
            and Path(sys.prefix).resolve() == _venv_root.resolve()
        )
        if (
            _venv_py is not None
            and _venv_py.is_file()
            and os.access(_venv_py, os.X_OK)
            and not _already_in_venv
        ):
            os.environ[_reexec_flag] = "1"
            os.execv(str(_venv_py), [str(_venv_py), *sys.argv])

TRIVIAL_PROBE_PROMPTS = {"hello", "hi", "ping", "pong", "test"}
_STDIN_CACHE: str | None = None


def _is_trivial_probe_prompt(prompt: str) -> bool:
    normalized = " ".join(prompt.lower().split()).strip(" .!?")
    return normalized in TRIVIAL_PROBE_PROMPTS


def _trivial_user_prompt_event(raw: str) -> str | None:
    try:
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(hook_input, dict):
        return None
    event_type = str(hook_input.get("hook_event_name", "UserPromptSubmit"))
    if event_type != "UserPromptSubmit":
        return None
    prompt = str(hook_input.get("prompt", "") or "")
    return event_type if _is_trivial_probe_prompt(prompt) else None


# --- Emission receipts -------------------------------------------------------
# One versioned row per hook invocation that reached an output decision, written
# AFTER the stdout write+flush succeeded. ``bytes`` is the UTF-8 length of the
# exact final ``additionalContext`` string (0 for measured-empty outputs). Raw
# text is never logged. Sibling file so legacy per-document consumers of
# ``cc-session-*-lessons.jsonl`` never see aggregate rows. Emission is not proof
# the harness delivered the context. Defined before the trivial fast path so it
# can receipt without the heavy imports below.
RECEIPT_VERSION = 1


def _get_lesson_receipts_file(session_id: str) -> Path:
    """Path of the emission-receipt JSONL: /tmp/cc-session-{id}-lessons-receipts.jsonl."""
    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    return tmpdir / f"cc-session-{safe_id}-lessons-receipts.jsonl"


def _write_emission_receipt(
    session_id: str,
    hook_point: str,
    context_bytes: int,
    outcome: str,
    reason: str = "",
    event_id: str | None = None,
) -> dict | None:
    """Append one receipt. Fail-open: a logging failure never affects the hook."""
    try:
        record = {
            "record_type": "emission_receipt",
            "receipt_version": RECEIPT_VERSION,
            "event_id": event_id or uuid.uuid4().hex,
            "session_id": session_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": hook_point,
            "outcome": outcome,
            "bytes": context_bytes,
        }
        if reason:
            record["reason"] = reason
        with open(_get_lesson_receipts_file(session_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return record
    except Exception:
        return None  # Never fail the hook for logging


def _emit_context(
    event_type: str,
    session_id: str,
    context: str,
    *,
    precision_items: list[dict] | None = None,
    intent: str = "",
) -> None:
    """Write the additionalContext payload, then receipt its exact byte size."""
    result = {
        "hookSpecificOutput": {
            "hookEventName": event_type,
            "additionalContext": context,
        }
    }
    json.dump(result, sys.stdout)
    sys.stdout.flush()
    receipt = _write_emission_receipt(
        session_id, event_type, len(context.encode("utf-8")), "emitted"
    )
    # Raw text is opt-in, private, and tied to this successful final receipt.
    # Missing enrollment/intent/telemetry is unknown precision, never zero.
    private_dir = os.environ.get("BOB_LESSON_PRECISION_DIR", "")
    enrollment_dir = os.environ.get("BOB_LESSON_ENROLLMENT_REHEARSAL_DIR", "")
    if private_dir and enrollment_dir and receipt and precision_items and intent:
        try:
            from context.lesson_precision import record_items

            run_id = os.environ.get("BOB_SESSION_ID", "")
            if str(uuid.UUID(run_id)) != run_id:
                return
            enrollment = json.loads(
                (Path(enrollment_dir) / f"{run_id}.json").read_text()
            )
            if (
                enrollment.get("runtime") != "claude-code"
                or enrollment.get("run_id") != run_id
            ):
                return
            record_items(
                Path(private_dir), enrollment, receipt, intent, precision_items
            )
        except Exception:
            pass  # Private measurement failure must not change hook output.


def _emit_empty_fast(event_name: str, session_id: str | None = None) -> None:
    if event_name == "UserPromptSubmit":
        json.dump({"hookSpecificOutput": {"hookEventName": event_name}}, sys.stdout)
        sys.stdout.flush()
        if session_id:
            _write_emission_receipt(session_id, event_name, 0, "empty", "trivial_probe")


def _session_id_of(raw: str) -> str | None:
    try:
        sid = json.loads(raw).get("session_id")
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None
    return sid if isinstance(sid, str) and sid else None


if __name__ == "__main__":
    try:
        _STDIN_CACHE = sys.stdin.read()
    except OSError:
        _STDIN_CACHE = ""
    _trivial_event = _trivial_user_prompt_event(_STDIN_CACHE)
    if _trivial_event:
        _emit_empty_fast(_trivial_event, _session_id_of(_STDIN_CACHE))
        sys.exit(0)

try:
    from gptme_rag.lesson_matcher import (
        BM25_MIN_RAW as _BM25_MIN_RAW,
    )
    from gptme_rag.lesson_matcher import (
        effective_status,
        extract_frontmatter,
        filter_by_harness,
        filter_by_repo,
        filter_by_session_category,
        keyword_to_regex,
        match_keyword,
        scan_lessons,
    )
    from gptme_rag.lesson_matcher import (
        score_lessons as _upstream_score_lessons,
    )

    _GPTME_RAG_AVAILABLE = True
except ImportError:  # pragma: no cover — degrade path when venv is missing
    _GPTME_RAG_AVAILABLE = False
    _BM25_MIN_RAW = 40.0

    def effective_status(fm: dict[Any, Any]) -> str:
        return str(fm.get("metadata", {}).get("status") or fm.get("status") or "active")

    # Signatures mirror ``gptme_rag.lesson_matcher`` exactly: mypy resolves the
    # import above and rejects conditional variants whose signatures differ.
    def extract_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        return {}, content

    def filter_by_harness(
        lessons: list[dict[str, Any]], harness: str
    ) -> list[dict[str, Any]]:
        return lessons

    def filter_by_session_category(
        lessons: list[dict[str, Any]], category: str | None
    ) -> list[dict[str, Any]]:
        return lessons

    def filter_by_repo(
        lessons: list[dict[str, Any]], current_repo: str | None
    ) -> list[dict[str, Any]]:
        return lessons

    def keyword_to_regex(keyword: str) -> re.Pattern[str] | None:
        return None

    def match_keyword(keyword: str, text_lower: str) -> bool:
        return False

    def scan_lessons(lesson_dirs: list[Path]) -> list[dict[str, Any]]:
        return []

    def _upstream_score_lessons(
        lessons: list[dict[str, Any]],
        prompt: str,
        max_results: int = 5,
        *,
        use_bm25: bool = True,
        bm25_min_z: float | None = None,
        skill_bm25_min_z: float | None = None,
        skill_bm25_exempt_paths: Collection[str] = (),
    ) -> list[dict[str, Any]]:
        return []


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
# Hard internal budget. The harness timeout DISCARDS hook output; we must
# finish (or degrade to empty) before that happens. 8s leaves headroom under
# the 10s documented timeout and the 30s currently configured in settings.
HOOK_BUDGET_SECONDS = float(os.environ.get("MATCH_LESSONS_HOOK_BUDGET", "8.0"))
# State directory for cross-invocation dedup (in /tmp, not workspace)
STATE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "claude-lesson-match"

# Production advisory-skill defaults after the skill-injection-dial-v1 readout.
MAX_PROMPT_SKILLS = 2
MAX_PRETOOL_SKILLS = 1
SKILL_BM25_MIN_Z = 5.0


def _skill_bm25_supported() -> bool:
    """Only pass skill thresholds when the shared matcher supports exemptions."""
    return _GPTME_RAG_AVAILABLE and {
        "skill_bm25_min_z",
        "skill_bm25_exempt_paths",
    }.issubset(inspect.signature(_upstream_score_lessons).parameters)


def _bm25_min_z_supported() -> bool:
    """Whether the upstream score_lessons accepts the bm25_min_z override."""
    return (
        _GPTME_RAG_AVAILABLE
        and "bm25_min_z" in inspect.signature(_upstream_score_lessons).parameters
    )


def apply_skill_cap(
    matches: list[dict], predicted: list[dict], cap: int | None
) -> tuple[list[dict], list[dict]]:
    """Limit new advisory skills, never ordinary lessons, memories or guards."""
    if cap is None:
        return matches, predicted
    remaining = max(cap, 0)

    def keep(items: list[dict]) -> list[dict]:
        nonlocal remaining
        kept = []
        for item in items:
            advisory = (
                item.get("is_skill") and _classify_lesson(item["path"])[0] != "exempt"
            )
            if not advisory:
                kept.append(item)
            elif remaining:
                kept.append(item)
                remaining -= 1
        return kept

    return keep(matches), keep(predicted)


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


# --- Lesson policy manifest (Stage 1 shadow logging) ---

_policy_manifest_cache: dict | None = None


def _policy_manifest_path() -> Path:
    """Return path to lesson-policy manifest."""
    return get_workspace() / "state" / "lesson-policy" / "manifest.yaml"


def _load_policy_manifest() -> dict:
    """Load and cache the lesson-policy manifest (YAML).

    Returns dict with keys: version, updated_at, validated_core, exempt, holdout_population.
    On load failure or missing file, returns a safe default (all lessons in holdout).
    """
    global _policy_manifest_cache
    if _policy_manifest_cache is not None:
        return _policy_manifest_cache

    manifest_path = _policy_manifest_path()
    if not manifest_path.exists():
        # Manifest not yet created (Stage 0 not run) — default all to holdout
        _policy_manifest_cache = {
            "version": 1,
            "updated_at": "",
            "validated_core": [],
            "exempt": [],
            "holdout_population": ["*"],  # sentinel: treat all lessons as holdout
        }
        return _policy_manifest_cache

    try:
        import yaml

        with open(manifest_path, encoding="utf-8") as f:
            _loaded = yaml.safe_load(f)
        manifest = _loaded if isinstance(_loaded, dict) else {}
    except ImportError:
        # YAML library unavailable — log once and return safe default
        print(
            f"Warning: yaml not available; lesson-policy manifest at {manifest_path} ignored",
            file=sys.stderr,
        )
        manifest = {
            "version": 1,
            "updated_at": "",
            "validated_core": [],
            "exempt": [],
            "holdout_population": ["*"],  # sentinel: treat all lessons as holdout
        }
    except Exception as e:
        # Parse error or file read failure — return safe default
        print(
            f"Warning: failed to load lesson-policy manifest: {e}",
            file=sys.stderr,
        )
        manifest = {
            "version": 1,
            "updated_at": "",
            "validated_core": [],
            "exempt": [],
            "holdout_population": ["*"],  # sentinel: treat all lessons as holdout
        }

    _policy_manifest_cache = manifest
    return manifest


def _classify_lesson(lesson_path: str) -> tuple[str, int]:
    """Classify a lesson by its path against the policy manifest.

    Args:
        lesson_path: e.g. "lessons/patterns/persistent-learning.md"

    Returns:
        (policy_class, policy_version) where policy_class is one of:
        - "validated_core": high-ROI, recommended for all sessions
        - "exempt": safety/retention policy, exempt from dropout
        - "holdout": under evaluation (default)
        - "unknown": lesson not in manifest (created after manifest timestamp)
    """
    manifest = _load_policy_manifest()
    policy_version = manifest.get("version", 1)

    # Normalize lesson_path to the key format used in manifest.
    # lesson_path may be absolute (/home/bob/bob/lessons/X/Y.md), relative
    # (lessons/X/Y.md), or dot-relative (./lessons/X/Y.md).
    # Note: the manifest format only indexes lessons under the 'lessons/'
    # directory. Paths under other configured lesson directories (e.g.
    # 'skills/') cannot be resolved to a manifest key and will always
    # produce "unknown" (or "holdout" if the "*" sentinel is present).
    path_str = str(lesson_path).replace("\\", "/")
    if "/lessons/" in path_str:
        key = path_str.split("/lessons/", 1)[1]
    else:
        # Strip any leading ./ before checking for lessons/ prefix
        stripped = path_str.lstrip("./")
        if stripped.startswith("lessons/"):
            key = stripped[len("lessons/") :]
        else:
            # Path is not under a 'lessons/' directory — cannot be resolved
            # to a manifest key. Apply the "*" sentinel if present (the
            # missing-manifest fallback that classifies all lessons as
            # holdout), otherwise "unknown" is the correct result.
            holdout_pop = manifest.get("holdout_population", [])
            if "*" in holdout_pop:
                return "holdout", policy_version
            return "unknown", policy_version
    key = key.replace(".md", "").strip("/")

    for category in ["validated_core", "exempt"]:
        if key in manifest.get(category, []):
            return category, policy_version

    # "*" sentinel means all lessons fall into holdout (manifest missing/unavailable).
    # For a present manifest that simply lacks holdout_population, default to [] so
    # unlisted lessons are classified as "unknown", not spuriously as "holdout".
    holdout_pop = manifest.get("holdout_population", [])
    if "*" in holdout_pop or key in holdout_pop:
        return "holdout", policy_version

    # Not found in manifest — unknown (created after manifest timestamp)
    return "unknown", policy_version


# --- Lesson loading ---


def load_lesson_dirs(workspace: Path) -> list[Path]:
    """Read lesson dirs from gptme.toml [lessons] dirs."""
    toml_path = workspace / "gptme.toml"
    if not toml_path.exists():
        return [workspace / "lessons"]

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            # tomllib/tomli unavailable — warn and fall back to default lessons dir.
            # Configured dirs in gptme.toml [lessons] dirs are silently ignored.
            # Fix: install tomli (Python < 3.11) or upgrade to Python 3.11+.
            print(
                "Warning: tomllib/tomli not available; gptme.toml [lessons] dirs ignored,"
                " using default './lessons' only.",
                file=sys.stderr,
            )
            return [workspace / "lessons"]

    with open(toml_path, "rb") as f:
        cfg = tomllib.load(f)

    dirs = cfg.get("lessons", {}).get("dirs", ["lessons"])
    return [workspace / d for d in dirs]


def detect_harness() -> str:
    """Detect the current runtime harness for lesson applicability filters."""
    if os.environ.get("CLAUDECODE"):
        return "claude-code"
    if os.environ.get("CODEX") or os.environ.get("CODEX_INSTALLED"):
        return "codex"
    return "gptme"


def detect_session_category() -> str | None:
    """Detect the current session category from environment variables.

    Autonomous sessions set CASCADE_CATEGORY via autonomous-run.sh.
    Other run types may set GRADE_CATEGORY or WORKER_CATEGORY.
    Returns lowercase category string, or None if unknown.
    """
    for var in (
        "CASCADE_CATEGORY",
        "CASCADE_EXECUTION_CATEGORY",
        "GRADE_CATEGORY",
        "WORKER_CATEGORY",
    ):
        val = os.environ.get(var, "").strip()
        if val:
            return val.lower()
    return None


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


def load_prediction_model() -> dict | None:
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


def _has_routing_match(matched_by: list) -> bool:
    """True if a match carries an explicit routing signal (keyword/pattern/skill).

    Descriptor-overlap and BM25 matches are prefixed ``name:``/``description:``/
    ``tags:``/``bm25:``; a raw keyword string, ``pattern:``, or ``skill:`` entry
    means the prompt explicitly invoked the lesson. Used to gate keyword-only
    skills so generic descriptor/BM25 overlap cannot inject them.
    """
    return any(
        not m.startswith(("name:", "description:", "tags:", "bm25:"))
        for m in matched_by
    )


def _is_keywords_only_skill(path: str) -> bool:
    """True if the skill opts into keyword-only routing (``match.keywords_only``).

    Reads the file's frontmatter on demand (only called for descriptor/BM25-only
    matches, which are rare) so the flag takes effect without a gptme-rag bump.
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
        fm, _ = extract_frontmatter(content)
        match_data = fm.get("match", {})
        return (
            bool(match_data.get("keywords_only"))
            if isinstance(match_data, dict)
            else False
        )
    except Exception:
        return False


def score_lessons(
    lessons: list[dict],
    prompt: str,
    max_results: int = 5,
    bm25_index: dict | None = None,
    *,
    use_bm25: bool | None = None,
    bm25_min_z: float | None = None,
    skill_bm25_min_z: float | None = None,
) -> list[dict]:
    """Match lessons via gptme-rag, then apply Bob Thompson-sampling re-rank.

    ``bm25_index`` is accepted for backward compatibility with the pre-wrapper
    API. Upstream builds its own BM25 index; passing a dict (or omitting the
    argument) enables BM25. Pass ``use_bm25=False`` to disable it.

    TS is applied *before* the top-k cut so a high posterior can promote a
    lesson that would otherwise fall just outside ``max_results``.

    Bob-local gate: a skill that opts into ``match.keywords_only`` is only
    injected when the prompt carries an explicit routing signal (keyword,
    pattern, or skill-name match). Generic descriptor/BM25 overlap alone no
    longer injects it — this fixes marketing-tweet-announce firing on every
    long prompt that mentions tweet/marketing/launch in passing.
    """
    if not _GPTME_RAG_AVAILABLE:
        return []
    if use_bm25 is None:
        use_bm25 = True
    cap = max(max_results, len(lessons))
    skill_kwargs: dict[str, Any] = {}
    if skill_bm25_min_z is not None and _skill_bm25_supported():
        skill_kwargs = {
            "skill_bm25_min_z": skill_bm25_min_z,
            "skill_bm25_exempt_paths": {
                lesson["path"]
                for lesson in lessons
                if lesson.get("is_skill")
                and _classify_lesson(lesson["path"])[0] == "exempt"
            },
        }
    corpus_kwargs: dict[str, Any] = {}
    if bm25_min_z is not None and _bm25_min_z_supported():
        corpus_kwargs["bm25_min_z"] = bm25_min_z
    results: list[dict] = _upstream_score_lessons(
        lessons,
        prompt,
        max_results=cap,
        use_bm25=use_bm25,
        **skill_kwargs,
        **corpus_kwargs,
    )
    results = [
        r
        for r in results
        if _has_routing_match(r.get("matched_by", []))
        or not _is_keywords_only_skill(r["path"])
    ]
    if results:
        ts_means = load_ts_means([r["path"] for r in results])
        for r in results:
            ts_mean = ts_means.get(r["path"], 0.5)
            r["score"] += TS_WEIGHT * ts_mean
            r["ts_score"] = ts_mean
        results.sort(key=lambda x: -x["score"])
    return results[:max_results]


def score_memories(
    workspace: Path,
    text: str,
    max_results: int,
    *,
    project_workspace: Path | None = None,
) -> list[dict]:
    """Adapt shared keyword memory matches to the hook's injection envelope."""
    try:
        from gptme.memory import MemoryStore
        from gptme.memory.match import match_memories
    except ImportError:
        # Older gptme installs still support the existing lesson-only hook.
        return []

    # The hook lives in the agent brain, while Claude may be working in another
    # project. Preserve project precedence and include the brain as agent scope.
    env = {
        **os.environ,
        "GPTME_AGENT_WORKSPACE": os.environ.get("GPTME_AGENT_WORKSPACE")
        or str(workspace),
    }
    store = MemoryStore.from_workspace(project_workspace or workspace, env=env)
    return [
        {
            "path": str(hit.entry.path.resolve()),
            "title": hit.entry.index_title,
            "description": hit.entry.description,
            "body": hit.entry.body,
            "score": hit.score,
            "matched_by": hit.matched_by,
            "is_memory": True,
        }
        for hit in match_memories(store, text, limit=max_results)
        if hit.entry.path is not None
    ]


# --- Holdout filtering (A/B testing) ---


def parse_holdout_lessons_env(value: str | None = None) -> set[str]:
    """Parse comma-separated lesson identifiers from HOLDOUT_LESSONS env var.

    Supports multiple identifier formats: file stem, filename, full/partial path,
    lesson ID (from frontmatter ``id`` field), or parent directory name (for SKILL.md).

    Example::

        HOLDOUT_LESSONS="browser-verification,strategic/scope-discipline.md"
    """
    raw = os.environ.get("HOLDOUT_LESSONS", "") if value is None else value
    if not raw:
        return set()
    return {
        token.strip().lower().replace("\\", "/")
        for token in raw.split(",")
        if token.strip()
    }


def is_held_out_lesson(lesson: dict, holdout_lessons: set[str]) -> bool:
    """Return True if the lesson matches a configured holdout identifier."""
    if not holdout_lessons:
        return False

    path = Path(str(lesson["path"]))
    path_str = str(path).lower().replace("\\", "/")
    identifiers = {
        path_str,
        path.name.lower(),
        # For SKILL.md files, use parent dir name as identifier; otherwise file stem
        (path.parent.name if path.name.lower() == "skill.md" else path.stem).lower(),
    }

    lesson_id = lesson.get("id")
    if isinstance(lesson_id, str) and lesson_id.strip():
        identifiers.add(lesson_id.strip().lower())

    for token in holdout_lessons:
        if token in identifiers:
            return True
        # Path suffix matching for partial paths like "strategic/foo.md"
        if "/" in token or token.endswith(".md"):
            normalized = token.lstrip("./")
            if path_str == normalized or path_str.endswith(f"/{normalized}"):
                return True

    return False


def filter_held_out_lessons(
    lessons: list[dict], holdout_lessons: set[str]
) -> list[dict]:
    """Remove lessons selected for holdout from injection output."""
    if not holdout_lessons:
        return lessons
    return [
        lesson for lesson in lessons if not is_held_out_lesson(lesson, holdout_lessons)
    ]


# --- Randomized lesson-dropout for causal LOO ---
# NOTE: Mirrors gptme/lessons/auto_include.py _apply_lesson_dropout / _log_dropout.
# If dropout logic changes in gptme core, update here too (and vice versa).
# Future: import from gptme core once the CC hook can safely depend on gptme being installed.


def _get_dropout_epsilon() -> float:
    """Get lesson-dropout probability from LESSON_DROPOUT_EPSILON env var (default 0.0)."""
    raw = os.environ.get("LESSON_DROPOUT_EPSILON")
    if raw is None:
        return 0.0
    try:
        epsilon = float(raw)
    except ValueError:
        return 0.0
    if not math.isfinite(epsilon) or epsilon <= 0.0:
        return 0.0
    return min(epsilon, 1.0)


def _get_dropout_epsilon_validated_core() -> float:
    """Get dropout epsilon for validated_core lessons (default 0.05).

    Only meaningful when global LESSON_DROPOUT_EPSILON > 0 (the global switch activates
    the dropout system; this controls how aggressively validated_core lessons are sampled).
    Override with LESSON_DROPOUT_EPSILON_VALIDATED_CORE env var.
    """
    raw = os.environ.get("LESSON_DROPOUT_EPSILON_VALIDATED_CORE")
    if raw is None:
        return 0.05
    try:
        epsilon = float(raw)
    except ValueError:
        return 0.05
    if not math.isfinite(epsilon):
        return 0.05
    return min(max(epsilon, 0.0), 1.0)


def _get_dropout_epsilon_for_class(policy_class: str, global_epsilon: float) -> float:
    """Return the effective dropout probability for a given policy class.

    - exempt: always 0.0 (never withheld, regardless of global_epsilon)
    - validated_core: min(LESSON_DROPOUT_EPSILON_VALIDATED_CORE, global_epsilon)
    - holdout / unknown: global_epsilon (LESSON_DROPOUT_EPSILON)

    When global_epsilon=0 the dropout system is off; validated_core is also 0.0
    so that the global switch is a clean disable, not a class-level one.
    validated_core epsilon is bounded by global_epsilon so a gentle global rate
    cannot withhold validated_core lessons at a higher rate than ordinary ones.
    """
    if policy_class == "exempt":
        return 0.0
    if global_epsilon <= 0.0:
        return 0.0
    if policy_class == "validated_core":
        return min(_get_dropout_epsilon_validated_core(), global_epsilon)
    return global_epsilon


def _get_dropout_log_dir(workspace: Path) -> Path:
    """Dropout log directory (LESSON_DROPOUT_LOG_DIR or workspace/state/lesson-dropout)."""
    raw = os.environ.get("LESSON_DROPOUT_LOG_DIR")
    return Path(raw) if raw else workspace / "state" / "lesson-dropout"


def _assignment_origin(lesson: dict) -> str:
    """Return the candidate origin for an assignment receipt.

    ``predicted`` is any co-occurrence candidate; everything else is a direct
    keyword/pattern/semantic match. Origin is recorded at roll time so later
    formatting cannot erase how the candidate entered the pool.
    """
    if lesson.get("predicted") is True:
        return "predicted"
    matched_by = lesson.get("matched_by") or []
    if isinstance(matched_by, list) and any(
        isinstance(token, str) and token.startswith("predicted") for token in matched_by
    ):
        return "predicted"
    return "keyword"


def _assignment_lesson_record(lesson: dict, *, effective_epsilon: float) -> dict:
    """Compact per-lesson assignment fields shared by both dropout arms."""
    origin = _assignment_origin(lesson)
    return {
        "path": lesson.get("path", ""),
        "title": lesson.get("title", ""),
        "effective_epsilon": effective_epsilon,
        "origin": origin,
        "predicted": origin == "predicted",
    }


def _enrich_assignment_arm(lessons: list[dict], *, assignment_id: str) -> list[dict]:
    """Attach policy class/version and the shared assignment identity."""
    enriched: list[dict] = []
    for lesson in lessons:
        policy_class, policy_version = _classify_lesson(lesson.get("path", ""))
        record = dict(lesson)
        record["policy_class"] = policy_class
        record["policy_version"] = policy_version
        record["assignment_id"] = assignment_id
        enriched.append(record)
    return enriched


def _log_dropout(
    session_id: str,
    epsilon: float,
    withheld: list[dict],
    workspace: Path,
    *,
    kept: list[dict] | None = None,
    event_type: str | None = None,
    assignment_id: str | None = None,
) -> None:
    """Append a dropout record for causal LOO analysis. Failures are swallowed.

    Both arms are logged here, before formatting/dedup. Extra fields are
    additive so legacy withheld-only readers keep working.
    """
    try:
        log_dir = _get_dropout_log_dir(workspace)
        log_dir.mkdir(parents=True, exist_ok=True)

        assignment_id = assignment_id or str(uuid.uuid4())
        enriched_withheld = _enrich_assignment_arm(
            withheld, assignment_id=assignment_id
        )
        enriched_matched = _enrich_assignment_arm(
            kept or [], assignment_id=assignment_id
        )

        # Get policy manifest version from the manifest itself
        manifest = _load_policy_manifest()
        manifest_version = manifest.get("version", 1)

        record = {
            "ts": time.time(),
            "session_id": session_id,
            "epsilon": epsilon,
            "policy_version": manifest_version,
            "assignment_id": assignment_id,
            "withheld": enriched_withheld,
            "matched": enriched_matched,
        }
        if event_type:
            record["event"] = event_type
        with open(log_dir / f"{session_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _apply_lesson_dropout(
    lessons: list[dict], session_id: str, workspace: Path
) -> list[dict]:
    """Randomly withhold lessons for causal leave-one-out analysis.

    Controlled by LESSON_DROPOUT_EPSILON (float [0,1]). Default 0 = no-op.
    Uses class-aware epsilon: validated_core uses LESSON_DROPOUT_EPSILON_VALIDATED_CORE
    (default 0.05), exempt is never withheld.

    Does NOT log — logging is done by _apply_lesson_dropout_multi which
    collects withheld lessons from both pools into one unified record.
    """
    epsilon = _get_dropout_epsilon()
    if epsilon <= 0.0 or not lessons:
        return lessons

    kept: list[dict] = []
    for lesson in lessons:
        policy_class, _ = _classify_lesson(lesson.get("path", ""))
        eff_epsilon = _get_dropout_epsilon_for_class(policy_class, epsilon)
        if eff_epsilon > 0.0 and random.random() < eff_epsilon:
            pass  # Withheld — skip
        else:
            kept.append(lesson)

    return kept


def _apply_lesson_dropout_multi(
    matches: list[dict],
    predicted: list[dict],
    session_id: str,
    workspace: Path,
    *,
    event_type: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Apply class-aware dropout to both matches and predicted, logging ONE unified record.

    Processes both pools together so that the analysis script receives a
    single JSONL record per hook invocation containing both assignment arms,
    not two split records and not a keep arm that waits for formatting.

    Class-aware epsilons (all require global LESSON_DROPOUT_EPSILON > 0 to activate):
    - exempt: 0.0 (never withheld regardless of global epsilon)
    - validated_core: LESSON_DROPOUT_EPSILON_VALIDATED_CORE (default 0.05)
    - holdout / unknown: LESSON_DROPOUT_EPSILON (global epsilon)
    """
    epsilon = _get_dropout_epsilon()

    # Always log when epsilon>0, even if both pools are empty, so
    # lesson-loo-analysis.py can distinguish treatment-group sessions
    # (epsilon>0, possibly 0 withheld) from control-group sessions (epsilon=0).
    if epsilon <= 0.0:
        return matches, predicted

    # Guard: if the policy manifest is unavailable or broken, _classify_lesson
    # returns the "*" sentinel (all lessons as "holdout").  Classification is
    # unreliable in this state — we cannot guarantee that exempt lessons are
    # preserved, so skip dropout entirely to fail safe.
    manifest = _load_policy_manifest()
    if "*" in manifest.get("holdout_population", []):
        _log_dropout(session_id, epsilon, [], workspace, kept=[], event_type=event_type)
        return matches, predicted

    withheld: list[dict] = []
    kept_records: list[dict] = []
    assignment_id = str(uuid.uuid4())

    def _do_dropout(lessons: list[dict]) -> list[dict]:
        kept: list[dict] = []
        for lesson in lessons:
            policy_class, _ = _classify_lesson(lesson.get("path", ""))
            eff_epsilon = _get_dropout_epsilon_for_class(policy_class, epsilon)
            entry = _assignment_lesson_record(lesson, effective_epsilon=eff_epsilon)
            if eff_epsilon > 0.0 and random.random() < eff_epsilon:
                withheld.append(entry)
            else:
                kept_records.append(entry)
                kept.append(lesson)
        return kept

    kept_matches = _do_dropout(matches)
    kept_predicted = _do_dropout(predicted)

    _log_dropout(
        session_id,
        epsilon,
        withheld,
        workspace,
        kept=kept_records,
        event_type=event_type,
        assignment_id=assignment_id,
    )
    return kept_matches, kept_predicted


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


_HEREDOC_RE = re.compile(
    r"(?P<head>(?<!<)<<-?(?!<)\s*(?P<q>['\"]?)(?P<word>[A-Za-z_]\w*)(?P=q)[^\n]*)\n"
    r"(?:.*?^[ \t]*(?P=word)[ \t]*$|.*\Z)",
    re.DOTALL | re.MULTILINE,
)


def _strip_quoted_spans(text: str) -> str:
    """Remove single/double-quoted string literals and heredoc bodies from shell text.

    Quoted spans in bash commands are data (grep patterns, python strings, etc.),
    not intent. Stripping them prevents false lesson matches triggered by literals
    inside commands like ``grep 'SKILL\\|skill'`` or ``python -c "import foo"``.

    Heredoc bodies (``<< 'EOF' ... EOF``) are also stripped for the same reason.
    The unquoted command tokens (paths, subcommands) are preserved.
    """
    # Heredoc bodies: <<[-]['"]WORD['"] ... up to a line that is exactly WORD.
    # The operator line itself is kept (it holds the real command); only the body
    # and terminator go. An unterminated heredoc drops everything after its line.
    text = _HEREDOC_RE.sub(r"\g<head>", text)
    # Double-quoted spans (handle backslash escapes inside them)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', " ", text)
    # Single-quoted spans (no escapes inside single quotes in POSIX shell)
    text = re.sub(r"'[^']*'", " ", text)
    return text


def build_pretool_match_text(tool_name: str, tool_input: dict) -> str:
    """Build match text from PreToolUse tool name and input fields."""
    parts = []

    # Extract relevant fields from tool input.
    # `command` is handled separately: quoted literals are stripped first because
    # grep patterns, python -c strings, etc. are data not intent and dominate the
    # false-match class (measured in session 72ce: every misfire was a quoted literal).
    for key in (
        "file_path",
        "pattern",
        "prompt",
        "query",
        "url",
        "description",
    ):
        val = tool_input.get(key)
        if val and isinstance(val, str):
            parts.append(val)

    command = tool_input.get("command")
    if command and isinstance(command, str):
        parts.append(_strip_quoted_spans(command))

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


def emit_empty(
    event_name: str, session_id: str | None = None, reason: str = ""
) -> None:
    """Emit empty hook output (no context to inject).

    With ``session_id``, also records a measured-zero emission receipt.
    """
    # For PreToolUse, exit 0 with no output is cleanest — doesn't interfere
    # with permissions. For UserPromptSubmit, we emit the standard structure.
    if event_name == "UserPromptSubmit":
        json.dump(
            {"hookSpecificOutput": {"hookEventName": event_name}},
            sys.stdout,
        )
        sys.stdout.flush()
    if session_id:
        _write_emission_receipt(session_id, event_name, 0, "empty", reason)


def _is_skill_path(path: str) -> bool:
    """True if the matched file is a SKILL.md (pointer-inject on PreToolUse)."""
    return Path(path).name.lower() == "skill.md"


def _render_lesson(m: dict, event_type: str, predicted: bool = False) -> list[str]:
    """Render one matched lesson as context lines.

    On PreToolUse, whole SKILL.md documents are rendered as a compact pointer
    (name + description + path) instead of the full body — a ~3,000-token bet
    on a low-confidence trigger. Lessons (small by design, 30-50 lines) and all
    UserPromptSubmit matches keep full-body injection.
    """
    suffix = " (predicted)" if predicted else ""
    if event_type == "PreToolUse" and _is_skill_path(m["path"]):
        desc = m.get("description", "").strip()
        lines = [f"### {m['title']}{suffix} (skill pointer)"]
        lines.append(f"*Source: {m['path']}*")
        if desc:
            lines.append(f"*Description: {desc}*")
        lines.append(f"*Read the full skill at `{m['path']}` when relevant.*")
        return lines
    return [
        f"### {m['title']}{suffix}",
        f"*Source: {m['path']}*\n",
        m["body"],
        "",
    ]


def format_lessons(
    matches: list[dict],
    already_injected: set[str],
    predicted: list[dict] | None = None,
    event_type: str = "UserPromptSubmit",
) -> str:
    """Format matched + predicted lessons as markdown context."""
    parts: list[str] = []

    for m in matches:
        if m["path"] in already_injected:
            continue
        parts.extend(_render_lesson(m, event_type))

    # Add predicted lessons (from co-occurrence model)
    if predicted:
        for p in predicted:
            if p["path"] in already_injected:
                continue
            parts.extend(_render_lesson(p, event_type, predicted=True))

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


def _get_lesson_events_file(session_id: str) -> Path:
    """Get the path to the lesson events JSONL file for this session.

    Location: /tmp/cc-session-{id}-lessons.jsonl
    Format: append-only, one JSON event per line
    """
    tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
    # Sanitize session_id for filesystem safety
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", session_id)
    return tmpdir / f"cc-session-{safe_id}-lessons.jsonl"


def _log_lesson_events(
    session_id: str,
    event_type: str,
    matches: list[dict],
    predicted: list[dict],
    already_injected: set[str],
) -> None:
    """Log structured lesson injection events for efficacy measurement.

    Appends to /tmp/cc-session-{id}-lessons.jsonl with:
    - lesson_name: human-readable lesson title
    - lesson_file: path to .md file
    - match_type: keyword, pattern, skill_name, descriptor, semantic, or predicted
    - injection_point: UserPromptSubmit or PreToolUse (hook name)
    - sequence_order: ordinal position in this injection (1, 2, 3, ...)
    - timestamp_inject: ISO8601 UTC timestamp when lesson was matched

    Never fails the hook for logging failures.
    """
    events_to_log = []

    # Log keyword/pattern/semantic matches
    for i, lesson in enumerate(matches, start=1):
        if lesson["path"] in already_injected:
            continue

        # Determine match_type from matched_by field
        matched_by = lesson.get("matched_by", [])
        match_type = "unknown"
        if matched_by:
            first_match = matched_by[0].lower()
            if "skill:" in first_match:
                match_type = "skill_name"
            elif "pattern:" in first_match:
                match_type = "pattern"
            elif "bm25:" in first_match:
                match_type = "semantic"
            elif "descriptor:" in first_match or first_match.startswith(
                ("name:", "description:", "tags:")
            ):
                # Skill descriptor-overlap matches (upstream _score_skill_descriptor)
                match_type = "descriptor"
            else:
                # Default to keyword for simple matches
                match_type = "keyword"

        events_to_log.append(
            {
                "lesson_name": lesson.get("title", ""),
                "lesson_file": lesson.get("path", ""),
                "match_type": match_type,
                "injection_point": event_type,
                "sequence_order": i,
                "timestamp_inject": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    # Log predicted lessons
    for i, lesson in enumerate(predicted, start=len(matches) + 1):
        if lesson["path"] in already_injected:
            continue

        events_to_log.append(
            {
                "lesson_name": lesson.get("title", ""),
                "lesson_file": lesson.get("path", ""),
                "match_type": "predicted",
                "injection_point": event_type,
                "sequence_order": i,
                "timestamp_inject": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    if not events_to_log:
        return

    # Append events to the session's lesson events file
    try:
        events_file = _get_lesson_events_file(session_id)
        with open(events_file, "a", encoding="utf-8") as f:
            for event in events_to_log:
                f.write(json.dumps(event) + "\n")
    except Exception:
        pass  # Never fail the hook for logging


def _arm_hook_budget(seconds: float) -> None:
    """Hard wall-clock budget. A timed-out hook's stdout is discarded."""
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return

    def _handler(_signum, _frame):
        raise TimeoutError("match-lessons hook budget exceeded")

    signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)


def _disarm_hook_budget() -> None:
    if hasattr(signal, "setitimer"):
        signal.setitimer(signal.ITIMER_REAL, 0.0)


def _selector_settings() -> dict:
    """Only global user configuration may authorize selector execution/spend."""
    try:
        from gptme.config import load_user_config

        return load_user_config().env
    except Exception:
        return {}


def _selector_exempt(item: dict, guards: set[str]) -> bool:
    """Canonical protected-item policy for selection and final precision evidence."""
    path = Path(str(item["path"]).lower())
    return bool(
        item.get("is_memory")
        or item.get("always_apply")
        or item.get("alwaysApply")
        or str(item.get("priority", "")).lower() == "critical"
        or _classify_lesson(item["path"])[0] == "exempt"
        or any(
            guard in (str(path), path.name, path.stem, path.parent.name)
            or str(path).endswith("/" + guard.lstrip("./"))
            for guard in guards
        )
    )


_SELECTOR_EXPERIMENT = "shared-lesson-selector-v1"
_SELECTOR_LIVE_SUFFIX = ".live.json"


def _check_enrollment_admission(enrollment_dir: str, run_id: str) -> bool:
    """Return True only if this session is an authorized treatment arm.

    Controls, rehearsal arms, and unenrolled sessions always return False so
    the shared service is never called — zero calls even when global selector
    settings are configured.  Reads two immutable files written by
    shared_lesson_enrollment.py; never modifies state.
    """
    try:
        if str(uuid.UUID(run_id)) != run_id:
            return False
        directory = Path(enrollment_dir)
        enrollment_path = directory / f"{run_id}.json"
        if not enrollment_path.exists():
            return False
        enrollment = json.loads(enrollment_path.read_text())
        if (
            enrollment.get("experiment") != _SELECTOR_EXPERIMENT
            or enrollment.get("run_id") != run_id
            or enrollment.get("arm") != "treatment"
        ):
            return False  # controls always skip the service
        live_path = directory / f"{run_id}{_SELECTOR_LIVE_SUFFIX}"
        if not live_path.exists():
            return False
        live_auth = json.loads(live_path.read_text())
        return (
            live_auth.get("experiment") == _SELECTOR_EXPERIMENT
            and live_auth.get("run_id") == run_id
            and live_auth.get("arm") == "treatment"
            and live_auth.get("selection_authorized") is True
        )
    except Exception:
        return False


def select_advisory_lessons(
    matches: list[dict], predicted: list[dict], hook_input: dict
) -> tuple[list[dict], list[dict]]:
    """Opt-in transport to the shared selector; preserve guards and memories."""
    settings = _selector_settings()
    command_json = settings.get("LESSONS_SELECTOR_COMMAND", "")
    guards = {
        item.strip().lower().replace("\\", "/")
        for item in settings.get("LESSONS_SELECTOR_GUARDS", "").split(",")
        if item.strip()
    }
    if not command_json or not guards or os.name != "posix":
        return matches, predicted
    session_id = hook_input.get("session_id")
    if not isinstance(session_id, str) or not session_id or session_id == "unknown":
        return matches, predicted

    # Gate: only authorized treatment arms reach the shared service.
    # Controls and rehearsal/unenrolled sessions use the deterministic matcher
    # and make zero selection calls even when global selector settings exist.
    _run_id = os.environ.get("BOB_SESSION_ID", "")
    _enrollment_dir = os.environ.get("BOB_LESSON_ENROLLMENT_REHEARSAL_DIR", "")
    if not _check_enrollment_admission(_enrollment_dir, _run_id):
        return matches, predicted

    advisory = {
        item["path"]: item
        for item in matches + predicted
        if not _selector_exempt(item, guards)
    }
    if not advisory:
        return matches, predicted
    try:
        command = json.loads(command_json)
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            return matches, predicted
        timeout = float(settings.get("LESSONS_SELECTOR_TIMEOUT", "2"))
        if not math.isfinite(timeout) or not 0 < timeout <= min(
            30, HOOK_BUDGET_SECONDS / 2
        ):
            return matches, predicted
        max_calls = int(settings.get("LESSONS_SELECTOR_MAX_CALLS", "0"))
        model = settings.get("LESSONS_SELECTOR_MODEL")
        if not model or not 0 < max_calls <= 10:
            return matches, predicted
        state = load_session_state(session_id)
        prompt_event = hook_input.get("hook_event_name") == "UserPromptSubmit"
        query = (
            hook_input.get("prompt", "")
            if prompt_event
            else state.get("selector_query", "")
        )
        if not isinstance(query, str) or not query.strip():
            return matches, predicted
        if prompt_event:
            state["selector_query"] = query
            save_session_state(session_id, state)
        request = {
            "version": 1,
            "runtime": "claude-code",
            "session_id": session_id,
            "event_kind": "user_prompt" if prompt_event else "tool",
            "query": query,
            "context_fingerprint": json.dumps(
                {
                    "workspace": str(get_workspace()),
                    "repo": os.environ.get("GPTME_CURRENT_REPO", ""),
                    "category": detect_session_category(),
                },
                sort_keys=True,
            ),
            "policy_version": json.dumps(
                {
                    "adapter": 1,
                    "guards": sorted(guards),
                    "manifest": _load_policy_manifest().get("version", 1),
                }
            ),
            "candidates": [
                {
                    "id": path,
                    "description": item.get("description") or item.get("title", ""),
                    "metadata": {
                        "keywords": item.get("keywords", []),
                        "tags": item.get("tags", []),
                    },
                }
                for path, item in advisory.items()
            ],
            "fallback_ids": list(advisory),
            "max_results": 3,
            "model": model,
            "max_calls_per_session": max_calls,
            "timeout_seconds": timeout,
        }
        payload = json.dumps(request).encode()
        if len(payload) > 1_048_576:
            return matches, predicted
        # Bound both elapsed time and bytes while draining the CLI output.
        with tempfile.TemporaryFile() as source:
            source.write(payload)
            source.seek(0)
            process = subprocess.Popen(
                command,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            raw = bytearray()
            deadline = time.monotonic() + timeout + 0.25
            timed_out = False
            try:
                assert process.stdout is not None
                with process.stdout, selectors.DefaultSelector() as poller:
                    poller.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not poller.select(remaining):
                            timed_out = True
                            raise TimeoutError("selector deadline exceeded")
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > 1_048_576:
                            raise ValueError("selector output too large")
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
            finally:
                # Kill the whole group on timeout even when the direct child has
                # already exited: a grandchild can inherit the stdout pipe and
                # keep this read loop blocked until the deadline. Leaving it
                # running would stall every subsequent invocation.
                if process.returncode is None or timed_out:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
            if process.returncode:
                return matches, predicted
            result = json.loads(raw)
        if (
            not isinstance(result, dict)
            or type(result.get("version")) is not int
            or result["version"] != 1
        ):
            return matches, predicted
        status, ids = result.get("status"), result.get("ids")
        if status not in ("selected", "empty") or not isinstance(ids, list):
            return matches, predicted
        if any(not isinstance(item, str) or item not in advisory for item in ids):
            return matches, predicted
        if (status == "empty" and ids) or (status == "selected" and not ids):
            return matches, predicted
        selected = set(dict.fromkeys(ids))
        if len(selected) != len(ids) or len(selected) > request["max_results"]:
            return matches, predicted
        return (
            [
                item
                for item in matches
                if item["path"] not in advisory or item["path"] in selected
            ],
            [
                item
                for item in predicted
                if item["path"] not in advisory or item["path"] in selected
            ],
        )
    except (
        OSError,
        ValueError,
        TypeError,
        RecursionError,
        subprocess.TimeoutExpired,
        TimeoutError,
    ):
        return matches, predicted


def main():
    # Read hook input from stdin
    try:
        raw_input = _STDIN_CACHE if _STDIN_CACHE is not None else sys.stdin.read()
        hook_input = json.loads(raw_input)
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    # Detect event type from hook_event_name (available in all hook inputs)
    event_type = hook_input.get("hook_event_name", "UserPromptSubmit")
    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path")
    # Preserve user intent before all early returns (including no-match).
    private_dir = os.environ.get("BOB_LESSON_PRECISION_DIR", "")
    if private_dir and event_type == "UserPromptSubmit":
        try:
            from context.lesson_precision import save_intent

            save_intent(Path(private_dir), session_id, hook_input.get("prompt", ""))
        except Exception:
            pass  # Missing private evidence never changes matching/output.

    # --- Build match text based on event type ---
    if event_type == "UserPromptSubmit":
        match_text = hook_input.get("prompt", "")
        max_results = MAX_PROMPT_LESSONS

    elif event_type == "PreToolUse":
        # Throttle: skip if we matched recently
        state = load_session_state(session_id)
        elapsed = time.time() - state.get("last_pretool", 0)
        if elapsed < PRETOOL_COOLDOWN_SECONDS:
            emit_empty(event_type, session_id, "cooldown")
            sys.exit(0)

        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input", {})
        if not isinstance(tool_input, dict):
            emit_empty(event_type, session_id, "invalid_tool_input")
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
        emit_empty(event_type, session_id, "empty_match_text")
        sys.exit(0)

    # --- Scan and match lessons and keyworded memories ---
    workspace = get_workspace()
    lesson_dirs = load_lesson_dirs(workspace)
    lessons = scan_lessons(lesson_dirs)
    lessons = filter_by_harness(lessons, detect_harness())
    session_cat = detect_session_category()
    lessons = filter_by_session_category(lessons, session_cat)
    current_repo = os.environ.get("GPTME_CURRENT_REPO") or None
    lessons = filter_by_repo(lessons, current_repo)
    holdout_lessons = parse_holdout_lessons_env()

    score_kwargs: dict = {"skill_bm25_min_z": SKILL_BM25_MIN_Z}
    # Corpus has grown ~2× since z=4.0 was calibrated (450→930 lessons).
    # Raise the gate to suppress weak BM25-only matches (precision regression fix).
    score_kwargs["bm25_min_z"] = 6.0
    raw_matches = score_lessons(
        lessons, match_text, max_results=max_results, **score_kwargs
    )
    project_cwd = hook_input.get("cwd")
    project_workspace = (
        Path(project_cwd)
        if isinstance(project_cwd, str) and Path(project_cwd).is_absolute()
        else Path.cwd()
    )
    raw_matches.extend(
        score_memories(
            workspace, match_text, max_results, project_workspace=project_workspace
        )
    )
    raw_matches.sort(key=lambda match: -match["score"])
    # A configured lesson directory can also expose a memory's physical file.
    # Keep one candidate across both sources before applying the shared cap.
    unique_matches: dict[Path, dict] = {}
    for match in raw_matches:
        existing_match = unique_matches.setdefault(Path(match["path"]).resolve(), match)
        if match.get("is_memory"):
            existing_match["is_memory"] = True
    raw_matches = list(unique_matches.values())[:max_results]
    if event_type == "PreToolUse":
        # Arm the cooldown only once the scan/match work has completed. Empty
        # injection is the common case, so arm before the no-match early exit
        # (otherwise the cooldown never fires and every tool call re-scans at
        # full price). Arming after the work — not before it — means a hook
        # timeout cannot suppress the next invocation for the cooldown window.
        state = load_session_state(session_id)
        state["last_pretool"] = time.time()
        save_session_state(session_id, state)
    if not raw_matches:
        emit_empty(event_type, session_id, "no_match")
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

    matches, predicted = select_advisory_lessons(matches, predicted, hook_input)

    # --- Randomized dropout for causal LOO (mirrors gptme/lessons/auto_include.py) ---
    # Log both arms here, before format_lessons / already-injected filtering.
    matches, predicted = _apply_lesson_dropout_multi(
        matches, predicted, session_id, workspace, event_type=event_type
    )

    skill_cap = (
        MAX_PROMPT_SKILLS if event_type == "UserPromptSubmit" else MAX_PRETOOL_SKILLS
    )
    matches = [m for m in matches if m["path"] not in already_injected]
    predicted = [p for p in predicted if p["path"] not in already_injected]
    matches, predicted = apply_skill_cap(matches, predicted, skill_cap)

    # --- Log structured lesson events for efficacy measurement (Phase 1a) ---
    _log_lesson_events(session_id, event_type, matches, predicted, already_injected)

    context = format_lessons(
        matches, already_injected, predicted, event_type=event_type
    )

    if not context:
        emit_empty(event_type, session_id, "all_suppressed")
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
    # Both control and treatment capture only post-dedup/dropout/cap items.
    # Latest user intent is retained before output; tool literals are not intent.
    precision_items = None
    intent = ""
    if os.environ.get("BOB_LESSON_PRECISION_DIR"):
        try:
            from context.lesson_precision import load_intent

            intent = load_intent(
                Path(os.environ["BOB_LESSON_PRECISION_DIR"]), session_id
            )
        except Exception:
            intent = ""
        guards = {
            item.strip().lower().replace("\\", "/")
            for item in _selector_settings()
            .get("LESSONS_SELECTOR_GUARDS", "")
            .split(",")
            if item.strip()
        }
        # Missing protected-set configuration leaves precision unknown.
        if guards:
            precision_items = [
                {
                    "item_id": item["path"],
                    "text": "\n".join(
                        _render_lesson(item, event_type, predicted=is_predicted)
                    ),
                }
                for items, is_predicted in ((matches, False), (predicted, True))
                for item in items
                if not _selector_exempt(item, guards)
            ]
    _emit_context(
        event_type, session_id, context, precision_items=precision_items, intent=intent
    )
    sys.exit(0)


if __name__ == "__main__":
    try:
        _arm_hook_budget(HOOK_BUDGET_SECONDS)
        main()
    except TimeoutError:
        # Degrade to empty rather than letting the harness discard a partial
        # JSON payload. emit_empty is fail-open: no injection is better than
        # a hung hook. Recover the event type from the cached stdin read at
        # startup: main() consumes that cache rather than sys.stdin, so it is
        # still readable here. A PreToolUse timeout must emit no payload, not
        # a UserPromptSubmit-shaped one.
        event = "UserPromptSubmit"
        try:
            cached_input = _STDIN_CACHE or ""
            event = json.loads(cached_input).get("hook_event_name") or event
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            pass
        emit_empty(event)
        sys.exit(0)
    finally:
        _disarm_hook_budget()
