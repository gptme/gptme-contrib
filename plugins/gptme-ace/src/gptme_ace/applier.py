#!/usr/bin/env python3
"""
ACE Applier: Apply approved deltas to lesson files.

Part of ACE Phase 5 utilities for lesson lifecycle management.

The Applier:
1. Loads approved deltas from deltas/approved/
2. Applies DeltaOperations (ADD, REMOVE, MODIFY) to lesson files
3. Tracks application metadata
4. Archives applied deltas

Usage:
    python -m gptme_ace.applier apply --delta-id abc123
    python -m gptme_ace.applier batch --all
    python -m gptme_ace.applier status
"""

import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .curator import Delta, DeltaOperation

logger = logging.getLogger(__name__)


class ApplierError(Exception):
    """Error during delta application"""

    pass


class DeltaApplier:
    """Apply approved deltas to lesson files"""

    def __init__(
        self,
        lessons_dir: Optional[Path] = None,
        delta_dir: Optional[Path] = None,
        dry_run: bool = False,
    ):
        """
        Initialize the DeltaApplier.

        Args:
            lessons_dir: Directory containing lesson files (default: ./lessons)
            delta_dir: Directory containing delta files (default: ./deltas)
            dry_run: If True, show what would be done without making changes
        """
        self.lessons_dir = lessons_dir or Path("lessons")
        self.delta_dir = delta_dir or Path("deltas")
        self.dry_run = dry_run

        # Ensure applied directory exists
        self.applied_dir = self.delta_dir / "applied"
        if not dry_run:
            self.applied_dir.mkdir(exist_ok=True)

    def load_delta(self, delta_id: str) -> Delta:
        """Load a delta from approved directory"""
        delta_path = self.delta_dir / "approved" / f"{delta_id}.json"
        if not delta_path.exists():
            # Also check pending for status
            pending_path = self.delta_dir / "pending" / f"{delta_id}.json"
            if pending_path.exists():
                raise ApplierError(
                    f"Delta {delta_id} is still pending approval. "
                    "Move to approved/ before applying."
                )
            raise ApplierError(f"Delta {delta_id} not found in approved/")

        delta_dict = json.loads(delta_path.read_text())
        return Delta(
            delta_id=delta_dict["delta_id"],
            created=delta_dict["created"],
            source=delta_dict["source"],
            source_insights=delta_dict["source_insights"],
            lesson_id=delta_dict["lesson_id"],
            operations=[
                DeltaOperation(
                    type=op["type"],
                    section=op["section"],
                    content=op.get("content"),
                    position=op.get("position"),
                    target=op.get("target"),
                )
                for op in delta_dict["operations"]
            ],
            rationale=delta_dict["rationale"],
            review_status=delta_dict["review_status"],
            applied_at=delta_dict.get("applied_at"),
            applied_by=delta_dict.get("applied_by"),
        )

    def find_lesson_file(self, lesson_id: str) -> Optional[Path]:
        """Find the lesson file for a given lesson_id"""
        # lesson_id format: category/name (e.g., workflow/git-workflow)
        # File path: lessons/category/name.md

        # Try direct path first
        lesson_path = self.lessons_dir / f"{lesson_id}.md"
        if lesson_path.exists():
            return lesson_path

        # Try searching by filename
        name = lesson_id.split("/")[-1] if "/" in lesson_id else lesson_id
        for path in self.lessons_dir.rglob(f"{name}.md"):
            return path

        return None

    def apply_delta(self, delta: Delta, applied_by: str = "ace_applier") -> dict:
        """
        Apply a delta to its target lesson file.

        Args:
            delta: The Delta to apply
            applied_by: Identifier for who/what is applying (for audit)

        Returns:
            dict with application status and details
        """
        result = {
            "delta_id": delta.delta_id,
            "lesson_id": delta.lesson_id,
            "operations_applied": 0,
            "operations_failed": 0,
            "errors": [],
            "dry_run": self.dry_run,
        }

        # Find the lesson file
        lesson_path = self.find_lesson_file(delta.lesson_id)
        if not lesson_path:
            result["errors"].append(f"Lesson file not found: {delta.lesson_id}")
            return result

        # Read current content
        original_content = lesson_path.read_text()
        modified_content = original_content

        # Apply each operation
        for op in delta.operations:
            try:
                modified_content = self._apply_operation(
                    modified_content, op, delta.lesson_id
                )
                result["operations_applied"] += 1
            except ApplierError as e:
                result["operations_failed"] += 1
                result["errors"].append(str(e))
                logger.warning(f"Failed to apply operation: {e}")

        # If dry run, just report what would happen
        if self.dry_run:
            result["would_modify"] = modified_content != original_content
            if modified_content != original_content:
                result["diff_preview"] = self._generate_diff_preview(
                    original_content, modified_content
                )
            return result

        # Write modified content
        if modified_content != original_content:
            lesson_path.write_text(modified_content)
            result["file_modified"] = True
        else:
            result["file_modified"] = False

        # Update delta with application metadata
        delta.applied_at = datetime.now(timezone.utc).isoformat()
        delta.applied_by = applied_by

        # Move delta to applied directory
        self._archive_applied_delta(delta)

        return result

    def _apply_operation(self, content: str, op: DeltaOperation, lesson_id: str) -> str:
        """Apply a single DeltaOperation to content"""
        if op.type == "add":
            return self._apply_add(content, op)
        elif op.type == "remove":
            return self._apply_remove(content, op)
        elif op.type == "modify":
            return self._apply_modify(content, op)
        else:
            raise ApplierError(f"Unknown operation type: {op.type}")

    def _apply_add(self, content: str, op: DeltaOperation) -> str:
        """Apply ADD operation - add content to a section"""
        if not op.content:
            raise ApplierError("ADD operation requires content")

        section = op.section
        position = op.position or "append"

        # Find the section in the content
        section_pattern = rf"(##\s*{re.escape(section)}.*?)(?=\n##\s|\Z)"
        match = re.search(section_pattern, content, re.DOTALL | re.IGNORECASE)

        if not match:
            # Section doesn't exist - create it
            if position == "append":
                return content.rstrip() + f"\n\n## {section}\n\n{op.content}\n"
            else:
                raise ApplierError(f"Section '{section}' not found for ADD")

        section_content = match.group(1)
        section_start = match.start()
        section_end = match.end()

        if position == "append":
            new_section = section_content.rstrip() + "\n\n" + op.content
        elif position == "prepend":
            # Insert after the section header
            header_end = section_content.find("\n")
            if header_end == -1:
                new_section = section_content + "\n\n" + op.content
            else:
                new_section = (
                    section_content[: header_end + 1]
                    + "\n"
                    + op.content
                    + "\n"
                    + section_content[header_end + 1 :]
                )
        elif position.startswith("after:"):
            # Insert after specific hash marker
            target_hash = position[6:]
            # Find content with matching hash (first 8 chars of sha256)
            lines = section_content.split("\n")
            insert_idx = None
            for i, line in enumerate(lines):
                line_hash = hashlib.sha256(line.encode()).hexdigest()[:8]
                if line_hash == target_hash:
                    insert_idx = i + 1
                    break
            if insert_idx is None:
                raise ApplierError(f"Target hash '{target_hash}' not found in section")
            lines.insert(insert_idx, op.content)
            new_section = "\n".join(lines)
        else:
            raise ApplierError(f"Unknown position: {position}")

        return content[:section_start] + new_section + content[section_end:]

    def _apply_remove(self, content: str, op: DeltaOperation) -> str:
        """Apply REMOVE operation - remove content from a section"""
        if not op.target:
            raise ApplierError("REMOVE operation requires target")

        section = op.section
        target = op.target

        # Find the section
        section_pattern = rf"(##\s*{re.escape(section)}.*?)(?=\n##\s|\Z)"
        match = re.search(section_pattern, content, re.DOTALL | re.IGNORECASE)

        if not match:
            raise ApplierError(f"Section '{section}' not found for REMOVE")

        section_content = match.group(1)
        section_start = match.start()
        section_end = match.end()

        # Find and remove the target content
        if "pattern" in target:
            # Regex pattern removal
            pattern = target["pattern"]
            new_section = re.sub(pattern, "", section_content)
        elif "hash" in target:
            # Remove line matching hash
            target_hash = target["hash"]
            lines = section_content.split("\n")
            new_lines = []
            for line in lines:
                line_hash = hashlib.sha256(line.encode()).hexdigest()[:8]
                if line_hash != target_hash:
                    new_lines.append(line)
            new_section = "\n".join(new_lines)
        elif "text" in target:
            # Remove exact text match
            new_section = section_content.replace(target["text"], "")
        else:
            raise ApplierError(f"Unknown target type in REMOVE: {target}")

        return content[:section_start] + new_section + content[section_end:]

    def _apply_modify(self, content: str, op: DeltaOperation) -> str:
        """Apply MODIFY operation - replace content in a section"""
        if not op.target or not op.content:
            raise ApplierError("MODIFY operation requires target and content")

        section = op.section
        target = op.target

        # Find the section
        section_pattern = rf"(##\s*{re.escape(section)}.*?)(?=\n##\s|\Z)"
        match = re.search(section_pattern, content, re.DOTALL | re.IGNORECASE)

        if not match:
            raise ApplierError(f"Section '{section}' not found for MODIFY")

        section_content = match.group(1)
        section_start = match.start()
        section_end = match.end()

        # Find and replace the target content
        if "pattern" in target:
            pattern = target["pattern"]
            new_section = re.sub(pattern, op.content, section_content)
        elif "text" in target:
            new_section = section_content.replace(target["text"], op.content)
        else:
            raise ApplierError(f"Unknown target type in MODIFY: {target}")

        return content[:section_start] + new_section + content[section_end:]

    def _generate_diff_preview(
        self, original: str, modified: str, context_lines: int = 3
    ) -> str:
        """Generate a simple diff preview for dry-run mode"""
        import difflib

        original_lines = original.splitlines(keepends=True)
        modified_lines = modified.splitlines(keepends=True)

        diff = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile="original",
            tofile="modified",
            n=context_lines,
        )
        return "".join(diff)

    def _archive_applied_delta(self, delta: Delta):
        """Move applied delta to applied directory"""
        source_path = self.delta_dir / "approved" / f"{delta.delta_id}.json"
        dest_path = self.applied_dir / f"{delta.delta_id}.json"

        # Write updated delta with application metadata
        delta_dict = {
            "delta_id": delta.delta_id,
            "created": delta.created,
            "source": delta.source,
            "source_insights": delta.source_insights,
            "lesson_id": delta.lesson_id,
            "operations": [asdict(op) for op in delta.operations],
            "rationale": delta.rationale,
            "review_status": "applied",
            "applied_at": delta.applied_at,
            "applied_by": delta.applied_by,
        }

        dest_path.write_text(json.dumps(delta_dict, indent=2))

        # Remove from approved
        if source_path.exists():
            source_path.unlink()

    def list_approved_deltas(self) -> list[Delta]:
        """List all approved deltas ready to apply"""
        approved_dir = self.delta_dir / "approved"
        if not approved_dir.exists():
            return []

        deltas = []
        for delta_file in approved_dir.glob("*.json"):
            try:
                delta_dict = json.loads(delta_file.read_text())
                deltas.append(
                    Delta(
                        delta_id=delta_dict["delta_id"],
                        created=delta_dict["created"],
                        source=delta_dict["source"],
                        source_insights=delta_dict["source_insights"],
                        lesson_id=delta_dict["lesson_id"],
                        operations=[
                            DeltaOperation(
                                type=op["type"],
                                section=op["section"],
                                content=op.get("content"),
                                position=op.get("position"),
                                target=op.get("target"),
                            )
                            for op in delta_dict["operations"]
                        ],
                        rationale=delta_dict["rationale"],
                        review_status=delta_dict["review_status"],
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to load delta {delta_file}: {e}")

        return deltas

    def apply_all_approved(self, applied_by: str = "ace_applier") -> dict:
        """Apply all approved deltas"""
        deltas = self.list_approved_deltas()

        results = {
            "total": len(deltas),
            "applied": 0,
            "failed": 0,
            "details": [],
        }

        for delta in deltas:
            result = self.apply_delta(delta, applied_by)
            results["details"].append(result)
            if not result["errors"]:
                results["applied"] += 1
            else:
                results["failed"] += 1

        return results


def main():
    """CLI entry point for the Applier"""
    import argparse

    parser = argparse.ArgumentParser(description="ACE Delta Applier")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Apply single delta
    apply_parser = subparsers.add_parser("apply", help="Apply a single delta")
    apply_parser.add_argument("--delta-id", required=True, help="Delta ID to apply")
    apply_parser.add_argument("--lessons-dir", type=Path, help="Lessons directory")
    apply_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen"
    )

    # Batch apply all approved
    batch_parser = subparsers.add_parser("batch", help="Apply all approved deltas")
    batch_parser.add_argument("--lessons-dir", type=Path, help="Lessons directory")
    batch_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen"
    )

    # List status
    _ = subparsers.add_parser("status", help="Show delta status")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.command == "apply":
        applier = DeltaApplier(
            lessons_dir=args.lessons_dir,
            dry_run=args.dry_run,
        )
        delta = applier.load_delta(args.delta_id)
        result = applier.apply_delta(delta)
        print(json.dumps(result, indent=2))

    elif args.command == "batch":
        applier = DeltaApplier(
            lessons_dir=args.lessons_dir,
            dry_run=args.dry_run,
        )
        results = applier.apply_all_approved()
        print(json.dumps(results, indent=2))

    elif args.command == "status":
        applier = DeltaApplier()
        deltas = applier.list_approved_deltas()
        print(f"Approved deltas ready to apply: {len(deltas)}")
        for delta in deltas:
            print(
                f"  - {delta.delta_id}: {delta.lesson_id} ({len(delta.operations)} ops)"
            )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
