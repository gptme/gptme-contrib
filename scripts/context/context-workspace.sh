#!/bin/bash

# Output workspace structure context for gptme agents
# Usage: ./scripts/context/context-workspace.sh [AGENT_DIR]
#
# Generates tree-based overview of key workspace directories.
# If AGENT_DIR is not provided, uses parent of the script's directory.

set -e

export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

AGENT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
pushd "$AGENT_DIR" > /dev/null

echo -e "# Workspace structure\n"

TREE_HARNESS="$(LANG=C tree -a --dirsfirst --noreport . -L 1)"
TREE_TASKS="$(LANG=C tree -a --dirsfirst --noreport ./tasks 2>/dev/null || echo '(no tasks directory)')"
TREE_PROJECTS="$(LANG=C tree -a --dirsfirst --noreport ./projects -L 1 2>/dev/null || echo '(no projects directory)')"
TREE_JOURNAL="$(LANG=C tree -a --dirsfirst --noreport ./journal 2>/dev/null || echo '(no journal directory)')"
TREE_KNOWLEDGE="$(LANG=C tree -a -L 2 --dirsfirst --noreport ./knowledge 2>/dev/null || echo '(no knowledge directory)')"
TREE_PEOPLE="$(LANG=C tree -a --dirsfirst --noreport ./people 2>/dev/null || echo '(no people directory)')"

cat << EOF
\`\`\`tree $AGENT_DIR
$TREE_HARNESS
$TREE_TASKS
$TREE_PROJECTS
$TREE_JOURNAL
$TREE_KNOWLEDGE
$TREE_PEOPLE
\`\`\`
EOF

popd > /dev/null
