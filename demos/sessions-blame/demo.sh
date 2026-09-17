#!/usr/bin/env bash
# Self-contained demo of `gptme-sessions blame`.
#
# Creates a throwaway git repo, makes one commit whose author-date falls
# inside the sample session window (see session-records.jsonl), then runs
# `gptme-sessions blame` against that sample store to show the attribution.
#
# Usage:
#   ./demo.sh            # uses the bundled session-records.jsonl
#   ./demo.sh --json     # also print the JSON form
#
# Requires: git, gptme-sessions (pip install gptme-sessions)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORDS="${HERE}/session-records.jsonl"
AS_JSON="${1:-}"

# The sample session window: [10:00, 10:30] UTC on 2026-06-01.
# The commit below is authored at 10:15 UTC, inside that window.
COMMIT_DATE="2026-06-01T10:15:00+00:00"

# --- build a throwaway repo with one commit inside the window ---
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
# Explicit -b main (a global init.defaultBranch would otherwise race with the
# branch creation) and no inherited commit signing.
git init -q -b main
git config user.email "demo@example.com"
git config user.name "Demo Agent"
printf 'def hello():\n    return "world"\n' > hello.py
git add hello.py
# Pin the author/committer date so the commit lands inside the sample window.
GIT_AUTHOR_DATE="$COMMIT_DATE" GIT_COMMITTER_DATE="$COMMIT_DATE" \
  git -c core.hooksPath=/dev/null -c commit.gpgsign=false commit -q -m "feat: add hello function"

echo "== commit =="
git log -1 --format="%h  %aI  %s"
echo
echo "== gptme-sessions blame hello.py --records session-records.jsonl =="
gptme-sessions blame hello.py --records "$RECORDS"

if [[ "$AS_JSON" == "--json" ]]; then
  echo
  echo "== JSON form =="
  gptme-sessions blame hello.py --records "$RECORDS" --json
fi
