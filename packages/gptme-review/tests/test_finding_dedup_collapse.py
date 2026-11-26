"""Regression: two distinct defects in one function must not become one finding.

gptme-contrib#2437: the review pass on ``81e6cab3b55c`` reported a new P1 (an
explicit ``BEGIN IMMEDIATE`` inside an already-open transaction) that inherited
the fingerprint and inline thread of an earlier, different P1 about a
stale-snapshot read in the same function. The two bodies share much of their
content words because they discuss the same code, which is enough for
``_same_defect``'s overlap coefficient to call them the same defect.

The bodies below are the real ones from that PR (marker stripped). Marker
recognition must not use a few shared API names to hide the second finding.
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from gptme_review import core, writer

FILE = "packages/gptme-wisdom-mcp/src/gptme_wisdom_mcp/indexer.py"

# Inline thread from the earlier pass: the stale-snapshot read.
STALE_SNAPSHOT: dict[str, Any] = {
    "file": FILE,
    "line": 115,
    "body": "❌ **P1** — The stale-snapshot read lets two overlapping indexer runs corrupt the contentless FTS index. `_upsert()` reads the stored `title`/`content`/`summary` with a bare SELECT under `with self.conn:`. Python's `sqlite3` with the default `isolation_level` opens an implicit transaction only before INSERT/UPDATE/DELETE, and `with conn:` does not begin one. So the SELECT runs in autocommit, and the write transaction starts later at the FTS `'delete'` INSERT. Suppose two `index_all_sessions` runs overlap, which is plausible with the cron usage the README recommends and the 50k-log gptme pass. Both read old values O for a grown session, and A deletes O, updates to X and inserts X. B then issues `'delete'` with O, which is no longer indexed, and inserts X a second time. For a contentless FTS5 table a delete with non-indexed values corrupts posting lists and doc counts, and the rowid now carries X twice. Every later `'delete'` built from the `sessions` row no longer matches, so BM25 ranks drift and stale terms keep matching permanently until `purge_paths_matching` rebuilds. The PR's concurrency note only covers the new-path IntegrityError race.",
}

# New finding from the next pass: `BEGIN IMMEDIATE` inside a transaction.
BEGIN_IMMEDIATE: dict[str, Any] = {
    "file": FILE,
    "line": 117,
    "body": "`_upsert()` now issues an explicit `BEGIN IMMEDIATE` inside `with self.conn:`.\n\n**Old behaviour:** `add()` ran a plain `INSERT` followed by `self.conn.commit()`. That joined any transaction already open on the shared `idx.conn` and committed it.\n\n**New behaviour:** if a transaction is already open, `BEGIN IMMEDIATE` raises `sqlite3.OperationalError: cannot start a transaction within a transaction`. `add()` does not catch this, since it only catches `IntegrityError`. The `with self.conn` exit then calls `rollback()`, which discards the caller's pending work.\n\n**Concrete trigger:** with the default legacy isolation level, any DML on `idx.conn` opens an implicit transaction. The new tests do exactly this with `idx.conn.execute(\"INSERT INTO sessions_fts ... 'integrity-check' ...\")`. Running `idx.add(doc)` after such a statement (or with a connection set to `autocommit=False` on 3.12+) now crashes.\n\n**Impact:** `_parse_and_add` does not guard `index.add`, so the whole `index_all_sessions` run aborts. It previously succeeded.",
}


def test_distinct_defects_in_one_function_do_not_share_a_finding():
    """A stored entry for the stale-snapshot read must not capture the new P1."""
    entry = core.suppression_entry(STALE_SNAPSHOT)
    assert not core.matches_entry(BEGIN_IMMEDIATE, entry)
    assert core.finding_fingerprint(BEGIN_IMMEDIATE) != entry["fp"]


@pytest.mark.parametrize(
    "original,fresh",
    [(STALE_SNAPSHOT, BEGIN_IMMEDIATE), (BEGIN_IMMEDIATE, STALE_SNAPSHOT)],
)
def test_distinct_finding_gets_its_own_thread(original, fresh):
    w = writer.bind_io(SimpleNamespace(MARKER_NAMESPACE="test-review"))
    entry = core.suppression_entry(original)
    thread = {
        "id": "old-defect",
        "isResolved": False,
        "body": (
            f"{w.inline_finding_marker()}\n"
            f"<!-- test-review-fp {json.dumps(entry)} -->\n"
            f"❌ **P1** — {original['body']}"
        ),
    }
    kept, stale = w.reconcile_inline_threads([fresh], [thread])
    assert kept == {}  # The publication path posts findings absent from kept.
    assert stale == ["old-defect"]
    state = w.build_marker_state(
        head_sha="new-head", score=3, engine="test", findings=[fresh], kept=kept
    )
    assert state["findings"][0]["fp"] == core.finding_fingerprint(fresh)


_1387_CORRECTNESS = {
    "body": "The 'Zero checks (webhook dropped)?' section treats an empty `gh pr checks` as proof of a dropped webhook and directs the agent to close+reopen the PR, but empty output also occurs for PRs that legitimately have no applicable checks or whose head-commit runs have not been created yet. The prompt should first make the agent verify no runs actually exist via `gh run list`.",
    "file": "packages/gptme_runloops/prompt_templates.py",
    "line": 557,
    "severity": "P2",
    "aspect": "correctness",
}


_1387_SECURITY = {
    "body": "The `_CI_FAILURE_ARM` remediation block asserts that when `gh pr checks` returns no results no workflow run exists, and regenerates the pull_request event by closing+reopening. That inference is false: a workflow still queued or throttled by a concurrency limit yields an empty check table while a real run is about to appear. There is no `gh run list` confirmation before the destructive close+reopen.",
    "file": "packages/gptme_runloops/prompt_templates.py",
    "line": 557,
    "severity": "P2",
    "security": True,
    "verification": "Checked the arm at lines 557-563.",
    "aspect": "security",
}


_1387_NEARBY = {
    "body": "The new line in `_PR_UPDATE_ARM` unconditionally instructs the agent to run retrigger-pr-checks.sh, which closes and reopens the PR, whenever `gh pr checks` prints nothing. Empty output is not specific to a dropped webhook. There is no step to confirm a run truly is absent via `gh run list`.",
    "file": "packages/gptme_runloops/prompt_templates.py",
    "line": 523,
    "severity": "P2",
    "security": True,
    "aspect": "security",
}


@pytest.mark.parametrize(
    "left,right",
    [
        (_1387_CORRECTNESS, _1387_SECURITY),
        (_1387_CORRECTNESS, _1387_NEARBY),
        (_1387_SECURITY, _1387_NEARBY),
    ],
)
def test_full_findings_keep_the_original_fanout_paraphrases(left, right):
    assert core.same_defect(left, right)
    assert core.same_defect(right, left)


def test_marker_keeps_a_reworded_response_leak_on_its_thread():
    original = {
        "file": "retry.py",
        "line": 10,
        "body": "the retry loop never closes the response handle so sockets leak",
    }
    fresh = {
        **original,
        "body": "sockets leak because the response handle is never closed in retry",
    }
    entry = core.suppression_entry(original)
    assert entry["fp"] != core.finding_fingerprint(fresh)
    assert core.matches_entry(fresh, entry)
    assert core.matches_entry(original, core.suppression_entry(fresh))


@pytest.mark.parametrize("file,line", [("other.py", 10), ("retry.py", 200)])
def test_fuzzy_marker_match_still_checks_location(file, line):
    original = {
        "file": "retry.py",
        "line": 10,
        "body": "the retry loop never closes the response handle so sockets leak",
    }
    fresh = {
        "file": file,
        "line": line,
        "body": "sockets leak because the response handle is never closed in retry",
    }
    assert not core.matches_entry(fresh, core.suppression_entry(original))


def test_exact_marker_identity_survives_a_line_move():
    assert core.matches_entry(
        {**STALE_SNAPSHOT, "line": 900}, core.suppression_entry(STALE_SNAPSHOT)
    )


def test_weak_marker_evidence_favors_a_new_thread():
    # Previously asymmetric: correctness -> nearby matched, the reverse did not.
    # The compact summaries share only 2/12 identifying words. Full-body fanout
    # above still merges them; a legacy marker cannot supply that missing body.
    assert not core.matches_entry(
        _1387_NEARBY, core.suppression_entry(_1387_CORRECTNESS)
    )
    assert not core.matches_entry(
        _1387_CORRECTNESS, core.suppression_entry(_1387_NEARBY)
    )
