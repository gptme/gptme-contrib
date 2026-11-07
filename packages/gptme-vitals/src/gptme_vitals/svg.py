"""Standard-library SVG primitives — the movable part of the dashboard skeleton.

Four renderers (``html_escape``, ``bar_svg``, ``donut_svg``, ``sparkline_svg``)
that know nothing about any one agent's pages. Page code stays in the agent's
own repo; this module is stdlib-only so the shared package stays dependency-free,
and ``tests/test_svg.py`` fails if a non-stdlib import creeps in.

Everything here is pure: strings in, SVG out, no workspace state. Colours are
CSS custom properties (``var(--text)``, ``var(--chart-blue)``, ``var(--dim)``),
so the caller owns the palette and these stay theme-agnostic.
"""

from __future__ import annotations

import math

__all__ = ["bar_svg", "donut_svg", "html_escape", "sparkline_svg"]


def html_escape(s: str) -> str:
    """Minimal HTML escaping."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def bar_svg(values: list[tuple[str, int, str]], max_val: int | None = None) -> str:
    """Generate inline SVG horizontal bar chart."""
    if not values:
        return "<p>No data</p>"
    if max_val is None:
        max_val = max(v for _, v, _ in values) if values else 1
    bar_h = 22
    gap = 4
    # Size label column to fit the longest label; only truncate beyond the cap
    # so distinct entries like `claude-code:claude-sonnet-4-6` and
    # `claude-code:claude-opus-4-7` stay distinguishable.
    max_label_chars = 40
    longest = max(len(label) for label, _, _ in values)
    label_chars = min(longest, max_label_chars)
    label_w = label_chars * 7 + 10
    bar_w = 200
    total_h = (bar_h + gap) * len(values) + gap
    svg_w = label_w + bar_w + 60

    lines = [
        f'<svg width="{svg_w}" height="{total_h}" xmlns="http://www.w3.org/2000/svg">',
    ]
    for i, (label, val, color) in enumerate(values):
        y = i * (bar_h + gap) + gap
        w = int(val / max(max_val, 1) * bar_w)
        label_esc = html_escape(label[:max_label_chars])
        lines.append(
            f'  <text x="{label_w - 4}" y="{y + bar_h - 6}" '
            f'text-anchor="end" fill="var(--text)" font-size="12" font-family="inherit">'
            f"{label_esc}</text>",
        )
        lines.append(
            f'  <rect x="{label_w}" y="{y}" width="{w}" height="{bar_h}" '
            f'rx="3" fill="{color}" opacity="0.85"/>',
        )
        lines.append(
            f'  <text x="{label_w + w + 6}" y="{y + bar_h - 6}" '
            f'fill="var(--dim)" font-size="12" font-family="inherit">{val}</text>',
        )
    lines.append("</svg>")
    return "\n".join(lines)


def donut_svg(segments: list[tuple[str, int, str]], size: int = 120) -> str:
    """Generate inline SVG donut chart."""
    total = sum(v for _, v, _ in segments)
    if total == 0:
        return "<p>No data</p>"

    cx = cy = size // 2
    r = size // 2 - 10
    inner_r = r * 0.55
    svg = [
        f'<svg width="{size + 140}" height="{size}" xmlns="http://www.w3.org/2000/svg">',
    ]

    start_angle: float = -90  # Start from top
    for _label, val, color in segments:
        if val == 0:
            continue
        angle = val / total * 360
        end_angle = start_angle + angle

        # Convert to radians
        s_rad = math.radians(start_angle)
        e_rad = math.radians(end_angle)

        # Arc points
        x1 = cx + r * math.cos(s_rad)
        y1 = cy + r * math.sin(s_rad)
        x2 = cx + r * math.cos(e_rad)
        y2 = cy + r * math.sin(e_rad)
        ix1 = cx + inner_r * math.cos(e_rad)
        iy1 = cy + inner_r * math.sin(e_rad)
        ix2 = cx + inner_r * math.cos(s_rad)
        iy2 = cy + inner_r * math.sin(s_rad)

        large_arc = 1 if angle > 180 else 0

        d = (
            f"M {x1:.1f} {y1:.1f} "
            f"A {r} {r} 0 {large_arc} 1 {x2:.1f} {y2:.1f} "
            f"L {ix1:.1f} {iy1:.1f} "
            f"A {inner_r} {inner_r} 0 {large_arc} 0 {ix2:.1f} {iy2:.1f} Z"
        )
        svg.append(f'  <path d="{d}" fill="{color}" opacity="0.85"/>')
        start_angle = end_angle

    # Center label
    svg.append(
        f'  <text x="{cx}" y="{cy + 5}" text-anchor="middle" '
        f'fill="var(--text)" font-size="16" font-weight="bold" font-family="inherit">{total}</text>',
    )

    # Legend
    legend_x = size + 10
    for i, (label, val, color) in enumerate(segments):
        ly = 20 + i * 22
        svg.append(
            f'  <rect x="{legend_x}" y="{ly - 10}" width="12" height="12" rx="2" fill="{color}"/>',
        )
        pct = val / total * 100
        svg.append(
            f'  <text x="{legend_x + 18}" y="{ly}" fill="var(--dim)" font-size="11" '
            f'font-family="inherit">{html_escape(label)} ({pct:.0f}%)</text>',
        )

    svg.append("</svg>")
    return "\n".join(svg)


def sparkline_svg(values: list[int], width: int = 200, height: int = 40) -> str:
    """Generate inline SVG sparkline."""
    if not values or max(values) == 0:
        return ""
    max_v = max(values)
    n = len(values)
    step = width / max(n - 1, 1)

    points = []
    for i, v in enumerate(values):
        x = i * step
        y = height - (v / max_v * (height - 4)) - 2
        points.append(f"{x:.1f},{y:.1f}")

    pts = " ".join(points)
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        f'  <polyline points="{pts}" fill="none" stroke="var(--chart-blue)" stroke-width="2"/>'
        + "".join(
            f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="2.5" fill="var(--chart-blue)"/>'
            for p in points
        )
        + "</svg>"
    )
