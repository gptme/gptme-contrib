#!/usr/bin/env bash

# Create a journal entry under its work day without imposing a body format.
# The entry body is read from stdin and written verbatim.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: journal-new.sh <name>

Create journal/YYYY-MM-DD/<name>.md from stdin without overwriting a file.

Environment:
  JOURNAL_STARTED  ISO-8601 timestamp for when the work began. Its date is used.
  JOURNAL_DATE     Explicit work date (YYYY-MM-DD). Must agree with STARTED.
  JOURNAL_ROOT     Journal directory (default: <git root>/journal).
EOF
}

if [[ $# -ne 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    [[ $# -eq 1 ]] && exit 0
    exit 2
fi

name=${1%.md}
if [[ -z "$name" || "$name" == "." || "$name" == ".." || "$name" == */* ]]; then
    echo "journal-new: name must be a single non-empty filename" >&2
    exit 2
fi

started=${JOURNAL_STARTED:-}
started_date=${started:0:10}
work_date=${JOURNAL_DATE:-$started_date}
if [[ -z "$work_date" ]]; then
    work_date=$(date +%F)
fi

if ! python3 - "$work_date" <<'PY'
import datetime
import sys

try:
    parsed = datetime.date.fromisoformat(sys.argv[1])
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if parsed.isoformat() == sys.argv[1] else 1)
PY
then
    echo "journal-new: invalid JOURNAL_DATE: $work_date" >&2
    exit 2
fi

if [[ -n "$started" ]]; then
    if ! python3 - "$started" <<'PY'
import datetime
import sys

value = sys.argv[1]
try:
    datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(1)
PY
    then
        echo "journal-new: invalid JOURNAL_STARTED: $started" >&2
        exit 2
    fi
    if [[ -n "${JOURNAL_DATE:-}" && "$started_date" != "$work_date" ]]; then
        echo "journal-new: JOURNAL_DATE ($work_date) disagrees with JOURNAL_STARTED ($started_date)" >&2
        exit 2
    fi
fi

if [[ -n "${JOURNAL_ROOT:-}" ]]; then
    journal_root=$JOURNAL_ROOT
else
    repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
        echo "journal-new: not in a Git repository; set JOURNAL_ROOT" >&2
        exit 2
    }
    journal_root=$repo_root/journal
fi

target=$journal_root/$work_date/$name.md
mkdir -p "$(dirname "$target")"

if ! (set -o noclobber; cat > "$target") 2>/dev/null; then
    echo "journal-new: refusing to overwrite existing entry: $target" >&2
    exit 1
fi

printf '%s\n' "$target"
