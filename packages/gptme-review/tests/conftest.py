"""Shared fixtures for the gptme-review test suite."""

import pytest
from gptme_review import core


@pytest.fixture(autouse=True, scope="session")
def _session_usage_ledger(tmp_path_factory):
    """Point ``core.USAGE_LEDGER`` off the live ``state/ai-review-usage.jsonl``.

    ``USAGE_LEDGER`` defaults to the host repo's state file. Run from a checkout
    that vendors this package (Bob's brain), the suite appended ``fast_fail``
    rows for ``owner/project#42`` to the production ledger that provider
    readouts and hang-rate quarantine read.

    Session scope matters: a fanout worker can log its row after the test that
    started it has returned (``review()`` raises on an expired deadline while a
    worker is still finishing), i.e. after any per-test monkeypatch is undone.
    Deliberately not restored, so such late writes still land here.
    """
    core.USAGE_LEDGER = tmp_path_factory.mktemp("ai-review") / "usage.jsonl"


@pytest.fixture(autouse=True)
def _isolated_usage_ledger(monkeypatch, tmp_path):
    """Give each test a fresh ledger; tests that read it may override this."""
    monkeypatch.setattr(core, "USAGE_LEDGER", tmp_path / "ai-review-usage.jsonl")
