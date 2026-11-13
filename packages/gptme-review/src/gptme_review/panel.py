"""Portable one-shot subscription transports and multi-model panel execution.

Callers own reviewer identities, privacy/quota admission, roster selection and
measurement persistence. ``panel_review`` accepts an already-admitted roster;
calling a transport does not establish permission to send a prompt to it.
Prices must be passed explicitly (or loaded from an explicit TOML path).
No brain checkout, private state directory or quota collector is consulted.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomllib

from . import lifecycle
from .core import (
    ReviewDeadline,
    ReviewError,
    consensus_findings,
    deduplicate_findings,
    parse_review,
)

_SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2}


#: Hard wall-clock cap per reviewer. The sweep's p90 target is one sweep
#: interval (20 min); one-shot reviews measure 17-60s, so this is a runaway
#: guard, not an expectation.
PANEL_TIMEOUT_S = float(os.environ.get("AI_REVIEW_PANEL_TIMEOUT_S", "420"))


#: Turn budget for the grok transport. One turn is not enough for a large
#: review prompt: grok opens with a preamble or a tool call and then dies at
#: the cap with ``Max turns reached``. See ``grok_transport``.
def _positive_int_env(name: str, default: int) -> int:
    """Parse a positive int from the environment; garbage or <=0 keeps *default*."""
    raw = os.environ.get(name, "")
    try:
        value = int(raw.strip() or default)
    except ValueError:
        return default
    return value if value >= 1 else default


GROK_MAX_TURNS = _positive_int_env("AI_REVIEW_GROK_MAX_TURNS", 3)


#: Built-in grok tool IDs to strip. ``--tools ""`` does **not** disable tools:
#: stock profiles inject optional tools before the allowlist, and MCP
#: meta-tools stay unless denied (headless-mode docs). An empty allowlist plus
#: ``--max-turns 1`` is the 2026-10-10..21 ledger signature: 0/42 grok answers.
#: Include current IDs as well as legacy aliases. The 2026-10-22 sweep still
#: called search_tool/use_tool and write after the original denylist shipped.
GROK_DISALLOWED_TOOLS = ",".join(
    (
        "read_file",
        "search_replace",
        "grep",
        "list_dir",
        "run_terminal_cmd",
        "run_terminal_command",
        "web_search",
        "web_fetch",
        "todo_write",
        "spawn_subagent",
        "memory_search",
        "memory_get",
        "search_tool",
        "use_tool",
        "read",
        "write",
        "write_file",
        "edit",
        "bash",
        "grep_search",
        "glob",
        "task",
        "kill_task",
        "get_task_output",
        "lsp",
        "monitor",
        "workflow",
        "send_feedback",
        "ask_user_question",
        "session_search",
        "image_gen",
        "video_gen",
        "get_command_or_subagent_output",
        "kill_command_or_subagent",
        "Agent",
        # grok 1.0.46 still offered these five after the list above (session
        # tool_definitions.json, 2026-10-23). Headless runs load the
        # grok-build-plan agent despite --no-plan; the model called the
        # plan/media tools as stand-ins for file reads, was denied, and spent
        # its last turn reasoning into the 420s cap (ErikBjare/bob#1423).
        "enter_plan_mode",
        "exit_plan_mode",
        "image_edit",
        "image_to_video",
        "reference_to_video",
    )
)


#: Environment for the grok reviewer. With every local tool removed the model
#: falls back to server-side ``x_keyword_search`` (``backend: true``), which
#: neither ``--disable-web-search`` nor the denylist covers; one such call
#: never returned and the run hit the cap (#1423 repro, 2026-10-24).
#: ``features.backend_tools`` is the documented switch.
GROK_ENV_OVERRIDES = {"GROK_BACKEND_SEARCH": "0"}


#: ``primary`` mode cap on subscription-posted reviews per UTC day. At the
#: measured ~$0.16 list-price-equivalent per codex review and 750-2,400 reviews a
#: day, uncapped primary would take $120-380/day of ChatGPT-pool quota from
#: autonomous work; past the cap reviews go to OpenRouter (the merge-gate
#: fallback). 120 reviews is roughly $20/day list-equivalent.
PRIMARY_DAILY_MAX = int(os.environ.get("AI_REVIEW_PANEL_PRIMARY_DAILY_MAX", "120"))


#: Optional: in ``primary`` mode keep PUBLIC repos on OpenRouter while the key
#: has at least this much left today (Erik: "or for public-repo traffic when the
#: key has headroom"). Unset = subscription first for everything.
PUBLIC_HEADROOM_ENV = "AI_REVIEW_PANEL_PUBLIC_OPENROUTER_HEADROOM_USD"


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PanelReviewer:
    key: str  # stable id used in ledgers: "<backend>:<model>"
    family: str  # model family: independence of opinion is per family
    transport: str  # codex | claude | grok | copilot
    model: str  # what the transport is told to run
    price_backend: str  # [prices.<backend>] table in config/harness-quota.toml
    price_model: str  # key inside that table
    pool: str  # quota pool, for reporting
    effort: str | None = None


def _parse_ledger_ts(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def choose_primary_route(
    *,
    mode: str,
    available: Sequence[PanelReviewer],
    sensitive_repo: bool,
    key_remaining: float | None,
    public_headroom_usd: float | None,
    primary_runs_today: int = 0,
    primary_daily_max: int | None = None,
) -> tuple[str, str]:
    """Which route posts the review: ``("subscription"|"openrouter", why)``.

    Subscription is chosen only in ``primary`` mode and only while some reviewer
    is clear. OpenRouter stays the merge-gate fallback in every other case, and
    optionally carries public-repo traffic while its key has headroom.
    """
    if mode != "primary":
        return "openrouter", f"panel mode is {mode}"
    if not available:
        return "openrouter", "no subscription reviewer available"
    cap = PRIMARY_DAILY_MAX if primary_daily_max is None else primary_daily_max
    if primary_runs_today >= cap:
        return (
            "openrouter",
            f"subscription primary cap reached ({primary_runs_today}/{cap} today)",
        )
    if (
        public_headroom_usd is not None
        and not sensitive_repo
        and key_remaining is not None
        and key_remaining >= public_headroom_usd
    ):
        return (
            "openrouter",
            f"public repo, review key has ${key_remaining:.2f} >= "
            f"${public_headroom_usd:.2f} headroom",
        )
    return "subscription", "primary mode, subscription reviewer available"


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def load_prices(path: Path | None = None) -> dict[str, dict[str, tuple[float, float]]]:
    if path is None:
        return {}
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for backend, models in (raw.get("prices") or {}).items():
        table: dict[str, tuple[float, float]] = {}
        for model, pair in (models or {}).items():
            if isinstance(pair, list) and len(pair) == 2:
                table[str(model)] = (float(pair[0]), float(pair[1]))
        out[str(backend)] = table
    return out


def list_price_usd(
    reviewer: PanelReviewer,
    tokens: Mapping[str, int],
    prices: Mapping[str, Mapping[str, tuple[float, float]]] | None = None,
) -> float | None:
    """List-price-equivalent cost of one call, ignoring cache discounts.

    The subscription's marginal cost is quota share, which is observable only
    as pool-wide utilisation deltas. Tokens at list price is the comparable
    proxy (conservative: cached input is billed as fresh).
    """
    table = (prices if prices is not None else load_prices()).get(
        reviewer.price_backend, {}
    )
    pair = table.get(reviewer.price_model)
    if pair is None:
        return None
    in_tok = int(tokens.get("input_tokens") or 0)
    out_tok = int(tokens.get("output_tokens") or 0)
    return round((in_tok * pair[0] + out_tok * pair[1]) / 1_000_000, 6)


# ---------------------------------------------------------------------------
# Transports: prompt in, (text, tokens, reported_cost) out. Raise ReviewError.
# ---------------------------------------------------------------------------

Transport = Callable[[PanelReviewer, str, float], tuple[str, dict, "float | None"]]


_BIN_FALLBACK_DIRS = (
    Path.home() / ".npm-global" / "bin",  # codex
    Path.home() / ".local" / "bin",  # claude, grok
)


def _resolve_bin(name: str) -> str:
    """Absolute path for a CLI, even under a systemd unit's minimal PATH.

    The sweep runs from a user unit whose PATH need not include the npm-global
    or ~/.local bin dirs where the subscription CLIs live; a bare name would
    make every shadow/overflow review die with ``No such file``.
    """
    found = shutil.which(name)
    if found:
        return found
    for d in _BIN_FALLBACK_DIRS:
        candidate = d / name
        if candidate.exists():
            return str(candidate)
    return name


def _run_bounded(
    cmd: list[str],
    *,
    stdin_text: str | None,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str = "/tmp",
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` that kills the whole process group on timeout.

    The CLIs spawn children (node workers, MCP servers); killing only the
    parent leaves them holding the quota slot and the pipe.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise ReviewError(
            f"{cmd[0]} timed out after {timeout:g}s (hard wall-clock cap)"
        ) from exc
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _transport_error(name: str, proc: subprocess.CompletedProcess[str]) -> ReviewError:
    tail = " ".join((proc.stderr or proc.stdout or "").split())[-300:]
    return ReviewError(f"{name} rc={proc.returncode}: {tail}")


def codex_transport(
    reviewer: PanelReviewer, prompt: str, timeout: float
) -> tuple[str, dict, float | None]:
    """``codex exec`` one-shot over the ChatGPT subscription.

    Read-only sandbox, cwd ``/tmp``: the prompt carries the diff and files, so
    there is nothing to read and nothing to write. Not ``--ephemeral``: the
    session file is the audit trail (same policy as ``claude_transport``).
    """
    cmd = [
        _resolve_bin("codex"),
        "exec",
        "-m",
        reviewer.model,
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "--json",
        "-C",
        "/tmp",
    ]
    if reviewer.effort:
        cmd += ["-c", f'model_reasoning_effort="{reviewer.effort}"']
    proc = _run_bounded([*cmd, "-"], stdin_text=prompt, timeout=timeout)
    text = ""
    tokens: dict = {}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if (
            event.get("type") == "item.completed"
            and item.get("type") == "agent_message"
        ):
            text = item.get("text") or text
        if event.get("type") == "turn.completed":
            tokens = event.get("usage") or {}
    if proc.returncode != 0 or not text:
        raise _transport_error("codex exec", proc)
    return text, dict(tokens), None


def _cc_env() -> dict[str, str]:
    env = os.environ.copy()
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CC_SESSION_ID", "CC_MODEL"):
        env.pop(k, None)
    return env


def claude_transport(
    reviewer: PanelReviewer, prompt: str, timeout: float
) -> tuple[str, dict, float | None]:
    """``claude -p`` one-shot on the active CC slot.

    No tools, no skills, no MCP, no user/project settings (so no hooks inject
    lessons into the review) and a minimal system prompt: the review prompt is
    the whole input. A fresh ``--session-id`` per call; session persistence stays
    ON (the trajectory is the audit trail — see AGENTS.md "Nested claude -p").
    """
    cmd = [
        _resolve_bin("claude"),
        "-p",
        "--model",
        reviewer.model,
        "--tools",
        "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--system-prompt",
        "You are a careful senior code reviewer. Follow the output format in the "
        "user message exactly.",
        "--output-format",
        "json",
        "--session-id",
        str(uuid.uuid4()),
    ]
    proc = _run_bounded(cmd, stdin_text=prompt, timeout=timeout, env=_cc_env())
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise _transport_error("claude -p", proc) from None
    if proc.returncode != 0 or data.get("is_error") or not data.get("result"):
        raise ReviewError(
            f"claude -p rc={proc.returncode}: "
            f"{' '.join(str(data.get('result') or data.get('subtype') or '').split())[:300]}"
        )
    usage = data.get("usage") or {}
    tokens = {
        # CC splits input into fresh + cache read/creation; list price treats all
        # three as input here (cache discounts are not modelled, see list_price_usd).
        "input_tokens": int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0),
        "cached_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }
    cost = data.get("total_cost_usd")
    return (
        str(data["result"]),
        tokens,
        float(cost) if isinstance(cost, int | float) else None,
    )


def grok_cli_transport(
    reviewer: PanelReviewer, prompt: str, timeout: float
) -> tuple[str, dict, float | None]:
    """``grok --prompt-file`` one-shot on the SuperGrok pool (plain output).

    Live-checked 2026-10-21 (session 7e31): a 320-token self-contained prompt
    parses in 9s; a 35k-token prompt on the old argv dies with
    ``Max turns reached`` in 27s. ``--tools ""`` does not disable tools (stock
    profile injection + MCP meta-tools). The denylist removes current and
    legacy tool IDs; deny-all permissions gate any IDs added by CLI upgrades.
    ``--verbatim`` is essential: otherwise large requests become excerpts
    plus file-read instructions that this tool-free reviewer cannot satisfy.
    Both flags are supported by the installed 2026-10-22 CLI and were exercised
    with a full 420k-character prompt. The 3-turn and wall-clock caps still
    bound the run; failed/unparseable answers remain degraded-panel failures.
    """
    path = Path(f"/tmp/ai-review-grok-{uuid.uuid4().hex}.txt")
    path.write_text(prompt, encoding="utf-8")
    try:
        cmd = [
            _resolve_bin("grok"),
            "--prompt-file",
            str(path),
            "-m",
            reviewer.model,
            "--output-format",
            "plain",
            "--disallowed-tools",
            GROK_DISALLOWED_TOOLS,
            "--deny",
            "*",
            "--verbatim",
            "--max-turns",
            str(GROK_MAX_TURNS),
            "--no-subagents",
            "--no-plan",
            "--disable-web-search",
            "--permission-mode",
            "dontAsk",
            "--cwd",
            "/tmp",
        ]
        if reviewer.effort:
            cmd += ["--reasoning-effort", reviewer.effort]
        proc = _run_bounded(
            cmd,
            stdin_text=None,
            timeout=timeout,
            env={**os.environ, **GROK_ENV_OVERRIDES},
        )
    finally:
        path.unlink(missing_ok=True)
    text = (proc.stdout or "").strip()
    if proc.returncode != 0 or not text:
        raise _transport_error("grok", proc)
    # Plain mode reports no usage; estimate from characters so the cost proxy is
    # at least the right order of magnitude (flagged by `tokens_estimated`).
    return (
        text,
        {
            "input_tokens": len(prompt) // 4,
            "output_tokens": len(text) // 4,
            "tokens_estimated": 1,
        },
        None,
    )


def copilot_transport(
    reviewer: PanelReviewer, prompt: str, timeout: float
) -> tuple[str, dict, float | None]:
    """Calibration's low-effort, tool-free stdin transport; AIU are not USD.

    Every checkpoint must attest zero tools, not just the final one. Missing
    or malformed model evidence cannot turn an agent response into a vote.
    """
    cmd = [
        _resolve_bin("copilot"),
        "--model",
        reviewer.model,
        "--available-tools=__none__",
        "--disable-builtin-mcps",
        "--no-custom-instructions",
        "--disable-client-telemetry",
        "--output-format",
        "json",
        "--reasoning-effort",
        "low",
        "-C",
        "/tmp",
    ]
    proc = _run_bounded(cmd, stdin_text=prompt, timeout=timeout)
    text = ""
    usage: dict = {}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        data = event.get("data") or {}
        if not isinstance(data, dict):
            raise ReviewError("Copilot malformed tool/usage evidence")
        if event.get("type") == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content:
                text = content
        if event.get("type") == "session.usage_checkpoint":
            try:
                calls = [
                    state["models"][reviewer.model]
                    for state in data["promptCacheBreakState"]
                ]
                if not calls or any(
                    type(call.get("tool_count")) is not int or call["tool_count"] != 0
                    for call in calls
                ):
                    raise ValueError("tools not confirmed zero")
                tokens = (data.get("accountingSnapshot") or {}).get(
                    "tokenDetails"
                ) or {}
                usage = {
                    "input_tokens": (tokens.get("input") or {}).get("tokenCount"),
                    "output_tokens": (tokens.get("output") or {}).get("tokenCount"),
                    "tool_count": 0,
                    "copilot_credits": data["totalNanoAiu"] / 1e9,
                }
            except (KeyError, TypeError, AttributeError, ValueError):
                raise ReviewError(
                    "Copilot tool_count must be confirmed 0 in usage checkpoint"
                ) from None
    if proc.returncode != 0 or not text:
        raise _transport_error("copilot", proc)
    if not usage:
        raise ReviewError("Copilot missing zero-tool usage checkpoint")
    return text, usage, None


GROK_DIRECT_URL = "https://cli-chat-proxy.grok.com/v1/chat/completions"


GROK_AUTH_FILE = Path.home() / ".grok" / "auth.json"


#: Bounds runaway output; the largest soak answer was ~8k chars (~2k tokens).
GROK_DIRECT_MAX_TOKENS = 16000


GROK_DIRECT_SYSTEM = "You are an expert code reviewer. Answer with the JSON block only."


#: The CLI owns the OIDC refresh (~6h keys). Never refresh here: a second
#: refresher would race the CLI's refresh-token rotation.
GROK_KEY_MARGIN_S = 900


def _grok_direct_key(timeout: float, now: datetime | None = None) -> str | None:
    """The CLI's bearer key if it outlives this review, else None."""
    try:
        data = json.loads(GROK_AUTH_FILE.read_text(encoding="utf-8"))
        account = next(iter(data.values()))
        key = account["key"]
        expires = _parse_ledger_ts(account.get("expires_at"))
    except (OSError, ValueError, StopIteration, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(key, str) or not key or expires is None:
        return None
    left = (expires - (now or datetime.now(UTC))).total_seconds()
    return key if left > timeout + GROK_KEY_MARGIN_S else None


def _grok_client_version() -> str:
    """The installed CLI version; the proxy answers HTTP 426 without it."""
    try:
        out = subprocess.run(
            [_resolve_bin("grok"), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewError(f"grok --version failed: {type(exc).__name__}") from exc
    parts = out.split()
    if len(parts) < 2 or not parts[1][:1].isdigit():
        raise ReviewError(f"grok --version unparseable: {out.strip()[:80]!r}")
    return parts[1]


def grok_direct_transport(
    reviewer: PanelReviewer, prompt: str, timeout: float, key: str
) -> tuple[str, dict, float | None]:
    """Stream the CLI's own chat endpoint with a minimal system prompt.

    The CLI's agent framing pulls grok toward tools it is denied: on
    gptme/gptme#4488 it streamed text to the 420s cap twice and once returned
    a fake ``<tool_call>`` as text. The same prompts direct: 8/8 parseable,
    23-193s (2026-10-25 soak readout).
    """
    import urllib.error
    import urllib.request

    body = {
        "model": reviewer.model,
        "stream": True,
        "max_tokens": GROK_DIRECT_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": GROK_DIRECT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    if reviewer.effort:
        body["reasoning_effort"] = reviewer.effort
    # Start the clock before the version probe so it counts against the cap.
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        GROK_DIRECT_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "x-grok-client-version": _grok_client_version(),
        },
    )
    wall_error = f"grok direct timed out after {timeout:g}s (hard wall-clock cap)"
    expired = threading.Event()
    content: list[str] = []
    finish = None
    usage: dict = {}
    try:
        with urllib.request.urlopen(request, timeout=min(60.0, timeout)) as resp:

            def _expire() -> None:
                # Closing the response unblocks a read stuck between lines;
                # the per-read socket timeout alone does not bound the total.
                expired.set()
                resp.close()

            timer = threading.Timer(max(deadline - time.monotonic(), 0.0), _expire)
            timer.daemon = True
            timer.start()
            try:
                for raw in resp:
                    if expired.is_set() or time.monotonic() > deadline:
                        raise ReviewError(wall_error)
                    line = raw.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        break
                    event = json.loads(payload)
                    usage = event.get("usage") or usage
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content.append(delta["content"])
                        finish = choice.get("finish_reason") or finish
            finally:
                timer.cancel()
    except ReviewError:
        raise
    except urllib.error.HTTPError as exc:
        raise ReviewError(f"grok direct HTTP {exc.code}") from exc
    except Exception as exc:
        # A stalled stream (socket idle timeout, or the deadline timer closing
        # the response) is a hang: report it so the timeout breaker trips.
        if expired.is_set() or isinstance(exc, TimeoutError):
            raise ReviewError(wall_error) from exc
        raise ReviewError(f"grok direct {type(exc).__name__}: {exc}") from exc
    text = "".join(content).strip()
    if finish == "length":
        raise ReviewError("grok direct hit max_tokens")
    if not text:
        raise ReviewError(f"grok direct returned no content (finish={finish})")
    tokens = {
        "input_tokens": usage.get("prompt_tokens") or len(prompt) // 4,
        "output_tokens": usage.get("completion_tokens") or len(text) // 4,
    }
    if not usage:
        tokens["tokens_estimated"] = 1
    return text, tokens, None


def grok_transport(
    reviewer: PanelReviewer, prompt: str, timeout: float
) -> tuple[str, dict, float | None]:
    """Direct endpoint while the CLI's key is fresh; otherwise the CLI.

    The CLI run also refreshes the key, so the next review goes direct.
    """
    key = _grok_direct_key(timeout)
    if key is None:
        return grok_cli_transport(reviewer, prompt, timeout)
    return grok_direct_transport(reviewer, prompt, timeout, key)


TRANSPORTS: dict[str, Transport] = {
    "codex": codex_transport,
    "claude": claude_transport,
    "grok": grok_transport,
    "copilot": copilot_transport,
}


# ---------------------------------------------------------------------------
# One reviewer, and the panel
# ---------------------------------------------------------------------------


class PanelReviewError(ReviewError):
    """A failed panel that still knows how each reviewer did.

    Callers log ``reviewer_rows`` instead of marking every requested reviewer
    failed: blanket rows charged codex/claude with grok's failures and dropped
    grok's timeout text, so the timeout breaker missed the first 420s hang.
    """

    def __init__(self, message: str, reviewer_rows: list[dict]) -> None:
        super().__init__(message)
        self.reviewer_rows = reviewer_rows


@dataclass
class ReviewerResult:
    reviewer: PanelReviewer
    ok: bool
    findings: list[dict] = field(default_factory=list)
    change_summary: dict = field(default_factory=dict)
    wall_s: float = 0.0
    tokens: dict = field(default_factory=dict)
    list_price_usd: float | None = None
    error: str = ""

    def row(self) -> dict:
        return {
            "key": self.reviewer.key,
            "model": self.reviewer.model,
            "family": self.reviewer.family,
            "engine": f"one-shot/{self.reviewer.transport}",
            "route": "subscription",
            "pool": self.reviewer.pool,
            "outcome": "ok" if self.ok else "failed",
            "wall_s": round(self.wall_s, 2),
            "tokens": self.tokens,
            "list_price_usd": self.list_price_usd,
            "n_findings": len(self.findings),
            **({"error": self.error[:300]} if self.error else {}),
        }


def run_reviewer(
    reviewer: PanelReviewer,
    prompt: str,
    *,
    timeout: float = PANEL_TIMEOUT_S,
    prices: Mapping[str, Mapping[str, tuple[float, float]]] | None = None,
) -> ReviewerResult:
    """Run one reviewer; never raises (a dead reviewer is a result, not a crash)."""
    started = time.monotonic()
    try:
        text, tokens, reported_cost = TRANSPORTS[reviewer.transport](
            reviewer, prompt, timeout
        )
        parsed = parse_review(text)
        if parsed is None:
            raise ReviewError(f"output unparseable: {' '.join(text.split())[:200]!r}")
        findings, change_summary = parsed
        cost = (
            reported_cost
            if reported_cost is not None
            else list_price_usd(reviewer, tokens, prices)
        )
        return ReviewerResult(
            reviewer,
            True,
            findings=findings,
            change_summary=change_summary if isinstance(change_summary, dict) else {},
            wall_s=time.monotonic() - started,
            tokens=tokens,
            list_price_usd=cost,
        )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        return ReviewerResult(
            reviewer,
            False,
            wall_s=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
        )


def panel_review(
    prompt: str,
    reviewers: Sequence[PanelReviewer],
    *,
    timeout: float = PANEL_TIMEOUT_S,
    min_agreement: int = 1,
    skipped: Mapping[str, str] | None = None,
    prices: Mapping[str, Mapping[str, tuple[float, float]]] | None = None,
    deadline: ReviewDeadline | None = None,
) -> tuple[list[dict], dict]:
    """Run *reviewers* in parallel; return (merged findings, usage).

    ``usage`` carries the same ``consensus_*`` / ``pass_failures`` keys as
    ``fanout_llm_review`` so the marker's partial-fan-out accounting needs no
    second code path, plus a ``panel`` block with the per-model measurements.
    """
    if not reviewers:
        raise ReviewError("subscription panel has no available reviewer")
    if not 1 <= min_agreement <= len(reviewers):
        raise ReviewError(
            f"subscription panel cannot apply {min_agreement}-of-"
            f"{len(reviewers)} agreement"
        )
    prices = prices if prices is not None else load_prices()
    started = time.monotonic()
    panel_deadline = started + timeout + 30
    if deadline is not None:
        # Check before launching any request. Keep an absolute deadline so
        # thread startup cannot extend the caller's remaining budget.
        panel_deadline = min(
            panel_deadline,
            time.monotonic() + deadline.remaining("subscription panel start"),
        )
    results: list[ReviewerResult | None] = [None] * len(reviewers)

    def _go(i: int, reviewer: PanelReviewer) -> None:
        transport_timeout = min(timeout, panel_deadline - time.monotonic())
        if transport_timeout <= 0:
            results[i] = ReviewerResult(
                reviewer, False, error="deadline exhausted before transport"
            )
            return
        lifecycle.stage("subscription_transport", attempt=i + 1)
        results[i] = run_reviewer(
            reviewer, prompt, timeout=transport_timeout, prices=prices
        )

    threads = [
        threading.Thread(
            target=copy_context().run,
            args=(_go, i, r),
            daemon=True,
            name=f"panel-{r.key}",
        )
        for i, r in enumerate(reviewers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, panel_deadline - time.monotonic()))
    final: list[ReviewerResult] = []
    for reviewer, res in zip(reviewers, results, strict=True):
        final.append(
            res
            if res is not None
            else ReviewerResult(
                reviewer, False, wall_s=timeout, error="abandoned at panel deadline"
            )
        )

    failures = [f"{r.reviewer.key}: {r.error}" for r in final if not r.ok]
    answered = [r for r in final if r.ok]
    if not answered:
        summary = "; ".join(" ".join(f.split()) for f in failures[:5])
        raise PanelReviewError(
            f"all subscription panel reviewers failed ({len(final)}): {summary}",
            [r.row() for r in final],
        )

    per_model: list[list[dict]] = []
    for r in answered:
        findings = list(r.findings)
        for f in findings:
            f.setdefault("aspect", "general")
            f.setdefault("independence_key", f"{r.reviewer.key}:general")
            f.setdefault("model", r.reviewer.key)
            # Each subscription reviewer executes one general pass. Stamp from
            # the transport, never from fields supplied in the model response.
            f["provenance"] = [
                {"model": r.reviewer.key, "pass": 1, "aspect": "general"}
            ]
        per_model.append(deduplicate_findings(findings))

    kept, rejected = consensus_findings(
        per_model,
        min_agreement=min_agreement,
        pass_aspects=[{"general"} for _ in per_model],
    )
    for f in kept:
        keys = {str(f.get("independence_key") or "")}
        keys.update(str(k) for k in f.get("merged_independence_keys") or [])
        f["panel_models"] = sorted(k.rsplit(":", 1)[0] for k in keys if k)

    # Same refusal as `fanout_llm_review`: a partial panel that found nothing is
    # indistinguishable from a clean review, so it must not post as one.
    if not kept and failures:
        raise PanelReviewError(
            f"degraded panel produced no findings: {len(answered)} of {len(final)} "
            "reviewer(s) answered — refusing to report a clean review when part of "
            "it did not run. Failures: " + "; ".join(failures[:5]),
            [r.row() for r in final],
        )

    usage: dict[str, object] = {
        "provider": "subscription",
        "providers": sorted({r.reviewer.family for r in answered}),
        "cost": 0.0,
        "completion_tokens": sum(
            int(r.tokens.get("output_tokens") or 0) for r in answered
        ),
        "consensus_requested_passes": len(final),
        "consensus_passes": len(answered),
        "consensus_requested_jobs": len(final),
        "consensus_answered_jobs": len(answered),
        "consensus_min_agreement_requested": min_agreement,
        "consensus_min_agreement": min(min_agreement, len(answered)),
        "consensus_rejected": len(rejected),
    }
    if failures:
        usage["pass_failures"] = failures
    for r in answered:
        cs = r.change_summary
        if isinstance(cs, dict) and (cs.get("summary") or cs.get("files")):
            usage["change_summary"] = cs
            break
    usage["panel"] = {
        "requested": [r.reviewer.key for r in final],
        "answered": [r.reviewer.key for r in answered],
        "skipped": dict(skipped or {}),
        "families": sorted({r.reviewer.family for r in answered}),
        "reviewers": [r.row() for r in final],
        "wall_s": round(time.monotonic() - started, 2),
        "list_price_usd": (
            round(sum(r.list_price_usd or 0.0 for r in final), 6)
            if all(r.list_price_usd is not None for r in final)
            else None
        ),
    }
    kept.sort(key=lambda f: _SEVERITY_RANK.get(str(f.get("severity", "P2")), 9))
    return kept, usage


# ---------------------------------------------------------------------------
# Measurement ledger
# ---------------------------------------------------------------------------


def compact_finding(f: Mapping[str, object]) -> dict:
    """The part of a finding needed to compare reviewers on the same SHA."""
    return {
        "severity": f.get("severity"),
        "file": f.get("file"),
        "line": f.get("line"),
        "class": f.get("class"),
        "security": bool(f.get("security")),
        "body": str(f.get("body") or "")[:1500],
        "quote": str(f.get("quote") or "")[:300],
        **({"provenance": f["provenance"]} if f.get("provenance") else {}),
        **(
            {"models": list(f["panel_models"])}  # type: ignore[call-overload]
            if f.get("panel_models")
            else {}
        ),
        **(
            {"agreement": f["agreement"]} if isinstance(f.get("agreement"), int) else {}
        ),
    }


def openrouter_reviewer_row(
    model: str, usage: Mapping[str, object], wall_s: float, *, ok: bool = True
) -> dict:
    cost = usage.get("cost")
    return {
        "key": f"openrouter:{model.removeprefix('openrouter/')}",
        "model": model.removeprefix("openrouter/"),
        "family": model.removeprefix("openrouter/").split("/", 1)[0],
        "engine": "llm",
        "route": "openrouter",
        "outcome": "ok" if ok else "failed",
        "wall_s": round(wall_s, 2),
        "tokens": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        },
        "billed_usd": cost if isinstance(cost, int | float) else None,
        "list_price_usd": cost if isinstance(cost, int | float) else None,
    }


#: Shadow-only (idea #6478): the family a pool-conditional panel cap would hold
#: back so the consensus gate keeps an independent reviewer. The 2026-11-06
#: replay measured codex as the lowest-recall family. Nothing here changes the
#: panel or the gate; it only records what the cap would have done.
PANEL_CAP_SHADOW_FAMILY = "openai"


def panel_cap_shadow_record(
    *,
    repo: str,
    number: int,
    sha: str,
    panel: Sequence[PanelReviewer],
    available: Sequence[PanelReviewer],
    label: str = "",
    reserve_family: str = PANEL_CAP_SHADOW_FAMILY,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What a ``reserve_family`` cap would have done to this panel (pure)."""
    shadow_panel = [r.key for r in panel if r.family != reserve_family]
    panel_families = sorted({r.family for r in panel})
    free_for_gate = sorted(
        {r.family for r in available} - {r.family for r in panel}
        | (
            {reserve_family}
            if any(r.family == reserve_family for r in panel)
            else set()
        )
    )
    return {
        "ts": (now or datetime.now(UTC)).isoformat(),
        "repo": repo,
        "number": number,
        "sha": sha,
        "label": label,
        "reserved_family": reserve_family,
        "actual_panel": [r.key for r in panel],
        "actual_families": panel_families,
        "reserved_in_actual_panel": any(r.family == reserve_family for r in panel),
        "shadow_panel": shadow_panel,
        "shadow_panel_nonempty": bool(shadow_panel),
        "available_families": sorted({r.family for r in available}),
        "families_free_for_gate_under_cap": free_for_gate,
    }


def parse_headroom(env: Mapping[str, str] | None = None) -> float | None:
    raw = (env if env is not None else os.environ).get(PUBLIC_HEADROOM_ENV, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
