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
# - Show comment authors in --verbose mode (requires extra API call per comment: gh api repos/OWNER/REPO/issues/NUM/comments | jq '.[-1].user.login')
#   * Trade-off: Valuable context vs API rate limits and speed
#   * Could batch fetch or cache to minimize calls

show_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo
    echo "Options:"
    echo "  --with-ids        Show notification IDs for marking as read"
    echo "  --mark-read ID    Mark specific notification as read"
    echo "  --mark-all-read   Mark all notifications as read"
    echo "  -h, --help        Show this help"
    echo
    echo "Examples:"
    echo "  $0                           # Show unread notifications (clean)"
    echo "  $0 --with-ids                # Show notifications with IDs"
    echo "  $0 --mark-read 19518981563   # Mark specific notification as read"
    echo "  $0 --mark-all-read           # Mark all as read"
}

# Parse options
WITH_IDS=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --with-ids)
            WITH_IDS=true
            shift
            ;;
        --mark-read)
            if [[ -n "$2" ]]; then
                echo "📌 Marking notification $2 as read..."
                gh api "/notifications/threads/$2" -X PATCH
                echo "✅ Marked as read"
                exit 0
            else
                echo "Error: --mark-read requires an ID"
                exit 1
            fi
            ;;
        --mark-all-read)
            echo "📌 Marking all notifications as read..."
            gh api notifications -X PUT
            echo "✅ All notifications marked as read (processing in background)"
            exit 0
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

echo "📬 Checking GitHub notifications..."
echo

# Get ALL unread notifications first (excluding CI noise)
ALL_NOTIFS=$(gh api notifications --jq '[.[] | select(.reason != "ci_activity")]')
TOTAL_COUNT=$(echo "$ALL_NOTIFS" | jq '. | length')

if [ "$TOTAL_COUNT" -eq 0 ]; then
    echo "No unread notifications."
    exit 0
fi

echo "Found $TOTAL_COUNT unread notification(s):"
echo

# Categorize by priority (sorted newest first)
MENTIONS=$(echo "$ALL_NOTIFS" | jq '[.[] | select(.reason == "mention")] | sort_by(.updated_at) | reverse')
ASSIGNS=$(echo "$ALL_NOTIFS" | jq '[.[] | select(.reason == "assign")] | sort_by(.updated_at) | reverse')
REVIEWS=$(echo "$ALL_NOTIFS" | jq '[.[] | select(.reason == "review_requested")] | sort_by(.updated_at) | reverse')
AUTHOR=$(echo "$ALL_NOTIFS" | jq '[.[] | select(.reason == "author")] | sort_by(.updated_at) | reverse')
COMMENTS=$(echo "$ALL_NOTIFS" | jq '[.[] | select(.reason == "comment")] | sort_by(.updated_at) | reverse')
OTHERS=$(echo "$ALL_NOTIFS" | jq '[.[] | select(.reason != "mention" and .reason != "assign" and .reason != "review_requested" and .reason != "author" and .reason != "comment")] | sort_by(.updated_at) | reverse')

# Count each category
MENTION_COUNT=$(echo "$MENTIONS" | jq '. | length')
ASSIGN_COUNT=$(echo "$ASSIGNS" | jq '. | length')
REVIEW_COUNT=$(echo "$REVIEWS" | jq '. | length')
AUTHOR_COUNT=$(echo "$AUTHOR" | jq '. | length')
COMMENT_COUNT=$(echo "$COMMENTS" | jq '. | length')
OTHER_COUNT=$(echo "$OTHERS" | jq '. | length')

# Helper function to format compactly with smart timestamps (limit 10 per category)
format_compact() {
    local category=$1
    local limit=10
    local today
    today=$(date +%Y-%m-%d)

    echo "$category" | jq -r ".[] | \"\(.id)|\(.repository.full_name)|\(.subject.title)|\(.subject.type)|\(.updated_at)\"" | head -$limit | while IFS='|' read -r id repo title type timestamp; do
        local date
        local time
        date=$(echo "$timestamp" | cut -d'T' -f1)
        time=$(echo "$timestamp" | cut -d'T' -f2 | cut -d':' -f1,2)

        local time_str
        if [ "$date" = "$today" ]; then
            time_str="$time"
        else
            time_str="$date"
        fi

        # Add type indicator
        local type_indicator=""
        case "$type" in
            PullRequest) type_indicator="[PR]" ;;
            Issue) type_indicator="[Issue]" ;;
            *) type_indicator="[$type]" ;;
        esac

        if [ "$WITH_IDS" = true ]; then
            echo "- $type_indicator $repo: $title ($time_str) [ID: $id]"
        else
            echo "- $type_indicator $repo: $title ($time_str)"
        fi
    done
}

# Show high-priority categories first
if [ "$MENTION_COUNT" -gt 0 ]; then
    echo "**Mentions** ($MENTION_COUNT):"
    format_compact "$MENTIONS"
    [ "$MENTION_COUNT" -gt 10 ] && echo "  ... and $((MENTION_COUNT - 10)) more"
    echo
fi

if [ "$ASSIGN_COUNT" -gt 0 ]; then
    echo "**Assigned** ($ASSIGN_COUNT):"
    format_compact "$ASSIGNS"
    [ "$ASSIGN_COUNT" -gt 10 ] && echo "  ... and $((ASSIGN_COUNT - 10)) more"
    echo
fi

if [ "$REVIEW_COUNT" -gt 0 ]; then
    echo "**Review Requests** ($REVIEW_COUNT):"
    format_compact "$REVIEWS"
    [ "$REVIEW_COUNT" -gt 10 ] && echo "  ... and $((REVIEW_COUNT - 10)) more"
    echo
fi

if [ "$AUTHOR_COUNT" -gt 0 ]; then
    echo "**Your Items** ($AUTHOR_COUNT):"
    format_compact "$AUTHOR"
    [ "$AUTHOR_COUNT" -gt 10 ] && echo "  ... and $((AUTHOR_COUNT - 10)) more"
    echo
fi

if [ "$COMMENT_COUNT" -gt 0 ]; then
    echo "**Comments** ($COMMENT_COUNT):"
    format_compact "$COMMENTS"
    [ "$COMMENT_COUNT" -gt 10 ] && echo "  ... and $((COMMENT_COUNT - 10)) more"
    echo
fi

if [ "$OTHER_COUNT" -gt 0 ]; then
    echo "**Other** ($OTHER_COUNT):"
    format_compact "$OTHERS"
    [ "$OTHER_COUNT" -gt 10 ] && echo "  ... and $((OTHER_COUNT - 10)) more"
    echo
fi

echo "💡 Use ./scripts/github/check-notifications.sh --help for management options"
