"""Tests for gptme_cc_memory.schema — frontmatter parsing and validation."""

from __future__ import annotations

from pathlib import Path


from gptme_cc_memory.schema import (
    discover_memory_files,
    load_yaml_frontmatter,
    validate_memory_file,
)

# Valid feedback memory
VALID_FEEDBACK_MD = """\
---
name: never-skip-precommit
description: Never bypass pre-commit hooks with --no-verify
metadata:
  type: feedback
---

Never use --no-verify to bypass pre-commit hooks.

**Why:** Prior incident where bypassing hooks caused a broken migration.
**How to apply:** Fix the underlying hook failure.
"""

# Valid user memory
VALID_USER_MD = """\
---
name: prefers-python-typing
description: User prefers Python type hints on all signatures
metadata:
  type: user
---

Always use Python typing hints.

**Why:** The user values type safety.
**How to apply:** Add return type annotations to all functions.
"""

# Valid project memory
VALID_PROJECT_MD = """\
---
name: current-sprint-migration
description: Database migration sprint goals and constraints
metadata:
  type: project
---

Current sprint focuses on PostgreSQL migration. Key constraint: zero downtime.
Don't change the schema in ways that require table rewrites.
"""

# Invalid — missing name
MISSING_NAME_MD = """\
---
metadata:
  type: feedback
---

No name field here.
"""

# Invalid — wrong type
WRONG_TYPE_MD = """\
---
name: some-memory
description: A test memory
metadata:
  type: invalid_type
---

This has an invalid type.
"""

# No frontmatter at all
NO_FRONTMATTER_MD = """\
Just a plain markdown file without frontmatter.
"""


class TestLoadYamlFrontmatter:
    def test_valid_frontmatter(self):
        metadata, body = load_yaml_frontmatter(VALID_FEEDBACK_MD)
        assert metadata["name"] == "never-skip-precommit"
        assert metadata["metadata"]["type"] == "feedback"
        assert "Never use --no-verify" in body

    def test_no_frontmatter(self):
        metadata, body = load_yaml_frontmatter(NO_FRONTMATTER_MD)
        assert metadata == {}
        assert "plain markdown" in body

    def test_empty_string(self):
        metadata, body = load_yaml_frontmatter("")
        assert metadata == {}
        assert body == ""


class TestValidateMemoryFile:
    def test_valid_feedback(self):
        metadata, body = load_yaml_frontmatter(VALID_FEEDBACK_MD)
        errors = validate_memory_file(metadata, body)
        assert errors == []

    def test_valid_user(self):
        metadata, body = load_yaml_frontmatter(VALID_USER_MD)
        errors = validate_memory_file(metadata, body)
        assert errors == []

    def test_valid_project(self):
        metadata, body = load_yaml_frontmatter(VALID_PROJECT_MD)
        errors = validate_memory_file(metadata, body)
        assert errors == []

    def test_missing_name(self):
        metadata, body = load_yaml_frontmatter(MISSING_NAME_MD)
        errors = validate_memory_file(metadata, body)
        error_texts = " ".join(errors).lower()
        assert "name" in error_texts

    def test_invalid_type(self):
        metadata, body = load_yaml_frontmatter(WRONG_TYPE_MD)
        errors = validate_memory_file(metadata, body)
        error_texts = " ".join(errors).lower()
        assert "invalid_type" in error_texts
        assert "type" in error_texts

    def test_no_frontmatter_validates(self):
        metadata, body = load_yaml_frontmatter(NO_FRONTMATTER_MD)
        errors = validate_memory_file(metadata, body)
        assert len(errors) >= 1  # Missing name, type, etc.


class TestDiscoverMemoryFiles:
    def test_discover_files(self, tmp_path: Path):
        # Create a few valid memory files
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()

        (mem_dir / "feedback-precommit.md").write_text(VALID_FEEDBACK_MD)
        (mem_dir / "user-typing.md").write_text(VALID_USER_MD)
        (mem_dir / "project-db.md").write_text(VALID_PROJECT_MD)

        # Special files should be skipped
        (mem_dir / "MEMORY.md").write_text("# Index")
        (mem_dir / "guidance.md").write_text("Some guidance")
        (mem_dir / "pending-items.md").write_text("Items")

        # Invalid file
        (mem_dir / "invalid.md").write_text(NO_FRONTMATTER_MD)

        entries = discover_memory_files(mem_dir)
        assert len(entries) == 3

        types = {e.type for e in entries}
        assert types == {"feedback", "user", "project"}

    def test_empty_directory(self, tmp_path: Path):
        entries = discover_memory_files(tmp_path / "nonexistent")
        assert entries == []

        entries = discover_memory_files(tmp_path)
        assert entries == []

    def test_memory_file_type_default(self, tmp_path: Path):
        # A file with type "memory" (not in MEMORY_TYPES) should be treated as
        # a generic entry — parseable but with a fallback type string.
        md = """\
---
name: generic-entry
description: Something useful
metadata:
  type: memory
---

Some useful content.
"""
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "generic.md").write_text(md)

        entries = discover_memory_files(mem_dir)
        assert len(entries) == 1
        assert entries[0].type == "memory"
