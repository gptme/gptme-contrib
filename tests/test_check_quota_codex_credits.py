"""Credits-aware ChatGPT pool gating in check-quota.py / check-codex-usage.sh.

The ChatGPT Codex pool is shared by ``codex:*`` and gptme's
``openai-subscription`` models. A spent *weekly plan allowance* is not a hard
stop: ``/status`` also reports a purchased credit balance (``Credits: N
credits``) that Codex keeps serving against. Treating the weekly gauge alone as
exhaustion blocked both backends for days while the account still had credits
(observed 2026-11-25 at weekly 100% with ~25.7k credits).

These tests pin both sides of the seam:

1. the scraper parses ``credits`` into the cache (``None`` when absent, so a
   credit-less cache is unknown, not zero), and
2. the quota row stays *available* when credits remain and stays *blocked* when
   they are zero or unknown.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
SCRAPER = SCRIPTS / "check-codex-usage.sh"
QUOTA = SCRIPTS / "check-quota.py"


@pytest.fixture(scope="module")
def quota_mod():
    """Load scripts/check-quota.py as a module (dashed filename)."""
    spec = importlib.util.spec_from_file_location("contrib_check_quota", QUOTA)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations via sys.modules[cls.__module__]
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _patch_pool(
    quota_mod,
    monkeypatch,
    tmp_path: Path,
    extra: dict,
    five_hour: float | None = None,
) -> Path:
    """Write a weekly-exhausted cache in the scraper's shape.

    ``five_hour`` defaults to None because current /status output has no 5h line.
    """
    cache = {
        "main": {
            "five_hour": None
            if five_hour is None
            else {"utilization": five_hour, "resets_in_seconds": 1000},
            "weekly": {"utilization": 1.0, "resets_in_seconds": 50_000},
        },
        **extra,
    }
    cache_file = tmp_path / "codex-usage-cache.json"
    cache_file.write_text(json.dumps(cache))
    monkeypatch.setattr(quota_mod, "CODEX_USAGE_CACHE_FILE", cache_file)
    return cache_file


def test_weekly_exhausted_pool_with_credits_stays_available(
    quota_mod, monkeypatch, tmp_path
):
    """A spent weekly allowance with a credit balance is not a hard stop."""
    _patch_pool(quota_mod, monkeypatch, tmp_path, {"credits": 25_706})

    rows = [
        quota_mod._shared_codex_pool_quota(backend, "gpt-5.6-sol")
        for backend in ("gptme", "codex")
    ]

    for row in rows:
        assert row is not None
        assert row.available is True
        assert row.source == "codex-usage-cache"
        assert "on credits: 25,706 remaining" in row.reason
        assert row.metadata["on_credits"] is True
        assert row.metadata["credits"] == 25_706
        # Still deprioritized even though usable: no headroom, no plan allowance.
        assert row.headroom == 0.0
        assert row.pacing_status != "blocked"


def test_weekly_exhausted_pool_with_zero_credits_stays_blocked(
    quota_mod, monkeypatch, tmp_path
):
    """Zero credits is the real hard stop."""
    _patch_pool(quota_mod, monkeypatch, tmp_path, {"credits": 0})

    row = quota_mod._shared_codex_pool_quota("gptme", "gpt-5.6-sol")

    assert row is not None
    assert row.available is False
    assert "BLOCKED" in row.reason
    assert "no credits remaining" in row.reason
    assert row.metadata["on_credits"] is False


def test_weekly_exhausted_pool_without_credits_field_stays_blocked(
    quota_mod, monkeypatch, tmp_path
):
    """An absent field is unknown, not zero — keep the pre-existing hard block."""
    _patch_pool(quota_mod, monkeypatch, tmp_path, {})

    row = quota_mod._shared_codex_pool_quota("codex", "gpt-5.6-sol")

    assert row is not None
    assert row.available is False
    assert "no credits remaining" not in row.reason
    assert row.metadata["credits"] is None


def test_stale_cache_credits_are_unknown_not_zero(quota_mod, monkeypatch, tmp_path):
    """A stale cache cannot vouch for a balance; do not lift the block on it."""
    _patch_pool(quota_mod, monkeypatch, tmp_path, {"credits": 25_706})
    stale = time.time() - (quota_mod.CODEX_USAGE_MAX_AGE_SECONDS + 60)
    os.utime(quota_mod.CODEX_USAGE_CACHE_FILE, (stale, stale))

    assert quota_mod._shared_codex_pool_quota("codex", "gpt-5.6-sol") is None


def test_exhausted_five_hour_window_blocks_despite_credits(
    quota_mod, monkeypatch, tmp_path
):
    """Credits lift the weekly cap only."""
    _patch_pool(quota_mod, monkeypatch, tmp_path, {"credits": 25_706}, five_hour=1.0)

    row = quota_mod._shared_codex_pool_quota("codex", "gpt-5.6-sol")

    assert row is not None
    assert row.available is False
    assert row.metadata["on_credits"] is False


def test_cache_rewrite_mid_row_cannot_drop_credits(quota_mod, monkeypatch, tmp_path):
    """The scraper truncates and rewrites the cache; one row must use one read."""
    cache_file = _patch_pool(quota_mod, monkeypatch, tmp_path, {"credits": 25_706})
    read_text = Path.read_text

    def read_then_truncate(self: Path, *args: Any, **kwargs: Any) -> str:
        text = read_text(self, *args, **kwargs)
        if self == cache_file:
            cache_file.write_text("")
        return text

    monkeypatch.setattr(Path, "read_text", read_then_truncate)

    row = quota_mod._shared_codex_pool_quota("codex", "gpt-5.6-sol")

    assert row is not None
    assert row.available is True


# --- Scraper seam -----------------------------------------------------------
# The parser lives in a Python heredoc inside check-codex-usage.sh. Rather than
# grepping for the regex, run the same block the script runs against a
# representative /status screen so a rename or a broken pattern shows up here.

STATUS_SCREEN = """\
> /status

