#!/usr/bin/env bash
# Tests for scripts/git/guard-mass-delete.sh
# Run: bash tests/test_guard_mass_delete.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GUARD_SCRIPT="$SCRIPT_DIR/scripts/git/guard-mass-delete.sh"
PASS=0
FAIL=0

# Create a temporary git repo for testing
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

setup_repo() {
    rm -rf "$TMPDIR/repo"
    mkdir -p "$TMPDIR/repo"
    cd "$TMPDIR/repo"
    git init -q
    # Disable global hooks (avoids master-commit protection in test repos)
    git config core.hooksPath /dev/null
    # Create 100 tracked files
    for i in $(seq 1 100); do
        echo "content $i" > "file$i.txt"
    done
    git add .
    git commit -q -m "initial: 100 files"
}

assert_pass() {
    local desc="$1"
    shift
    # Run in subshell because guard calls exit 1 (not return 1)
    # shellcheck disable=SC1090  # dynamic source path is intentional in tests
    if (source "$GUARD_SCRIPT" && "$@") 2>/dev/null; then
        PASS=$((PASS + 1))
        echo "  ✓ $desc"
    else
        FAIL=$((FAIL + 1))
        echo "  ✗ $desc (expected pass, got fail)"
    fi
}

assert_fail() {
    local desc="$1"
    shift
    # Run in subshell because guard calls exit 1 (not return 1)
    # shellcheck disable=SC1090  # dynamic source path is intentional in tests
    if (source "$GUARD_SCRIPT" && "$@") 2>/dev/null; then
        FAIL=$((FAIL + 1))
        echo "  ✗ $desc (expected fail, got pass)"
    else
        PASS=$((PASS + 1))
        echo "  ✓ $desc"
    fi
}

echo "=== guard_mass_delete_staged tests ==="

# Test 1: Small deletion passes
setup_repo
git rm -q file1.txt file2.txt file3.txt
echo "Test 1: Small deletion (3 files) passes"
assert_pass "3 files below threshold" guard_mass_delete_staged

# Test 2: Large deletion blocked
setup_repo
for i in $(seq 1 60); do
    git rm -q "file$i.txt"
done
echo "Test 2: Large deletion (60 files) blocked"
assert_fail "60 files above threshold=50" guard_mass_delete_staged

# Test 3: Custom threshold
setup_repo
for i in $(seq 1 10); do
    git rm -q "file$i.txt"
done
echo "Test 3: Custom threshold (MASS_DELETE_THRESHOLD=5)"
MASS_DELETE_THRESHOLD=5 assert_fail "10 files above threshold=5" guard_mass_delete_staged

# Test 4: Bypass with ALLOW_MASS_DELETE
setup_repo
for i in $(seq 1 60); do
    git rm -q "file$i.txt"
done
echo "Test 4: Bypass env var"
ALLOW_MASS_DELETE=1 assert_pass "60 files allowed with ALLOW_MASS_DELETE=1" guard_mass_delete_staged

# Test 5: Exactly at threshold passes
setup_repo
for i in $(seq 1 50); do
    git rm -q "file$i.txt"
done
echo "Test 5: Exactly at threshold (50 files)"
assert_pass "50 files at threshold=50 passes (> not >=)" guard_mass_delete_staged

echo ""
echo "=== guard_mass_delete_commit tests ==="

# Test 6: Commit with small deletion passes
setup_repo
git rm -q file1.txt file2.txt
git commit -q -m "delete 2 files"
COMMIT=$(git rev-parse HEAD)
echo "Test 6: Commit deleting 2 files passes"
assert_pass "commit with 2 deletions" guard_mass_delete_commit "$COMMIT"

# Test 7: Commit with large deletion blocked
setup_repo
for i in $(seq 1 60); do
    git rm -q "file$i.txt"
done
git commit -q -m "delete 60 files"
COMMIT=$(git rev-parse HEAD)
echo "Test 7: Commit deleting 60 files blocked"
assert_fail "commit with 60 deletions" guard_mass_delete_commit "$COMMIT"

echo ""
echo "=== Results ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "FAILED"
    exit 1
else
    echo "ALL PASSED"
    exit 0
fi
