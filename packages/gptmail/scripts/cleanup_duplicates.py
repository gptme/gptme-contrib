#!/usr/bin/env python3
"""Script to identify and clean up duplicate email files.

This script finds duplicate sent emails that were created before the
duplicate detection fix was implemented. It identifies pairs of files
where one has the agent's UUID Message-ID and another has Gmail's Message-ID,
but they represent the same email.
"""

import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple


def parse_headers(content: str) -> Dict[str, str]:
    """Parse email headers from markdown content."""
    headers = {}
    for line in content.split("\n"):
        if not line or line.isspace():
            break
        if ": " in line:
            key, value = line.split(": ", 1)
            headers[key] = value
    return headers


def normalize_addr(addr: str) -> str:
    """Normalize email address by removing quotes and whitespace."""
    return addr.strip().strip('"').strip()


def parse_date(date_str: str) -> datetime:
    """Parse email date string."""
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(date_str)
    except Exception:
        return datetime.min


def find_duplicates(sent_dir: Path) -> List[Tuple[Path, Path, str]]:
    """Find duplicate email files in sent directory.

    Returns:
        List of (file1, file2, reason) tuples for duplicates
    """
    duplicates = []

    # Group files by potential duplicate indicators
    by_in_reply_to: Dict[str, List[Path]] = defaultdict(list)
    by_subject_to_date: Dict[str, List[Path]] = defaultdict(list)

    for email_file in sent_dir.glob("*.md"):
        try:
            content = email_file.read_text()
            headers = parse_headers(content)

            # Group by In-Reply-To
            in_reply_to = headers.get("In-Reply-To", "").strip().strip("<>")
            if in_reply_to:
                by_in_reply_to[in_reply_to].append(email_file)

            # Group by Subject + To + Date
            subject = headers.get("Subject", "").strip()
            to_addr = normalize_addr(headers.get("To", ""))
            date = headers.get("Date", "").strip()
            if subject and to_addr and date:
                key = f"{subject}|{to_addr}|{date}"
                by_subject_to_date[key].append(email_file)

        except Exception as e:
            print(f"Error processing {email_file}: {e}", file=sys.stderr)
            continue

    # Find duplicates by In-Reply-To
    for in_reply_to, files in by_in_reply_to.items():
        if len(files) > 1:
            # Sort by modification time (keep older one)
            files.sort(key=lambda f: f.stat().st_mtime)
            for i in range(1, len(files)):
                reason = f"Same In-Reply-To: <{in_reply_to}>"
                duplicates.append((files[0], files[i], reason))

    # Find duplicates by Subject + To + Date (excluding already found)
    found_files = {dup[1] for dup in duplicates}
    for key, files in by_subject_to_date.items():
        if len(files) > 1:
            files.sort(key=lambda f: f.stat().st_mtime)
            for i in range(1, len(files)):
                if files[i] not in found_files:
                    reason = "Same Subject+To+Date"
                    duplicates.append((files[0], files[i], reason))

    return duplicates


def main():
    """Main function to identify and optionally clean up duplicates."""
    import argparse

    parser = argparse.ArgumentParser(description="Find and clean up duplicate email files")
    parser.add_argument("--dry-run", action="store_true", help="Only show duplicates, don't delete")
    parser.add_argument(
        "--sent-dir",
        type=Path,
        default=Path.home() / "workspace" / "email" / "sent",
        help="Path to sent email directory",
    )

    args = parser.parse_args()

    if not args.sent_dir.exists():
        print(f"Error: Directory not found: {args.sent_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning for duplicates in: {args.sent_dir}")
    duplicates = find_duplicates(args.sent_dir)

    if not duplicates:
        print("✅ No duplicates found!")
        return

    print(f"\n🔍 Found {len(duplicates)} duplicate(s):\n")

    for original, duplicate, reason in duplicates:
        print(f"Keep:   {original.name}")
        print(f"Remove: {duplicate.name}")
        print(f"Reason: {reason}")
        print()

        if not args.dry_run:
            try:
                duplicate.unlink()
                print(f"✅ Deleted: {duplicate.name}\n")
            except Exception as e:
                print(f"❌ Error deleting {duplicate.name}: {e}\n", file=sys.stderr)

    if args.dry_run:
        print("\n⚠️  DRY RUN: No files were deleted.")
        print("   Run without --dry-run to actually delete duplicates.")
    else:
        print(f"\n✅ Cleanup complete: Removed {len(duplicates)} duplicate(s)")


if __name__ == "__main__":
    main()
