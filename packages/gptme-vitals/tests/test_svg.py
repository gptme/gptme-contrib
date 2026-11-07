"""Guard the SVG primitives' no-workspace-imports contract."""

from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

import gptme_vitals.svg as svg_module
import pytest

SVG_PATH = Path(svg_module.__file__)


@pytest.mark.parametrize("zeros", [False, True])
def test_single_category_donut_draws_a_complete_ring(zeros):
    segments = [("only", 4, "#123456")]
    if zeros:
        segments = [("before", 0, "#111111"), *segments, ("after", 0, "#222222")]
    root = ET.fromstring(svg_module.donut_svg(segments))
    rings = root.findall("{http://www.w3.org/2000/svg}circle")
    assert len(rings) == 1
    ring = rings[0]
    assert ring.attrib["fill"] == "none"
    assert ring.attrib["stroke"] == "#123456"
    assert float(ring.attrib["cx"]) == float(ring.attrib["cy"]) == 60
    radius = float(ring.attrib["r"])
    half_width = float(ring.attrib["stroke-width"]) / 2
    assert radius + half_width == 50
    assert radius - half_width == 27.5


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
