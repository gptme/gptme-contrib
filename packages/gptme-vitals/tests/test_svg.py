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


@pytest.mark.parametrize("count,size", [(1, 120), (6, 120), (10, 120), (6, 200)])
def test_donut_viewport_contains_every_legend_row(count, size):
    segments = [(f"category {i}", i, "#123456") for i in range(1, count + 1)]
    root = ET.fromstring(svg_module.donut_svg(segments, size=size))
    height = float(root.attrib["height"])
    assert height >= size
    labels = root.findall("{http://www.w3.org/2000/svg}text")[1:]
    assert len(labels) == count
    for label in labels:
        assert float(label.attrib["y"]) + 4 <= height
    for swatch in root.findall("{http://www.w3.org/2000/svg}rect"):
        assert float(swatch.attrib["y"]) + float(swatch.attrib["height"]) <= height


@pytest.mark.parametrize("size", [120, 200])
@pytest.mark.parametrize(
    "label", ["short", "claude-code:claude-sonnet-4-6", "<wide & long>" * 5]
)
def test_donut_viewport_grows_to_fit_legend_text(size, label):
    root = ET.fromstring(svg_module.donut_svg([(label, 4, "#123456")], size=size))
    text = root.findall("{http://www.w3.org/2000/svg}text")[1]
    assert text.text == f"{label} (100%)"
    assert (
        float(root.attrib["width"]) >= float(text.attrib["x"]) + len(text.text) * 7 + 10
    )


@pytest.mark.parametrize("renderer", ["bar", "donut_wedges", "donut_ring"])
def test_palette_values_cannot_inject_svg_attributes(renderer):
    color = 'red" onmouseover="alert(1)&<>'
    segments = [("a", 1, color)]
    if renderer == "donut_wedges":
        segments.append(("b", 2, color))
    output = (
        svg_module.bar_svg(segments)
        if renderer == "bar"
        else svg_module.donut_svg(segments)
    )
    root = ET.fromstring(output)
    palette_values = []
    for element in root.iter():
        assert "onmouseover" not in element.attrib
        for attr in ("fill", "stroke"):
            if element.attrib.get(attr) == color:
                palette_values.append(element.attrib[attr])
    assert len(palette_values) == (1 if renderer == "bar" else len(segments) * 2)


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
