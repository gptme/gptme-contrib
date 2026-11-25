"""Same-named symbols in different files must stay separate graph nodes (C4, gptme/gptme-contrib#2356)."""

from __future__ import annotations

from pathlib import Path

import pytest
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


def _graph(root: Path, files: dict[str, str]) -> dict[str, set[str]]:
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    callees, _ = build_cross_file_call_graph(build_index(root), root)
    return callees


# Each import must pick ``beta``'s helper, which sorts after ``alpha``'s.
@pytest.mark.parametrize(
    ("grammar", "files", "caller", "callee"),
    [
        pytest.param(
            "typescript",
            {
                "alpha.ts": "export function helper() {}\n",
                "beta.ts": "export function helper() {}\n",
                "main.ts": "import { helper } from './beta';\n"
                "function go() { helper(); }\n",
            },
            "main::go",
            "beta::helper",
            id="ts-relative",
        ),
        pytest.param(
            "typescript",
            {
                "beta.ts": "export function helper() {}\n",
                "app/beta.ts": "export function helper() {}\n",
                "app/main.ts": "import { helper } from '../beta.js';\n"
                "function go() { helper(); }\n",
            },
            "app.main::go",
            "beta::helper",
            id="ts-parent-dir-with-js-extension",
        ),
        pytest.param(
            "typescript",
            {
                "alpha/index.ts": "export function helper() {}\n",
                "beta/index.ts": "export function helper() {}\n",
                "main.ts": "import * as b from './beta';\n"
                "function go() { b.helper(); }\n",
            },
            "main::go",
            "beta.index::helper",
            id="ts-namespace-directory-index",
        ),
        pytest.param(
            "typescript",
            {
                "lib/alpha.ts": "export function helper() {}\n",
                "lib/beta.ts": "export function helper() {}\n",
                "main.ts": "import { helper } from '@/lib/beta';\n"
                "function go() { helper(); }\n",
            },
            "main::go",
            "lib.beta::helper",
            id="ts-path-alias",
        ),
        pytest.param(
            "rust",
            {
                "src/alpha.rs": "pub fn helper() {}\n",
                "src/beta.rs": "pub fn helper() {}\n",
                "src/main.rs": "use crate::beta::helper;\nfn go() { helper(); }\n",
            },
            "src.main::go",
            "src.beta::helper",
            id="rust-use",
        ),
        pytest.param(
            "rust",
            {
                "src/alpha/mod.rs": "pub fn helper() {}\n",
                "src/beta/mod.rs": "pub fn helper() {}\n",
                "src/main.rs": "fn go() { crate::beta::helper(); }\n",
            },
            "src.main::go",
            "src.beta.mod::helper",
            id="rust-qualified-path",
        ),
        pytest.param(
            "go",
            {
                "alpha/util.go": "package alpha\n\nfunc Helper() {}\n",
                "beta/util.go": "package beta\n\nfunc Helper() {}\n",
                "main.go": 'package main\n\nimport "github.com/x/repo/beta"\n\n'
                "func run() { beta.Helper() }\n",
            },
            "main::run",
            "beta.util::Helper",
            id="go-package-directory",
        ),
        pytest.param(
            "python",
            {
                "beta.py": "def helper():\n    pass\n",
                "pkg/beta.py": "def helper():\n    pass\n",
                "pkg/main.py": "from .beta import helper\n\n\n"
                "def go():\n    helper()\n",
            },
            "pkg.main::go",
            "pkg.beta::helper",
            id="python-relative",
        ),
        pytest.param(
            "python",
            {
                "alpha.py": "def helper():\n    pass\n",
                "pkg/__init__.py": "",
                "pkg/beta.py": "def helper():\n    pass\n",
                "pkg/main.py": "from . import beta\n\n\n"
                "def go():\n    beta.helper()\n",
            },
            "pkg.main::go",
            "pkg.beta::helper",
            id="python-relative-submodule",
        ),
        pytest.param(
            "python",
            {
                "beta.py": "class Aaa:\n    def helper(self):\n        pass\n\n\n"
                "def helper():\n    pass\n",
                "main.py": "from beta import helper\n\n\ndef go():\n    helper()\n",
            },
            "main::go",
            "beta::helper",
            id="python-import-prefers-top-level",
        ),
    ],
)
def test_import_resolves_in_importer_language(
    tmp_path: Path,
    grammar: str,
    files: dict[str, str],
    caller: str,
    callee: str,
):
    pytest.importorskip(f"tree_sitter_{grammar}")
    assert _graph(tmp_path, files)[caller] == {callee}


def test_implicit_this_call_prefers_callers_class(tmp_path: Path):
    pytest.importorskip("tree_sitter_java")
    classes = "".join(
        f"class {name} {{ void validate() {{}} void process() {{ validate(); }} }}\n"
        for name in ("A", "B")
    )
    callees = _graph(tmp_path, {"Mod.java": classes})

    assert callees["Mod::A.process"] == {"Mod::A.validate"}
    assert callees["Mod::B.process"] == {"Mod::B.validate"}


def test_python_bare_call_in_method_prefers_top_level(tmp_path: Path):
    source = (
        "def validate():\n    pass\n\n\n"
        "class B:\n    def validate(self):\n        pass\n\n"
        "    def process(self):\n        validate()\n"
    )
    callees = _graph(tmp_path, {"mod.py": source})

    assert callees["mod::B.process"] == {"mod::validate"}
