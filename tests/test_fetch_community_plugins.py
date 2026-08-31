"""Fail-closed contract for scripts/fetch-community-plugins.py.

A fresh CI checkout has no previous docs/community_plugins.json (gitignored),
so a one-source failure that still returns entries must not write + exit 0.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "scripts" / "fetch-community-plugins.py"
_spec = importlib.util.spec_from_file_location("fetch_community_plugins", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_ENTRY = {
    "name": "gptme/example",
    "description": "example plugin",
    "url": "https://github.com/gptme/example",
    "stars": 3,
    "language": "Python",
    "topics": ["gptme-plugin"],
}


def _run_main(tmp_path: Path, *extra: str) -> tuple[int, Path]:
    out = tmp_path / "docs" / "community_plugins.json"
    argv = ["fetch-community-plugins.py", "--output", str(out), *extra]
    with patch.object(sys, "argv", argv):
        rc = _mod.main()
    return rc, out


class TestShouldRefuseWrite:
    def test_partial_source_failure_refuses(self) -> None:
        assert _mod.should_refuse_write(any_source_failed=True, dry_run=False) is True

    def test_all_sources_ok_writes(self) -> None:
        assert _mod.should_refuse_write(any_source_failed=False, dry_run=False) is False

    def test_dry_run_never_refuses(self) -> None:
        assert _mod.should_refuse_write(any_source_failed=True, dry_run=True) is False


class TestMainFailClosed:
    def test_partial_registry_failure_does_not_write(self, tmp_path: Path) -> None:
        """Registry down, topic search up → refuse, no file. Fresh-checkout case."""
        with (
            patch.object(_mod, "fetch_registry", return_value=([], True)),
            patch.object(_mod, "fetch_topics", return_value=([_ENTRY], [])),
        ):
            rc, out = _run_main(tmp_path)
        assert rc == 1
        assert not out.exists()

    def test_partial_topic_failure_does_not_write(self, tmp_path: Path) -> None:
        """One GitHub topic down, registry up → refuse, no file."""
        with (
            patch.object(_mod, "fetch_registry", return_value=([_ENTRY], False)),
            patch.object(_mod, "fetch_topics", return_value=([], ["gptme-skill"])),
        ):
            rc, out = _run_main(tmp_path)
        assert rc == 1
        assert not out.exists()

    def test_all_sources_ok_writes(self, tmp_path: Path) -> None:
        with (
            patch.object(_mod, "fetch_registry", return_value=([_ENTRY], False)),
            patch.object(_mod, "fetch_topics", return_value=([_ENTRY], [])),
        ):
            rc, out = _run_main(tmp_path)
        assert rc == 0
        data = json.loads(out.read_text())
        assert data["entries"] == [_ENTRY]

    def test_total_failure_does_not_write(self, tmp_path: Path) -> None:
        with (
            patch.object(_mod, "fetch_registry", return_value=([], True)),
            patch.object(_mod, "fetch_topics", return_value=([], ["gptme-plugin"])),
        ):
            rc, out = _run_main(tmp_path)
        assert rc == 1
        assert not out.exists()

    def test_dry_run_partial_prints_and_skips_write(self, tmp_path: Path) -> None:
        with (
            patch.object(_mod, "fetch_registry", return_value=([], True)),
            patch.object(_mod, "fetch_topics", return_value=([_ENTRY], [])),
        ):
            rc, out = _run_main(tmp_path, "--dry-run")
        assert rc == 0
        assert not out.exists()
