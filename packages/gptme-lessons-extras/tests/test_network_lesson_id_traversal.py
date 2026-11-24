"""Security: network lesson_id from untrusted frontmatter must not escape lessons_dir.

Network lessons come from the shared gptme/gptme-lessons repo (any agent can
push). Both adoption paths derive the local write path from
frontmatter['network']['lesson_id'] = "{agent}-{category}-{slug}" with no
containment check.
"""

import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PKG = SRC / "gptme_lessons_extras"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(PKG))  # adopt.py falls back to `import review`


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PKG / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MALICIOUS_ID = "evil-..-../../pwned"  # category="..", slug="../../pwned"

LESSON = f"""---
match:
  keywords: [harmless]
network:
  lesson_id: "{MALICIOUS_ID}"
  agent_origin: evil
  created: "2026-01-01T00:00:00"
  updated: "2026-01-01T00:00:00"
  confidence: 0.9
  adoption_count: 0
  schema_version: "1.0"
---

# Harmless lesson
PAYLOAD
"""


def _layout(tmp_path: Path):
    root = tmp_path / "root"
    lessons_dir = root / "agent" / "lessons"
    lessons_dir.mkdir(parents=True)
    network_dir = root / "network"
    (network_dir / "evil" / "workflow").mkdir(parents=True)
    lesson_file = network_dir / "evil" / "workflow" / "x.md"
    lesson_file.write_text(LESSON)
    return root, lessons_dir, network_dir, lesson_file


def _escaped_files(tmp_path: Path, lessons_dir: Path, network_dir: Path):
    """Every *.md under tmp_path that is neither in lessons_dir nor network_dir."""
    return [
        p
        for p in tmp_path.rglob("*.md")
        if not p.resolve().is_relative_to(lessons_dir.resolve())
        and not p.resolve().is_relative_to(network_dir.resolve())
    ]


def test_import_adopt_network_lesson_refuses_traversal(tmp_path):
    imp = _load("lessons_import", "import.py")
    root, lessons_dir, network_dir, lesson_file = _layout(tmp_path)

    imp.adopt_network_lesson(lesson_file, lessons_dir)

    escaped = _escaped_files(tmp_path, lessons_dir, network_dir)
    assert escaped == [], f"lesson written outside lessons_dir: {escaped}"


def test_adopt_lesson_refuses_traversal(tmp_path):
    adopt = _load("lessons_adopt", "adopt.py")
    review = _load("lessons_review", "review.py")
    root, lessons_dir, network_dir, _lesson_file = _layout(tmp_path)

    [meta] = review.list_network_lessons(network_dir, agent="me")
    assert meta["network"]["lesson_id"] == MALICIOUS_ID
    adopt.adopt_lesson(meta, network_dir, lessons_dir)

    escaped = _escaped_files(tmp_path, lessons_dir, network_dir)
    assert escaped == [], f"lesson written outside lessons_dir: {escaped}"
