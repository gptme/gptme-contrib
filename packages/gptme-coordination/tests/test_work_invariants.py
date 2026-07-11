"""Property/stress tests for claim-lifecycle invariants under real concurrency.

Multi-process contenders hammer one WAL SQLite database with seeded-random
interleavings, then a checker asserts the lifecycle invariants that recent
incidents violated one at a time:

- I-MUTEX: a contested key is won by exactly one claimer (never two live
  holders of one canonical key).
- I-EPOCH-UNIQ: every successful (non-renewal) claim consumes a unique
  ``(key, epoch)`` pair, and per key the epochs form the gapless sequence
  ``1..n`` — any duplicate or gap is a lost CAS update.

Prior incidents in this class, each caught reactively via a single
deterministic regression test rather than a stress suite that exercises real
multi-process interleavings:

- An expired claim could still be ``complete()``-d, letting a session that
  outlived its TTL ghost-complete a task after another agent had already
  reclaimed it (fixed in #1198; see ``TestComplete.test_complete_after_expiry_is_denied``
  in ``test_work.py``).
- The claim HMAC was computed over a Python-clock expiry string while the
  stored ``expires_at`` came from a separate SQLite-evaluated
  ``datetime('now', ...)`` call; when the two reads straddled a second
  boundary the stored HMAC could never verify (fixed in #1265; see
  ``TestExpiryHmacCoherence`` in ``test_work.py``).

This module covers the *invariant class* under real concurrency by driving
many seeded-random multi-process interleavings per run (failures reproduce
via the fixed seed) rather than one hand-picked interleaving at a time. A
sibling suite in the Bob workspace repo covers the reap/liveness layer
(``reap_dead_holders``), which is Bob-local and not part of this package.
"""

from __future__ import annotations

import multiprocessing
import random
import sqlite3
import time
from pathlib import Path

from gptme_coordination.db import CoordinationDB
from gptme_coordination.work import WorkClaimManager

# CI-friendly sizes: the suite completes in a few seconds. Bump locally when
# hunting a suspected race (the invariant checks don't change with scale).
N_WORKERS = 6
N_KEYS = 12
N_CHURN_ITERATIONS = 40
SEED = 0x53F6


def _open_manager(db_path: str) -> tuple[CoordinationDB, WorkClaimManager]:
    db = CoordinationDB(Path(db_path))
    return db, WorkClaimManager(db, default_ttl_minutes=60)


def _precreate_db(db_path: str) -> None:
    """Create the database, WAL mode, and work schema before forking workers.

    The WAL journal-mode switch needs exclusive access, so N processes racing
    to first-open a fresh file fail with 'database is locked'. Real
    deployments always run against a pre-existing database.
    """
    db, _work = _open_manager(db_path)
    db.close()


def _claim_once_worker(args: tuple[str, str, list[str], int]) -> list[tuple[str, int]]:
    """S1: attempt to claim every key once, in seeded-random order."""
    db_path, agent_id, keys, seed = args
    rng = random.Random(seed)
    shuffled = list(keys)
    rng.shuffle(shuffled)
    wins: list[tuple[str, int]] = []
    db, work = _open_manager(db_path)
    with db:
        for key in shuffled:
            try:
                claim = work.claim(agent_id, key, ttl_minutes=60)
            except sqlite3.OperationalError:
                claim = None  # lock timeout counts as a denial
            if claim is not None:
                wins.append((key, claim.epoch))
    return wins


def _churn_worker(
    args: tuple[str, str, list[str], int, int],
) -> tuple[list[tuple[str, int]], int]:
    """S2: claim -> release churn; returns (claim events, lost-hold count).

    A "lost hold" is a complete()/abandon() that failed for a key this worker
    had just successfully claimed with a 60-minute TTL — under I-MUTEX that
    must never happen (nothing may take the key from a live holder inside its
    TTL).
    """
    db_path, agent_id, keys, seed, iterations = args
    rng = random.Random(seed)
    events: list[tuple[str, int]] = []
    lost_holds = 0
    db, work = _open_manager(db_path)
    with db:
        for _ in range(iterations):
            key = rng.choice(keys)
            try:
                claim = work.claim(agent_id, key, ttl_minutes=60)
            except sqlite3.OperationalError:
                continue
            if claim is None:
                continue
            events.append((key, claim.epoch))
            # Hold the claim briefly so concurrent claim/complete paths
            # actually observe rows in 'claimed' status — instant release
            # would leave the contention windows empty and the invariants
            # unexercised.
            time.sleep(rng.uniform(0.001, 0.01))
            try:
                if rng.random() < 0.5:
                    released = work.complete(agent_id, key)
                else:
                    released = work.abandon(agent_id, key)
            except sqlite3.OperationalError:
                released = False
            if not released:
                lost_holds += 1
    return events, lost_holds


class TestMutualExclusion:
    def test_contested_keys_have_exactly_one_winner(self, tmp_path: Path) -> None:
        """I-MUTEX: N processes race for K nonexistent keys; each key is won once."""
        db_path = str(tmp_path / "stress.db")
        _precreate_db(db_path)
        keys = [f"cascade:task:mutex-{i}" for i in range(N_KEYS)]
        args = [
            (db_path, f"contrib-stress-w{i}", keys, SEED + i) for i in range(N_WORKERS)
        ]
        with multiprocessing.Pool(N_WORKERS) as pool:
            results = pool.map(_claim_once_worker, args)

        all_wins = [win for wins in results for win in wins]
        won_keys = [key for key, _epoch in all_wins]
        assert sorted(won_keys) == sorted(keys), (
            "each contested key must be won exactly once across all workers; "
            f"duplicates/missing: {sorted(won_keys)}"
        )
        # First claim of a nonexistent key always lands at epoch 1.
        assert all(epoch == 1 for _key, epoch in all_wins)

    def test_epoch_uniqueness_under_claim_release_churn(self, tmp_path: Path) -> None:
        """I-EPOCH-UNIQ: churning claim/complete/abandon never reuses an epoch."""
        db_path = str(tmp_path / "churn.db")
        _precreate_db(db_path)
        keys = [f"cascade:task:churn-{i}" for i in range(N_KEYS)]
        args = [
            (
                db_path,
                f"contrib-stress-w{i}",
                keys,
                SEED + 100 + i,
                N_CHURN_ITERATIONS,
            )
            for i in range(N_WORKERS)
        ]
        with multiprocessing.Pool(N_WORKERS) as pool:
            results = pool.map(_churn_worker, args)

        events = [event for evts, _lost in results for event in evts]
        lost_holds = sum(lost for _evts, lost in results)

        assert lost_holds == 0, (
            f"{lost_holds} release(s) failed for a freshly-claimed key — "
            "something took the key from a live holder inside its TTL"
        )
        # No two successful claims share (key, epoch)...
        assert len(set(events)) == len(events), "duplicate (key, epoch) claim event"
        # ...and per key the consumed epochs are the gapless sequence 1..n:
        # every ownership transition bumps epoch by exactly 1, so a gap means
        # a lost update and a duplicate means two CAS winners.
        by_key: dict[str, list[int]] = {}
        for key, epoch in events:
            by_key.setdefault(key, []).append(epoch)
        for key, epochs in by_key.items():
            assert sorted(epochs) == list(
                range(1, len(epochs) + 1)
            ), f"{key}: epochs not gapless: {sorted(epochs)}"
