"""SQLite coordination database with WAL mode for concurrent access."""

import datetime
import os
import sqlite3
import subprocess
from collections.abc import Mapping
from pathlib import Path

# Register datetime adapters to suppress Python 3.12+ deprecation warning
# about the default ISO format adapter
sqlite3.register_adapter(datetime.datetime, lambda d: d.isoformat())
sqlite3.register_adapter(datetime.date, lambda d: d.isoformat())


DEFAULT_DB_PATH = "state/coordination/coord.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    path TEXT PRIMARY KEY,
    holder TEXT,
    epoch INTEGER NOT NULL DEFAULT 0,
    acquired_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    recipient TEXT,  -- NULL = broadcast
    channel TEXT NOT NULL DEFAULT 'general',
    body TEXT NOT NULL,
    hmac TEXT,  -- HMAC authenticating (sender|recipient|channel|body)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
"""


def resolve_coordination_db_path(
    repo_root: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the authoritative coordination DB path.

    Priority order:
    1. ``COORDINATION_DB`` environment variable
    2. Explicit ``repo_root`` from the caller
    3. Git root discovered from ``cwd`` (or the current directory)
    4. ``cwd``-relative fallback outside a git repo
    """

    environment = os.environ if env is None else env
    if env_path := environment.get("COORDINATION_DB"):
        return Path(env_path)

    if repo_root is not None:
        return Path(repo_root) / DEFAULT_DB_PATH

    base_dir = Path.cwd() if cwd is None else Path(cwd)
    try:
        git_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(base_dir),
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return base_dir / DEFAULT_DB_PATH

    return Path(git_root) / DEFAULT_DB_PATH


class CoordinationDB:
    """Manages the shared SQLite coordination database.

    Uses WAL mode for concurrent readers and CAS-based leasing
    to prevent conflicts between parallel agents.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,  # autocommit for explicit transaction control
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            # Add hmac column to existing tables (migration)
            self._migrate()
        return self._conn

    def _migrate(self) -> None:
        """Apply schema migrations for databases created before schema changes."""
        conn: sqlite3.Connection | None = self._conn
        # _migrate is only called from conn property after _conn is initialized
        assert conn is not None
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN hmac TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE work ADD COLUMN hmac TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists (or work table doesn't exist yet)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "CoordinationDB":
        _ = self.conn  # ensure connected
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
