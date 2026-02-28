#!/bin/bash

# Output journal context for gptme agents
# Usage: ./scripts/context/context-journal.sh [AGENT_DIR]
#
# Supports both journal formats:
#   - Legacy flat: journal/2025-12-24-topic.md
#   - Subdirectory:  journal/2025-12-24/topic.md
#
# If AGENT_DIR is not provided, uses parent of the script's directory.

set -e

if [ -n "${1:-}" ]; then
    AGENT_DIR="$1"
else
    AGENT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || (cd "$(dirname "$0")/.." && pwd))"
fi
pushd "$AGENT_DIR" > /dev/null

if [ ! -d journal ]; then
    echo "Journal folder not found, skipping journal section."
    popd > /dev/null
    exit 0
fi

echo "# Journal Context"
echo

# Find all journal files (both formats)
ALL_JOURNALS=$(
    # Legacy flat format
    find journal -maxdepth 1 -name "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*.md" -type f 2>/dev/null
    # New subdirectory format
    find journal -mindepth 2 -maxdepth 2 -name "*.md" -type f 2>/dev/null | while read -r f; do
        parent=$(basename "$(dirname "$f")")
        if echo "$parent" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
            echo "$f"
        fi
    done
)

LATEST_JOURNAL=$(echo "$ALL_JOURNALS" | sort -r | head -n 1)

if [ -z "$LATEST_JOURNAL" ]; then
    echo "No journal entries found."
    popd > /dev/null
    exit 0
fi

# Extract date from path (supports both formats)
extract_date_from_path() {
    local path="$1"
    local parent
    parent=$(basename "$(dirname "$path")")
    if echo "$parent" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
        echo "$parent"
    else
        basename "$path" .md | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
    fi
}

DATE=$(extract_date_from_path "$LATEST_JOURNAL")

# Get all journal files for this date, sorted by mtime (most recent first)
JOURNALS_BY_MTIME=$(
    {
        find journal -maxdepth 1 -name "${DATE}*.md" -type f 2>/dev/null
        if [ -d "journal/${DATE}" ]; then
            find "journal/${DATE}" -maxdepth 1 -name "*.md" -type f 2>/dev/null
        fi
    } | while read -r f; do python3 -c "import os,sys; print(int(os.stat(sys.argv[1]).st_mtime), sys.argv[1])" "$f" 2>/dev/null; done | sort -rn | cut -d' ' -f2
)

# Header
YESTERDAY=$(python3 -c "from datetime import date, timedelta; print((date.today() - timedelta(days=1)).isoformat())")
if [ "$(date +%Y-%m-%d)" = "$DATE" ]; then
    HEADER="Today's Journal Entry"
elif [ "$YESTERDAY" = "$DATE" ]; then
    HEADER="Yesterday's Journal Entry"
else
    HEADER="Journal Entry from $DATE"
fi

if [ -z "$JOURNALS_BY_MTIME" ]; then
    JOURNAL_COUNT=0
else
    JOURNAL_COUNT=$(echo "$JOURNALS_BY_MTIME" | wc -l)
fi

if [ "$JOURNAL_COUNT" -eq 1 ]; then
    echo "$HEADER:"
else
    echo "$HEADER ($JOURNAL_COUNT sessions):"
fi
echo

if [ "$(date +%Y-%m-%d)" != "$DATE" ]; then
    echo "**IMPORTANT**: This journal is from $DATE (not today: $(date +%Y-%m-%d))."
    echo "Create a NEW journal entry for today at: \`journal/$(date +%Y-%m-%d)/<description>.md\`"
    echo
fi

# Configuration
MAX_FULL_ENTRIES=10

# Get most recent N entries by mtime, then re-sort chronologically
RECENT_JOURNALS=$(echo "$JOURNALS_BY_MTIME" | head -n $MAX_FULL_ENTRIES | while read -r f; do python3 -c "import os,sys; print(int(os.stat(sys.argv[1]).st_mtime), sys.argv[1])" "$f"; done | sort -n | cut -d' ' -f2)
OLDER_JOURNALS=$(echo "$JOURNALS_BY_MTIME" | tail -n +$((MAX_FULL_ENTRIES + 1)))

for JOURNAL in $RECENT_JOURNALS; do
    BASENAME=$(basename "$JOURNAL" .md)
    PARENT_DIR=$(basename "$(dirname "$JOURNAL")")
    DESCRIPTION=""

    if echo "$PARENT_DIR" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
        DESCRIPTION="$BASENAME"
    elif [[ "$BASENAME" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-(.+)$ ]]; then
        DESCRIPTION="${BASH_REMATCH[1]}"
    fi

    if [ -n "$DESCRIPTION" ] && [ "$JOURNAL_COUNT" -gt 1 ]; then
        echo "## Session: $DESCRIPTION"
        echo
    fi

    echo "\`\`\`$JOURNAL"
    cat "$JOURNAL"
    echo "\`\`\`"
    echo
done

if [ -n "$OLDER_JOURNALS" ]; then
    echo "## Older Sessions (read with cat if relevant)"
    echo
    for JOURNAL in $OLDER_JOURNALS; do
        echo "- \`$JOURNAL\`"
    done
    echo
fi

popd > /dev/null
