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


def _pacing(weekly: float = 1.0):
    """Five-hour window is gone upstream (None since 2026-07); weekly binds."""
    return (0.1, 1000, weekly, 50_000)


def _patch_pool(quota_mod, monkeypatch, tmp_path: Path, cache: dict) -> None:
    cache_file = tmp_path / "codex-usage-cache.json"
    cache_file.write_text(json.dumps(cache))
    monkeypatch.setattr(quota_mod, "CODEX_USAGE_CACHE_FILE", cache_file)
    monkeypatch.setattr(quota_mod, "_load_codex_main_pacing", lambda: _pacing())


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

    assert quota_mod._load_codex_credits() is None


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


def test_scraper_parses_credit_balance_into_cache():
    data = _parse(STATUS_SCREEN)

    assert data["credits"] == 25_706
    # The weekly gauge is still reported as exhausted; credits only decide
    # whether that exhaustion is a hard stop.
    assert data["main"]["weekly"]["utilization"] == 1.0


def test_scraper_omits_credits_as_none_when_absent():
    data = _parse(STATUS_SCREEN.replace("Credits: 25,706 credits\n", ""))

    assert data["credits"] is None
