#!/usr/bin/env python3
"""One-liner: render a presentation deck and optionally open it in a browser.

Usage:
    python3 skills/agentic-presentation/present.py deck.json
    python3 skills/agentic-presentation/present.py deck.json --live
    python3 skills/agentic-presentation/present.py deck.json -o slides.html --live
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="presentation JSON file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output HTML path (default: temp file when --live, else index.html)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="open rendered deck in the default browser",
    )
    parser.add_argument(
        "--max-kb",
        type=int,
        default=500,
        help="fail if generated HTML exceeds this many KB (default: 500)",
    )
    args = parser.parse_args(argv)

    skill_dir = Path(__file__).parent
    generator = skill_dir / "generate_presentation_html.py"

    if args.live and not args.output:
        tmp_dir = Path(tempfile.mkdtemp())
        output = tmp_dir / f"{args.input.stem}.html"
    else:
        output = args.output or Path("index.html")

    cmd = [
        sys.executable,
        str(generator),
        str(args.input),
        "-o",
        str(output),
        "--max-kb",
        str(args.max_kb),
    ]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        return result.returncode

    if args.live:
        _open_browser(output)

    return 0


def _open_browser(path: Path) -> None:
    for cmd in ("xdg-open", "open"):
        if shutil.which(cmd):
            subprocess.run([cmd, str(path)], check=False)
            return
    print(f"[present] browser not found — open manually: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
