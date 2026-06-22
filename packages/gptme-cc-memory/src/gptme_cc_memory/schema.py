"""Memory type definitions, frontmatter parsing, and file validation.

The four memory types encode different *what-to-do-with-this* semantics:

- **user**: Who the user is — role, expertise, preferences, context
- **feedback**: Behavioral rules from corrections or confirmations (always inject)
- **project**: Ongoing work, goals, decisions, deadlines (situational)
- **reference**: Where to find things in external systems (system-triggered)

Every memory file must have YAML frontmatter with at minimum a ``name`` and
``metadata.type`` field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Valid memory types and their display order / priority tier
MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference"})

# Files that are not memory entries
SPECIAL_MEMORY_FILES = frozenset(
    {
        "MEMORY.md",
        "guidance.md",
        "pending-items.md",
        "pending-updates.md",
        "pending-session-context.md",
    }
)

# Type priority for injection (higher = always inject first)
TYPE_INJECTION_PRIORITY: dict[str, int] = {
    "feedback": 4,
    "user": 3,
    "project": 2,
    "reference": 1,
}

# Default confidence by type (used when metadata.json has no entry)
DEFAULT_CONFIDENCE_BY_TYPE: dict[str, float] = {
    "feedback": 0.88,
    "project": 0.78,
    "reference": 0.72,
}

# Score boost by type for retrieval
TYPE_BOOST: dict[str, float] = {
    "feedback": 1.10,
    "project": 1.00,
    "reference": 0.96,
}


@dataclass
class MemoryFile:
    """A parsed memory file with validated frontmatter."""

    path: Path
    name: str
    description: str
    type: str
    body: str
    excerpt: str
    aliases: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def default_confidence(self) -> float:
        return DEFAULT_CONFIDENCE_BY_TYPE.get(self.type, 0.7)

    @property
    def type_boost(self) -> float:
        return TYPE_BOOST.get(self.type, 1.0)

    @property
    def injection_priority(self) -> int:
        return TYPE_INJECTION_PRIORITY.get(self.type, 0)


def load_yaml_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from a markdown file.

    Returns (metadata, body) where metadata is the parsed YAML and body is
    everything after the frontmatter delimiter.
    """
    content = content.strip()
    if not content.startswith("---"):
        return {}, content

    # Strip the opening --- delimiter
    after_opening = content[3:].lstrip("\n")
    consumed = len(content) - len(after_opening)

    # Find closing --- on its own line
    end_match = re.search(r"^---\s*$", after_opening, re.MULTILINE)
    if not end_match:
        return {}, content

    # YAML text is everything between the two delimiters
    yaml_text = after_opening[: end_match.start()].strip()
    body_start = consumed + end_match.end()
    body = content[body_start:].strip()

    try:
        metadata = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return {}, body

    if not isinstance(metadata, dict):
        return {}, body

    return metadata, body


def validate_memory_file(metadata: dict[str, Any], body: str) -> list[str]:
    """Validate a memory file's frontmatter and body.

    Returns a list of validation error messages (empty list = valid).
    """
    errors: list[str] = []

    if (
        "name" not in metadata
        or not isinstance(metadata["name"], str)
        or not metadata["name"].strip()
    ):
        errors.append("Missing or empty 'name' field in frontmatter")

    if "description" not in metadata or not isinstance(metadata["description"], str):
        errors.append("Missing or non-string 'description' field in frontmatter")

    mem_type = metadata.get("metadata", {}).get("type", "")
    if not mem_type or mem_type not in MEMORY_TYPES:
        valid_types = ", ".join(sorted(MEMORY_TYPES))
        errors.append(
            f"Invalid or missing 'metadata.type': got {mem_type!r}, "
            f"expected one of: {valid_types}"
        )

    if not body or not body.strip():
        errors.append("Memory body is empty")

    return errors


def parse_memory_file(path: Path) -> MemoryFile | None:
    """Parse and validate a single memory file."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    metadata, body = load_yaml_frontmatter(content)
    if not metadata:
        return None

    mem_type = str(metadata.get("metadata", {}).get("type", "memory")).strip().lower()
    if mem_type not in MEMORY_TYPES:
        mem_type = "memory"

    name = str(metadata.get("name", path.stem)).strip()
    description = str(metadata.get("description", "")).strip()

    # Build excerpt
    text = re.sub(r"\s+", " ", body).strip()
    excerpt = text[:217] + "..." if len(text) > 220 else text

    # Build aliases from name, filename, and explicit aliases field
    aliases: list[str] = []
    seen: set[str] = set()
    for candidate in [name, path.stem] + metadata.get("aliases", []):
        normalized = candidate.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            aliases.append(candidate.strip())

    return MemoryFile(
        path=path,
        name=name,
        description=description,
        type=mem_type,
        body=body.strip(),
        excerpt=excerpt,
        aliases=aliases,
        metadata=metadata.get("metadata", {}),
    )


def discover_memory_files(memory_dir: Path) -> list[MemoryFile]:
    """Discover all valid memory files in a directory.

    Skips special files (MEMORY.md, guidance.md, pending-*.md) and files that
    fail to parse.
    """
    entries: list[MemoryFile] = []
    if not memory_dir.is_dir():
        return entries

    for path in sorted(memory_dir.glob("*.md")):
        if path.name in SPECIAL_MEMORY_FILES:
            continue
        entry = parse_memory_file(path)
        if entry is not None:
            entries.append(entry)

    return entries
