"""Tests for the TTL-keyed fact bus."""

import time
from pathlib import Path

import pytest
from gptme_coordination.db import CoordinationDB
from gptme_coordination.fact_bus import FactBus


@pytest.fixture
def db(tmp_path: Path) -> CoordinationDB:
    return CoordinationDB(tmp_path / "test.db")


@pytest.fixture
def bus(db: CoordinationDB) -> FactBus:
    return FactBus(db)


class TestPublish:
    def test_publish_returns_fact(self, bus: FactBus) -> None:
        fact = bus.publish("key:one", "value-one")
        assert fact.fact_key == "key:one"
        assert fact.value == "value-one"
        assert fact.session_id is None
        assert fact.expires_at > fact.created_at

    def test_publish_with_session(self, bus: FactBus) -> None:
        fact = bus.publish("key:two", "v2", session_id="session-abc")
        assert fact.session_id == "session-abc"

    def test_publish_overwrites(self, bus: FactBus) -> None:
        bus.publish("key:ow", "old")
        fact = bus.publish("key:ow", "new")
        assert fact.value == "new"
        # Only one row for this key
        result = bus.query("key:ow")
        assert result is not None
        assert result.value == "new"

    def test_custom_ttl(self, bus: FactBus) -> None:
        fact = bus.publish("key:ttl", "val", ttl_minutes=10)
        expected = fact.created_at + 10 * 60
        assert abs(fact.expires_at - expected) < 1


class TestQuery:
    def test_query_returns_fact(self, bus: FactBus) -> None:
        bus.publish("q:one", "hello")
        result = bus.query("q:one")
        assert result is not None
        assert result.value == "hello"

    def test_query_missing_key(self, bus: FactBus) -> None:
        assert bus.query("nonexistent") is None

    def test_query_expired_returns_none(self, bus: FactBus) -> None:
        # Publish with tiny TTL (use direct DB write with past expiry)
        now = time.time()
        bus.db.conn.execute(
            "INSERT INTO fact_bus (fact_key, value, session_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("exp:key", "val", None, now - 120, now - 1),
        )
        assert bus.query("exp:key") is None


class TestList:
    def test_list_empty(self, bus: FactBus) -> None:
        assert bus.list_facts() == []

    def test_list_returns_live_facts(self, bus: FactBus) -> None:
        bus.publish("a:1", "v1")
        bus.publish("b:2", "v2")
        facts = bus.list_facts()
        assert len(facts) == 2
        keys = {f.fact_key for f in facts}
        assert keys == {"a:1", "b:2"}

    def test_list_excludes_expired(self, bus: FactBus) -> None:
        bus.publish("live:1", "ok")
        now = time.time()
        bus.db.conn.execute(
            "INSERT INTO fact_bus (fact_key, value, session_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("dead:1", "gone", None, now - 120, now - 1),
        )
        facts = bus.list_facts()
        assert len(facts) == 1
        assert facts[0].fact_key == "live:1"

    def test_list_with_prefix(self, bus: FactBus) -> None:
        bus.publish("cascade:supply:2026-07-25", "drained")
        bus.publish("cascade:supply:2026-07-24", "live")
        bus.publish("idea:821:status", "claimed")
        cascade_facts = bus.list_facts(prefix="cascade:")
        assert len(cascade_facts) == 2
        idea_facts = bus.list_facts(prefix="idea:")
        assert len(idea_facts) == 1

    def test_list_prefix_with_like_wildcards(self, bus: FactBus) -> None:
        # Keys whose prefixes contain SQL LIKE special chars (% and _) must
        # match literally, not as wildcards.
        bus.publish("50%_done:task-a", "yes")
        bus.publish("50%_done:task-b", "yes")
        bus.publish("unrelated:key", "no")
        facts = bus.list_facts(prefix="50%_done:")
        assert len(facts) == 2
        assert all(f.fact_key.startswith("50%_done:") for f in facts)


class TestPurge:
    def test_purge_removes_expired(self, bus: FactBus) -> None:
        bus.publish("live:x", "ok")
        now = time.time()
        bus.db.conn.execute(
            "INSERT INTO fact_bus (fact_key, value, session_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("dead:y", "gone", None, now - 120, now - 1),
        )
        removed = bus.purge_expired()
        assert removed == 1
        assert bus.query("live:x") is not None
        assert bus.query("dead:y") is None
