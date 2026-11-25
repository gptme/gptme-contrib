"""Same-named symbols in different files must stay separate graph nodes (C4, gptme/gptme-contrib#2356)."""

from __future__ import annotations

from pathlib import Path

from gptme_codegraph.core import build_cross_file_call_graph, build_index


def _write_two_modules(tmp_path: Path) -> None:
    for mod in ("alpha", "beta"):
        (tmp_path / f"{mod}.py").write_text(
            f"def helper():\n    return '{mod}'\n\n\ndef run():\n    return helper()\n"
        )


def test_same_named_callers_are_not_merged(tmp_path: Path):
    _write_two_modules(tmp_path)
    callees, callers = build_cross_file_call_graph(build_index(tmp_path), tmp_path)

    assert "alpha::run" in callees
    assert "beta::run" in callees


def test_bare_call_resolves_to_same_file_definition(tmp_path: Path):
    _write_two_modules(tmp_path)
    callees, callers = build_cross_file_call_graph(build_index(tmp_path), tmp_path)

    assert callees["alpha::run"] == {"alpha::helper"}
    assert callees["beta::run"] == {"beta::helper"}
    assert callers["alpha::helper"] == {"alpha::run"}
    assert callers["beta::helper"] == {"beta::run"}


def test_from_import_resolves_to_imported_module(tmp_path: Path):
    _write_two_modules(tmp_path)
    (tmp_path / "main.py").write_text(
        "from beta import helper\n\n\ndef go():\n    return helper()\n"
    )
    callees, _ = build_cross_file_call_graph(build_index(tmp_path), tmp_path)

    assert callees["main::go"] == {"beta::helper"}


def test_module_import_resolves_to_imported_module(tmp_path: Path):
    _write_two_modules(tmp_path)
    (tmp_path / "main.py").write_text(
        "import beta\n\n\ndef go():\n    return beta.helper()\n"
    )
    callees, _ = build_cross_file_call_graph(build_index(tmp_path), tmp_path)

    assert callees["main::go"] == {"beta::helper"}
