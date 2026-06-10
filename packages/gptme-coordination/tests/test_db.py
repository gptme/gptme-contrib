"""Tests for coordination database module."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from gptme_coordination.db import CoordinationDB, resolve_coordination_db_path


class TestResolveCoordinationDbPath:
    """Tests for resolve_coordination_db_path."""

    def test_env_var_overrides_everything(self):
        """COORDINATION_DB env var takes highest priority."""
        with tempfile.TemporaryDirectory() as tmp:
            custom_path = Path(tmp) / "custom" / "coord.db"
            env = {"COORDINATION_DB": str(custom_path)}
            result = resolve_coordination_db_path(env=env)
            assert result == custom_path

    def test_explicit_repo_root(self):
        """Explicit repo_root is used when COORDINATION_DB is not set."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "myrepo"
            repo_root.mkdir()
            result = resolve_coordination_db_path(repo_root=repo_root)
            assert result == repo_root / "state/coordination/coord.db"

    def test_git_root_discovery(self):
        """Git root discovered from cwd when no explicit path is given."""
        with tempfile.TemporaryDirectory() as tmp:
            git_root = Path(tmp) / "gitrepo"
            git_root.mkdir()
            subprocess.run(["git", "init"], cwd=git_root, capture_output=True)
            # Use a subdirectory as cwd so git-discovery result differs from fallback.
            subdir = git_root / "sub"
            subdir.mkdir()
            result = resolve_coordination_db_path(cwd=subdir)
            assert result == git_root / "state/coordination/coord.db"

    def test_fallback_when_not_in_git_repo(self):
        """Falls back to cwd-relative path when outside a git repo."""
        with tempfile.TemporaryDirectory() as tmp:
            result = resolve_coordination_db_path(cwd=tmp)
            assert result == Path(tmp) / "state/coordination/coord.db"

    def test_env_overrides_git_root(self):
        """COORDINATION_DB env var still wins even if git root is discoverable."""
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "env_path" / "coord.db"
            git_root = Path(tmp) / "gitrepo"
            git_root.mkdir()
            subprocess.run(["git", "init"], cwd=git_root, capture_output=True)
            env = {"COORDINATION_DB": str(custom)}
            result = resolve_coordination_db_path(cwd=git_root, env=env)
            assert result == custom

    def test_default_env_defaults_to_os_environ(self):
        """Default env=None uses os.environ."""
        # Smoke test: no crash when COORDINATION_DB is unset
        old = os.environ.pop("COORDINATION_DB", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = resolve_coordination_db_path(cwd=tmp)
                assert isinstance(result, Path)
        finally:
            if old is not None:
                os.environ["COORDINATION_DB"] = old


class TestCoordinationDB:
    """Tests for CoordinationDB."""

    def test_init_creates_parent_dir(self):
        """DB init creates parent directories and the DB file."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "subdir" / "coord.db"
            db = CoordinationDB(str(db_path))
            _ = db.conn  # triggers DB creation
            assert db_path.exists()
            db.close()

    def test_schema_tables_exist(self):
        """Expected tables are created on init."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coord.db"
            db = CoordinationDB(str(db_path))
            tables = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [row["name"] for row in tables]
            assert "leases" in table_names
            assert "messages" in table_names
            db.close()

    def test_context_manager(self):
        """Context manager works (connect + close lifecycle)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ctx.db"
            with CoordinationDB(str(db_path)) as db:
                assert db.conn is not None
                assert db_path.exists()

    def test_wal_mode_enabled(self):
        """WAL journal mode is enabled."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "wal.db"
            db = CoordinationDB(str(db_path))
            mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"
            db.close()

    def test_busy_timeout_set(self):
        """Busy timeout is set to 5000ms."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "busy.db"
            db = CoordinationDB(str(db_path))
            timeout = db.conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert timeout == 5000
            db.close()

    def test_idempotent_init(self):
        """Initializing an existing DB doesn't throw."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "idemp.db"
            db1 = CoordinationDB(str(db_path))
            _ = db1.conn
            db1.close()
            db2 = CoordinationDB(str(db_path))
            _ = db2.conn
            db2.close()

    def test_close_releases_connection(self):
        """close() sets _conn to None."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "close.db"
            db = CoordinationDB(str(db_path))
            _ = db.conn
            db.close()
            assert db._conn is None
            # Accessing .conn after close should create a new connection
            conn2 = db.conn
            assert conn2 is not None
            db.close()
