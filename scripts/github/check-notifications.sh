#!/bin/bash
# Check GitHub notifications for Bob
#
# TODO: Future enhancements
# - Fetch and display PR/issue details (status, author, labels, etc.)
# - Group notifications by repository for better organization
# - Show who mentioned/assigned you
# - Highlight urgent items (review requests, direct mentions)
# - Add filtering options (by repo, by reason, by type)
# - Show thread context/preview of comment text
# - Color coding by notification type/priority
# - Show notification age/staleness

show_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo
    echo "Options:"
    echo "  --mark-read ID    Mark specific notification as read"
    echo "  --mark-all-read   Mark all notifications as read"
    echo "  -h, --help        Show this help"
    echo
    echo "Examples:"
    echo "  $0                           # Show unread notifications (with IDs)"
    echo "  $0 --mark-read 19518981563   # Mark specific notification as read"
    echo "  $0 --mark-all-read           # Mark all as read"
}

if [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
    show_usage
    exit 0
fi

if [[ "$1" == "--mark-read" ]] && [[ -n "$2" ]]; then
    echo "📌 Marking notification $2 as read..."
    gh api "/notifications/threads/$2" -X PATCH
    echo "✅ Marked as read"
    exit 0
fi

if [[ "$1" == "--mark-all-read" ]]; then
    echo "📌 Marking all notifications as read..."
    gh api notifications -X PUT
    echo "✅ All notifications marked as read (processing in background)"
    exit 0
fi

echo "📬 Checking GitHub notifications..."
echo

# Get unread notifications, filtering out CI noise
# Show IDs by default for easy marking as read
gh api notifications \
  --jq '.[] | select(.reason != "ci_activity") | {
    id: .id,
    reason: .reason,
    type: .subject.type,
    title: .subject.title,
    repo: .repository.full_name,
    updated: .updated_at
  }' | jq -s 'sort_by(.updated) | reverse | .[] |
    "ID: \(.id)\n[\(.reason)] \(.type): \(.title)\n  Repo: \(.repo)\n  Updated: \(.updated)\n"' -r | head -50

echo
echo "💡 To mark specific notification as read: $0 --mark-read <ID>"
echo "💡 To mark all as read: $0 --mark-all-read"
