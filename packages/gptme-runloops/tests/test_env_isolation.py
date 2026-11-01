"""Regression: tests stay deterministic when run inside a PM slot environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CONFIG_TESTS = Path(__file__).parent / "test_run_item_config.py"

# Synthetic stand-ins for what a project-monitoring slot exports.
SYNTHETIC_SLOT_ENV = {
    "PM_GPTME_CANARY": "1",
    "PM_GPTME_CANARY_DEFAULT_TIMEOUT": "2100",
    "PROJECT_MONITORING_SELF_MERGE_ALLOWED_PATHS": "sentinel",
    "WORKSPACE_REPO": "fixture/repo",
    "SELF_MERGE_ALLOWED_PATHS": "sentinel",
}


def test_config_tests_pass_with_inherited_slot_environment() -> None:
    node_ids = [
        # Package defaults: fails with timeout 2100 if the inherited canary leaks.
        f"{CONFIG_TESTS}::test_missing_config_file_yields_defaults",
        # Explicit setenv still applies: expects 1800, not the inherited 2100.
        f"{CONFIG_TESTS}::test_gptme_canary_scales_default_timeout_only",
        f"{CONFIG_TESTS}::test_gptme_canary_timeout_disabled_keeps_default",
        # Config fallback for WORKSPACE_REPO is used only when it is not inherited.
        f"{CONFIG_TESTS}::test_assemble_hooks_picks_up_conventional_scripts",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *node_ids],
        env={**os.environ, **SYNTHETIC_SLOT_ENV},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "4 passed" in result.stdout
