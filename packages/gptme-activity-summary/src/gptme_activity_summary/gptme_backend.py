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
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Env switches so operators can disable the fallback or pin a specific model.
_DISABLE_ENV = "GPTME_ACTIVITY_SUMMARY_GPTME_FALLBACK"  # set to "0" to disable
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
    """Whether the gptme fallback is enabled (on by default, off via env)."""
    val = os.environ.get(_DISABLE_ENV, "1").strip().lower()
    return val not in ("0", "false", "no", "off")


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
        logger.debug("gptme fallback disabled via %s", _DISABLE_ENV)
        return ""
    if shutil.which("gptme") is None:
        logger.debug("gptme binary not found on PATH; skipping fallback")
        return ""

    model = os.environ.get(_MODEL_ENV, _DEFAULT_MODEL).strip()
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
        prompt,
    ]

    env = os.environ.copy()
    # Prevent recursion / contamination from a parent gptme invocation.
    env.pop("GPTME_SUBPROCESS", None)

    try:
        result = subprocess.run(
            cmd,
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

    if result.returncode != 0:
        logger.warning(
            "gptme fallback exited %d: stderr=%s stdout=%s",
            result.returncode,
            (result.stderr or "")[:300],
            (result.stdout or "")[:300],
        )
        return ""

    return _extract_assistant_text(result.stdout)


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
                contents.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if text.strip():
                            contents.append(text)
    if not contents:
        logger.warning("gptme fallback produced no assistant messages")
        return ""
    return "\n".join(contents)
