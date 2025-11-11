#!/usr/bin/env python3
"""Migrate email.json from OLD to NEW unified format.

OLD format:
  Key: platform message ID directly
  Value: {message_id, state, created_at, updated_at, in_reply_to}

NEW format:
  Key: Universal UUID
  Value: {message_id, conversation_id, platform, platform_message_id, state, ...}
"""

import json
import uuid
from pathlib import Path
from datetime import datetime


def migrate_email_json(email_json_path: Path, dry_run: bool = True):
    """Migrate email.json from OLD to NEW format."""
    # Load existing data
    with open(email_json_path) as f:
        data = json.load(f)

    messages = data.get("messages", {})
    old_count = 0
    new_count = 0
    migrated = {}

    # Identify OLD and NEW format entries
    for msg_id, msg_data in messages.items():
        if "platform" in msg_data and "platform_message_id" in msg_data:
            # NEW format - keep as is
            new_count += 1
            migrated[msg_id] = msg_data
        else:
            # OLD format - migrate
            old_count += 1

            # Generate new UUID
            new_uuid = str(uuid.uuid4())

            # Create NEW format entry
            migrated[new_uuid] = {
                "message_id": new_uuid,
                "conversation_id": "email",
                "platform": "email",
                "platform_message_id": msg_id,  # OLD key becomes platform_message_id
                "state": msg_data.get("state", "pending"),
                "created_at": msg_data.get("created_at", datetime.now().isoformat()),
                "updated_at": msg_data.get("updated_at", datetime.now().isoformat()),
                "in_reply_to": msg_data.get("in_reply_to"),
                "from_user": None,
                "to_user": None,
                "subject": None,
                "references": [],
            }

    print("📊 Migration Summary:")
    print(f"  OLD format entries: {old_count}")
    print(f"  NEW format entries: {new_count}")
    print(f"  Total: {len(messages)}")
    print(f"  Migrated total: {len(migrated)}")

    if dry_run:
        print("\n🔍 DRY RUN - No changes made")
        print("Run with --apply to actually migrate")
        return

    # Backup original file
    backup_path = email_json_path.with_suffix(".json.pre-migration-backup")
    with open(backup_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n💾 Backup saved: {backup_path}")

    # Write migrated data
    data["messages"] = migrated
    with open(email_json_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"✅ Migration complete: {email_json_path}")


if __name__ == "__main__":
    import sys

    email_json = Path("/home/bob/bob/email/locks/email.json")

    dry_run = "--apply" not in sys.argv
    migrate_email_json(email_json, dry_run=dry_run)
