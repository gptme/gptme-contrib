"""Guard the SVG primitives' no-workspace-imports contract."""

from __future__ import annotations

import ast
from pathlib import Path

import gptme_vitals.svg as svg_module

SVG_PATH = Path(svg_module.__file__)


def test_svg_module_imports_only_stdlib():
    tree = ast.parse(SVG_PATH.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add("." * node.level + (node.module or "").split(".")[0])
    assert roots == {"__future__", "math"}


def test_primitives_emit_svg_and_escape():
    assert svg_module.html_escape("<a&b>") == "&lt;a&amp;b&gt;"
    outs = [
        svg_module.bar_svg([("a", 1, "#111"), ("b", 3, "#222")]),
        svg_module.donut_svg([("a", 1, "#111"), ("b", 3, "#222")]),
        svg_module.sparkline_svg([1, 3, 2]),
    ]
    assert all(o.startswith("<svg") and o.rstrip().endswith("</svg>") for o in outs)
