"""Regression: two distinct defects in one function must not become one finding.

gptme-contrib#2437: the review pass on ``81e6cab3b55c`` reported a new P1 (an
explicit ``BEGIN IMMEDIATE`` inside an already-open transaction) that inherited
the fingerprint and inline thread of an earlier, different P1 about a
stale-snapshot read in the same function. The two bodies share much of their
content words because they discuss the same code, which is enough for
``_same_defect``'s overlap coefficient to call them the same defect.

The bodies below are the real ones from that PR (marker stripped), so the test
fails again if the heuristic goes back to folding them together.
"""

import pytest
from gptme_review import core

FILE = "packages/gptme-wisdom-mcp/src/gptme_wisdom_mcp/indexer.py"

# Inline thread from the earlier pass: the stale-snapshot read.
STALE_SNAPSHOT = {
    "file": FILE,
    "line": 115,
    "body": '<!-- bob-ai-review-fp {"fp": "cbe32c698903", "file": "packages/gptme-wisdom-mcp/src/gptme_wisdom_mcp/indexer.py", "line": 115, "tokens": ["autocommit", "concurrency", "contentless", "index_all_sessions", "integrityerror", "isolation_level", "overlapping", "permanently", "plausible", "purge_paths_matching", "recommends", "transaction"], "class": "bug", "provenance": [{"model": "claude-code:claude-opus-5-5", "pass": 1, "aspect": "general"}]} -->\n❌ **P1** — The stale-snapshot read lets two overlapping indexer runs corrupt the contentless FTS index. `_upsert()` reads the stored `title`/`content`/`summary` with a bare SELECT under `with self.conn:`. Python\'s `sqlite3` with the default `isolation_level` opens an implicit transaction only before INSERT/UPDATE/DELETE, and `with conn:` does not begin one. So the SELECT runs in autocommit, and the write transaction starts later at the FTS `\'delete\'` INSERT. Suppose two `index_all_sessions` runs overlap, which is plausible with the cron usage the README recommends and the 50k-log gptme pass. Both read old values O for a grown session, and A deletes O, updates to X and inserts X. B then issues `\'delete\'` with O, which is no longer indexed, and inserts X a second time. For a contentless FTS5 table a delete with non-indexed values corrupts posting lists and doc counts, and the rowid now carries X twice. Every later `\'delete\'` built from the `sessions` row no longer matches, so BM25 ranks drift and stale terms keep matching permanently until `purge_paths_matching` rebuilds. The PR\'s concurrency note only covers the new-path IntegrityError race.',
}

# New finding from the next pass: `BEGIN IMMEDIATE` inside a transaction.
BEGIN_IMMEDIATE = {
    "file": FILE,
    "line": 117,
    "body": "`_upsert()` now issues an explicit `BEGIN IMMEDIATE` inside `with self.conn:`.\n\n**Old behaviour:** `add()` ran a plain `INSERT` followed by `self.conn.commit()`. That joined any transaction already open on the shared `idx.conn` and committed it.\n\n**New behaviour:** if a transaction is already open, `BEGIN IMMEDIATE` raises `sqlite3.OperationalError: cannot start a transaction within a transaction`. `add()` does not catch this, since it only catches `IntegrityError`. The `with self.conn` exit then calls `rollback()`, which discards the caller's pending work.\n\n**Concrete trigger:** with the default legacy isolation level, any DML on `idx.conn` opens an implicit transaction. The new tests do exactly this with `idx.conn.execute(\"INSERT INTO sessions_fts ... 'integrity-check' ...\")`. Running `idx.add(doc)` after such a statement (or with a connection set to `autocommit=False` on 3.12+) now crashes.\n\n**Impact:** `_parse_and_add` does not guard `index.add`, so the whole `index_all_sessions` run aborts. It previously succeeded.",
}


@pytest.mark.xfail(
    strict=True,
    reason="gptme-contrib#2437: the overlap coefficient folds distinct defects "
    "that discuss the same function; needs a distinctive-token measure",
)
def test_distinct_defects_in_one_function_do_not_share_a_finding():
    """A stored entry for the stale-snapshot read must not capture the new P1."""
    entry = core.suppression_entry(STALE_SNAPSHOT)
    assert not core.matches_entry(BEGIN_IMMEDIATE, entry)
    assert core.finding_fingerprint(BEGIN_IMMEDIATE) != entry["fp"]


def test_the_overlap_is_generic_code_vocabulary():
    """Documents *why* the collapse happens, so a fix keeps this in view.

    The shared words are the vocabulary of the function under review (``conn``,
    ``index``, ``transaction``), not the subject of either claim.
    """
    shared = core._claim_tokens(STALE_SNAPSHOT["body"]) & core._claim_tokens(
        BEGIN_IMMEDIATE["body"]
    )
    assert len(shared) >= core.DEDUP_MIN_SHARED
    assert (
        len(shared)
        / min(
            len(core._claim_tokens(STALE_SNAPSHOT["body"])),
            len(core._claim_tokens(BEGIN_IMMEDIATE["body"])),
        )
        >= core.DEDUP_SIMILARITY
    )
