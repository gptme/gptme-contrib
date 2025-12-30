#!/usr/bin/env python3
"""Migrate all old-format entries to new unified-message format."""

import json
import uuid
from pathlib import Path
from datetime import datetime

lock_file = Path("email/locks/email.json")

# Load current data
with open(lock_file) as f:
    data = json.load(f)

messages = data.get("messages", {})

# Build set of platform_message_ids that already have new format entries
new_format_platform_ids = set()
for key, entry in messages.items():
    if "conversation_id" in entry and "platform_message_id" in entry:
        new_format_platform_ids.add(entry["platform_message_id"])

# Migrate old format entries
migrated = []
for key, entry in list(messages.items()):
    # Skip if already new format
    if "conversation_id" in entry:
        continue

    # Skip if already has new format equivalent
    message_id = entry["message_id"]
    if message_id in new_format_platform_ids:
        continue

    # Create new format entry
    universal_id = str(uuid.uuid4())
    new_entry = {
        "message_id": universal_id,
        "conversation_id": "email",
        "platform": "email",
        "platform_message_id": message_id,
        "in_reply_to": entry.get("in_reply_to"),
        "references": [],
        "from_user": None,
        "to_user": None,
        "subject": None,
        "state": entry.get("state", "pending"),
        "created_at": entry.get("created_at", datetime.utcnow().isoformat()),
        "updated_at": entry.get("updated_at", datetime.utcnow().isoformat()),
        "error": entry.get("error"),
    }

    messages[universal_id] = new_entry
    migrated.append((key, message_id, entry.get("state")))
    print(f"Migrating: {message_id[:60]}... → {universal_id}")
    print(f"  State: {entry.get('state')}")

print(f"\nMigrated {len(migrated)} old-format entries to new format")

if migrated:
    # Create backup
    backup_file = lock_file.parent / "email.json.backup2"
    with open(backup_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Created backup: {backup_file}")

    # Write migrated data
    with open(lock_file, "w") as f:
        json.dump(data, f, indent=2)

    print("Migration complete!")
    print("Old entries kept for reference (can be cleaned up later)")
else:
    print("No old entries to migrate")
