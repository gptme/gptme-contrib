"""Python defs nested in compound statements are extracted (gptme/gptme-contrib#2356, C5)."""

from pathlib import Path

import pytest
from gptme_codegraph import core
from gptme_codegraph.core import (
    SqliteIndexCache,
    build_call_graph,
    build_cross_file_call_graph,
    build_index,
    extract_symbols,
)

SOURCE = """\
import sys

if sys.platform == "win32":
    def on_windows():
        pass
elif sys.platform == "darwin":
    def on_mac():
        pass
else:
    def on_other():
        pass

try:
    import fast
    def via_fast():
        pass
except ImportError:
    def via_fallback():
        pass
else:
    def try_else():
        pass
finally:
    def in_finally():
        pass

with open(__file__) as fh:
    class InWith:
        def method_in_with(self):
            if True:
                def not_a_symbol():
                    pass

for _ in range(1):
    def in_for():
        pass

while False:
    def in_while():
        pass

match sys.argv:
    case [_]:
        def in_case():
            pass

class Outer:
    if sys.version_info >= (3, 12):
        def conditional_method(self):
            pass
    else:
        def fallback_method(self):
            pass
"""


def test_defs_in_compound_statements_are_extracted(tmp_path: Path):
    path = tmp_path / "mod.py"
    path.write_text(SOURCE)
    by_name = {s.name: s for s in extract_symbols(path)}

    for name in (
        "on_windows",
        "on_mac",
        "on_other",
        "via_fast",
        "via_fallback",
        "try_else",
        "in_finally",
        "InWith",
        "in_for",
        "in_while",
        "in_case",
        "Outer",
    ):
        assert name in by_name, name
        assert by_name[name].parent_class is None, name

    assert by_name["method_in_with"].kind == "method"
    assert by_name["method_in_with"].parent_class == "InWith"
    for name in ("conditional_method", "fallback_method"):
        assert by_name[name].kind == "method", name
        assert by_name[name].parent_class == "Outer", name

    # Function bodies stay opaque, as before: nested helpers are not symbols.
    assert "not_a_symbol" not in by_name


BRANCHES = """\
try:
    def run():
        fast()
except ImportError:
    def run():
        slow()

def fast():
    pass

def slow():
    pass
"""


@pytest.mark.parametrize("cross_file", [False, True])
def test_calls_from_every_branch_definition_are_kept(tmp_path: Path, cross_file: bool):
    path = tmp_path / "mod.py"
    path.write_text(BRANCHES)
    if cross_file:
        callees, _ = build_cross_file_call_graph(build_index(tmp_path), tmp_path)
        key = "mod::run"
    else:
        callees, _ = build_call_graph(extract_symbols(path))
        key = "::run"

    assert {c.rpartition("::")[2] for c in callees[key]} == {"fast", "slow"}


def test_index_cached_by_previous_extractor_is_discarded(tmp_path: Path, monkeypatch):
    """A schema-4 index lacks the conditional defs, so it must not be served."""
    monkeypatch.setattr(core, "_CODEGRAPH_STATE_DIR", tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    (project / "mod.py").write_text("def plain():\n    pass\n")
    old = SqliteIndexCache(str(project))
    monkeypatch.setattr(old, "SCHEMA_VERSION", 4)
    old.save(build_index(project))
    old.close()

    current = SqliteIndexCache(str(project))
    loaded = current.load()
    current.close()
    assert loaded is None or not loaded.has("plain")
