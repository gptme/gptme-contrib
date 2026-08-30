"""
gptme fallback backend for journal summarization.

Used when the `claude -p` backend fails on quota/auth exhaustion, so daily (and
weekly/monthly) summary generation does not hard-fail when the active Claude
subscription is at its weekly quota. Invokes the `gptme` CLI in non-interactive
mode against a configured model and returns the raw assistant response text.

The caller (cc_backend) owns the retry/failure policy; this module is a thin,
best-effort adapter that never raises — it returns an empty string on any
failure so the caller can degrade gracefully.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from json_repair import repair_json

logger = logging.getLogger(__name__)

# Env switches to enable the fallback or pin a specific model.
# The fallback is OFF by default because it sends journal content (potentially
# sensitive personal data) to an external model via OpenRouter. Operators must
# explicitly opt in by setting _ENABLE_ENV=1.
_ENABLE_ENV = "GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK"  # set to "1" to enable
_MODEL_ENV = "GPTME_ACTIVITY_SUMMARY_GPTME_MODEL"  # override the default model
_DEFAULT_MODEL = "openrouter/deepseek/deepseek-v4-flash-0731@deepseek"

# gptme emits one JSON object per line on stdout in --output-format json. The
# JSON answer appears in the first assistant message after the user prompt; the
# trailing auto-reply ("No tool call detected ... use the `complete` tool") and
# the `complete` tool-call message are not valid JSON, so extract_json handles
# them. We still pick the assistant content greedily and let the caller's
# extract_json_from_response pull the real JSON out of whatever text survives.
_OUTPUT_FORMAT = "json"


def is_enabled() -> bool:
    """Whether the gptme fallback is enabled (off by default, on via env).

    Set GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK=1 to enable. The fallback sends
    journal content to an external model via OpenRouter; it is disabled by
    default to avoid unintended data exfiltration.
    """
    val = os.environ.get(_ENABLE_ENV, "0").strip().lower()
    return val in ("1", "true", "yes", "on")


def call_gptme(prompt: str, timeout: int = 120) -> str:
    """
    Run ``gptme -n --output-format json`` against the fallback model.

    Best-effort: returns ``""`` on any failure (binary missing, non-zero exit,
    timeout, unparseable output) so callers can degrade gracefully.

    Args:
        prompt: The prompt to send to gptme.
        timeout: Maximum time to wait for a response (seconds).

    Returns:
        The raw assistant response text, or ``""`` if the call failed.
    """
    if not is_enabled():
        logger.debug("gptme fallback disabled; set %s=1 to enable", _ENABLE_ENV)
        return ""
    if shutil.which("gptme") is None:
        logger.debug("gptme binary not found on PATH; skipping fallback")
        return ""

    model = os.environ.get(_MODEL_ENV, _DEFAULT_MODEL).strip()
    logger.warning(
        "gptme fallback: sending prompt to external model %r via OpenRouter.",
        model,
    )
    cmd = [
        "gptme",
        "-n",  # non-interactive; implies --no-confirm
        "--no-stream",
        "--output-format",
        _OUTPUT_FORMAT,
        "--tools",
        "none",
        "-m",
        model,
        # Prompt is passed via stdin (not as a positional arg) to avoid exposing
        # potentially-sensitive journal content in the process list and to prevent
        # OSError "Argument list too long" for large journal summaries.
    ]

    env = os.environ.copy()
    # Prevent recursion / contamination from a parent gptme invocation.
    env.pop("GPTME_SUBPROCESS", None)
    # Nested gptme must not inherit the parent session's log dir or name.
    # Incident 2026-08-30: a parent autonomous session had
    # GPTME_LOGS_HOME=/tmp/gptme-logs-<id> and GPTME_NAME=autonomous-<id>;
    # the fallback appended its JSON summary into that conversation.jsonl
    # and stdout then mixed parent+child assistant messages so
    # _extract_assistant_text preferred a completion-ack over the JSON.
    for key in (
        "GPTME_LOGS_HOME",
        "GPTME_NAME",
        "GPTME_AGENT_NAME",
        "GPTME_WORKSPACE",
    ):
        env.pop(key, None)

    # Create the private log dir inside the try so a full/unwritable temp
    # filesystem returns "" instead of raising (never-raise contract).
    isolated_logs = None
    try:
        isolated_logs = Path(tempfile.mkdtemp(prefix="gptme-activity-summary-"))
        env["GPTME_LOGS_HOME"] = str(isolated_logs)
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("gptme fallback failed to run: %s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001 - best-effort adapter must not raise
        logger.warning("gptme fallback errored unexpectedly: %s", exc)
        return ""
    finally:
        if isolated_logs is not None:
            shutil.rmtree(isolated_logs, ignore_errors=True)

    if result.returncode != 0:
        logger.warning(
            "gptme fallback exited %d: stderr=%s stdout=%s",
            result.returncode,
            (result.stderr or "")[:300],
            (result.stdout or "")[:300],
        )
        return ""

    return _extract_assistant_text(result.stdout)


_THINK_TAG_RE = re.compile(r"<think>[\s\S]*?</think>", re.DOTALL)
# gptme's no-tool-call auto-reply. Shared prefix of both variants in
# gptme.tools.complete ("No tool call detected" / "... in last message").
_GPTME_TRAILER_MARKER = "No tool call detected"
_COMPLETE_TOOL_FENCE_RE = re.compile(r"^```complete\s*```$", re.DOTALL)
_SUMMARY_KEYS = ("narrative", "month_narrative")


def _is_gptme_trailer(text: str) -> bool:
    """True if this assistant message is gptme's no-tool-call auto-reply.

    After a successful JSON answer gptme injects a later non-JSON assistant
    message ("No tool call detected ... use the `complete` tool"). That
    trailer is not a better summary; preferring it over a JSON object that
    already carries ``narrative``/``month_narrative`` re-breaks the
    daily-summary fallback.
    """
    return _GPTME_TRAILER_MARKER in text or bool(_COMPLETE_TOOL_FENCE_RE.fullmatch(text.strip()))


def _last_non_trailer(messages: list[str]) -> str:
    """Last message that is not gptme's no-tool-call auto-reply.

    Falls back to ``messages[-1]`` if every message is a trailer (or the
    list is empty, which the caller already guards).
    """
    for message in reversed(messages):
        if not _is_gptme_trailer(message):
            return message
    return messages[-1] if messages else ""


def _strip_think_tags(text: str) -> str:
    """Strip <think>...</think> reasoning blocks from model output.

    Some reasoning models (e.g. DeepSeek) embed internal reasoning in
    <think>...</think> blocks before the actual response. These blocks may
    themselves contain JSON-shaped text that confuses downstream JSON
    extraction; stripping them before processing avoids greedy-regex traps.
    """
    return _THINK_TAG_RE.sub("", text).strip()


def _repair_summary_json(text: str) -> str | None:
    """Repair malformed model JSON when it still contains a root summary.

    ``json_repair`` may return several top-level values when a wrong closing
    delimiter splits the root object. Reassemble that stream only when the
    first value carries summary structure and a valid schema-key suffix carries
    the narrative field. This narrow gate avoids treating arbitrary prose or
    nested provider-error JSON as a successful summary.
    """
    repaired = repair_json(text, return_objects=True)
    if isinstance(repaired, dict):
        return json.dumps(repaired) if any(repaired.get(k) for k in _SUMMARY_KEYS) else None
    if not isinstance(repaired, list) or not repaired:
        return None

    root_index = next(
        (
            i
            for i in range(len(repaired) - 1, -1, -1)
            if isinstance(repaired[i], dict)
            and (
                any(repaired[i].get(key) for key in _SUMMARY_KEYS)
                or any(key in repaired[i] for key in ("accomplishments", "decisions"))
            )
        ),
        None,
    )
    if root_index is None:
        return None
    root = dict(repaired[root_index])
    for value in repaired[root_index + 1 :]:
        if isinstance(value, dict) and {"topic", "decision"}.issubset(value):
            root.setdefault("decisions", []).append(value)

    # json_repair can split a malformed root into several top-level values and
    # lose their field names. Find the earliest schema-key suffix that is valid
    # JSON; that preserves every recoverable field after the malformed token
    # without guessing names for anonymous values that came before it.
    decoder = json.JSONDecoder()
    suffix_keys = ("blockers", "themes", "work_in_progress", *_SUMMARY_KEYS)
    for key in suffix_keys:
        marker = f'"{key}"'
        start = text.find(marker)
        if start == -1:
            continue
        try:
            suffix, _ = decoder.raw_decode("{" + text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(suffix, dict) and any(suffix.get(k) for k in _SUMMARY_KEYS):
            root.update(suffix)
            break
    return json.dumps(root) if any(root.get(k) for k in _SUMMARY_KEYS) else None


def _has_summary_json(text: str) -> bool:
    """True if ``text`` contains a JSON object with a recognised summary key.

    Completion-ack messages mention the word ``narrative`` without carrying a
    JSON object. Preferring those over an earlier JSON answer is what broke
    the 2026-08-30 live fallback (gptme log ``dancing-sad-monster``): DeepSeek
    emitted a complete summary JSON, then "I have completed the analysis."
    ``extract_json_from_response`` lives in ``cc_backend`` (which imports this
    module), so this predicate stays local to avoid a circular import.
    """
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        start = text.find("{", pos)
        if start == -1:
            return False
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict) and any(obj.get(k) for k in _SUMMARY_KEYS):
            return True
        pos = end
    return False


def _extract_assistant_text(stdout: str) -> str:
    """Collect assistant message contents from gptme NDJSON output."""
    contents: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "message" and obj.get("role") == "assistant":
            content = obj.get("content")
            if isinstance(content, str) and content.strip():
                # Strip <think>...</think> reasoning blocks before recording.
                stripped = _strip_think_tags(content)
                if stripped:
                    contents.append(stripped)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if isinstance(text, str) and text.strip():
                            stripped = _strip_think_tags(text)
                            if stripped:
                                contents.append(stripped)
    if not contents:
        logger.warning("gptme fallback produced no assistant messages")
        return ""
    # Reasoning models (e.g. deepseek) emit a thinking/preamble first, which
    # may itself be valid JSON (e.g. {"thinking": "..."}).  Collect all
    # JSON-parseable candidates, then prefer the one that contains a recognised
    # summary key so that a JSON-shaped preamble is not returned instead of the
    # real answer; only fall back to the first JSON-parseable if none contain a
    # recognised key.
    json_candidates: list[str] = []
    for candidate in contents:
        try:
            json.loads(candidate)
            json_candidates.append(candidate)
        except json.JSONDecodeError:
            continue
    if json_candidates:
        # Iterate in reverse so the last (final answer) wins over an earlier
        # JSON-shaped thinking preamble that may also contain a summary key.
        # Reasoning models (e.g. deepseek) emit a preamble first, then the
        # real answer; scanning forward would return the preamble instead.
        json_set = set(json_candidates)
        for candidate in reversed(json_candidates):
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and any(parsed.get(k) for k in _SUMMARY_KEYS):
                # Later prose only wins if it *itself* carries a summary JSON
                # object (e.g. a fenced JSON revision). Completion-ack prose
                # ("I have completed the analysis") and gptme trailers do not.
                json_index = max(i for i, c in enumerate(contents) if c == candidate)
                later_prose = [
                    c
                    for c in contents[json_index + 1 :]
                    if c not in json_set and not _is_gptme_trailer(c)
                ]
                for prose in reversed(later_prose):
                    if _has_summary_json(prose):
                        logger.debug(
                            "gptme fallback: later prose carries summary JSON "
                            "(%d chars); preferring it over earlier JSON draft",
                            len(prose),
                        )
                        return prose
                logger.debug(
                    "gptme fallback: returning JSON with summary key (%d chars): %.100r",
                    len(candidate),
                    candidate,
                )
                return candidate
        # No JSON candidate has a summary key. Prefer non-JSON content if
        # available — the real answer may be plain text while the JSON was
        # a preamble (e.g. {"thinking": "..."}).  Let extract_json_from_response
        # try to pull JSON out of the plain text (handles embedded JSON,
        # code blocks, etc.).
        non_json = [c for c in contents if c not in set(json_candidates)]
        if non_json:
            for candidate in reversed(non_json):
                if _is_gptme_trailer(candidate):
                    continue
                repaired = _repair_summary_json(candidate)
                if repaired is not None:
                    logger.warning(
                        "gptme fallback: repaired malformed root summary JSON "
                        "after JSON preamble (%d -> %d chars)",
                        len(candidate),
                        len(repaired),
                    )
                    return repaired
            chosen = _last_non_trailer(non_json)
            logger.warning(
                "gptme fallback: no JSON candidate has summary key; "
                "falling back to last non-JSON content (%d chars): %.100r",
                len(chosen),
                chosen,
            )
            return chosen
        logger.warning(
            "gptme fallback: no JSON candidate has summary key; "
            "returning last JSON candidate (%d chars): %.100r",
            len(json_candidates[-1]),
            json_candidates[-1],
        )
        return json_candidates[-1]
    for candidate in reversed(contents):
        if _is_gptme_trailer(candidate):
            continue
        repaired = _repair_summary_json(candidate)
        if repaired is not None:
            logger.warning(
                "gptme fallback: repaired malformed root summary JSON (%d -> %d chars)",
                len(candidate),
                len(repaired),
            )
            return repaired

    chosen = _last_non_trailer(contents)
    logger.warning(
        "gptme fallback: no assistant message parsed as JSON (%d messages); "
        "returning last non-trailer message for caller to attempt extraction: %.100r",
        len(contents),
        chosen,
    )
    return chosen