5h limit:    100% left (resets 12:00)
Weekly limit: 0% left (resets 5:32 AM on 14 Oct)
Credits: 25,706 credits
"""


def _parser_block() -> str:
    text = SCRAPER.read_text()
    marker = "# --- Parse usage via Python ---"
    start = text.index(marker)
    start = text.index("<< 'PYEOF'", start)
    start = text.index("\n", start) + 1
    end = text.index("\nPYEOF\n", start)
    return text[start:end]


def _parse(screen: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-c", _parser_block()],
        env={**os.environ, "CODEX_ACCUM": screen, "REPO_ROOT": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data: dict[str, Any] = json.loads(proc.stdout)
    return data


def test_scraper_output_without_five_hour_line_reaches_the_credit_gate(
    quota_mod, monkeypatch, tmp_path
):
    """Weekly-only /status output must produce an on-credits row end to end."""
    data = _parse(STATUS_SCREEN.replace("5h limit:    100% left (resets 12:00)\n", ""))
    assert data["main"]["five_hour"] is None
    cache_file = tmp_path / "codex-usage-cache.json"
    cache_file.write_text(json.dumps(data))
    monkeypatch.setattr(quota_mod, "CODEX_USAGE_CACHE_FILE", cache_file)

    row = quota_mod._shared_codex_pool_quota("codex", "gpt-5.6-sol")

    assert row is not None
    assert row.available is True
    assert row.metadata["on_credits"] is True


def test_scraper_parses_credit_balance_into_cache():
    data = _parse(STATUS_SCREEN)

    assert data["credits"] == 25_706
    # The weekly gauge is still reported as exhausted; credits only decide
    # whether that exhaustion is a hard stop.
    assert data["main"]["weekly"]["utilization"] == 1.0


def test_scraper_omits_credits_as_none_when_absent():
    data = _parse(STATUS_SCREEN.replace("Credits: 25,706 credits\n", ""))

    assert data["credits"] is None
