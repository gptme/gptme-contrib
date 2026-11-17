"""Tests for scripts/verify-package-coverage.py (README package index drift)."""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify-package-coverage.py"
_spec = importlib.util.spec_from_file_location("verify_package_coverage", _SCRIPT)
assert _spec is not None and _spec.loader is not None
vpc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vpc)


def test_parses_link_and_bold_table_rows():
    content = (
        "| [gptme-redact](./packages/gptme-redact/) | Redactor |\n"
        "| **gptme-review** | Reviewer | `uv pip install -e packages/gptme-review` |\n"
    )
    assert vpc.parse_documented_packages(content) == ["gptme-redact", "gptme-review"]


def test_ignores_file_links_and_bold_text_outside_first_cell():
    content = (
        "| Parts | **gptme-contrib** (this repo) | Plugins and packages |\n"
        "See [packages/README.md](./packages/README.md).\n"
    )
    assert vpc.parse_documented_packages(content) == []


def test_repo_readmes_list_every_package(capsys):
    assert vpc.main() == 0, capsys.readouterr().out
    assert "⚠️" not in capsys.readouterr().out
