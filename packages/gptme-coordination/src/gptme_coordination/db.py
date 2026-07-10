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

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Trigger classification
    trigger_type TEXT NOT NULL,
    source TEXT NOT NULL,
    -- Event identity (for dedup)
    external_id TEXT,
    thread_key TEXT NOT NULL,
    -- Payload
    repo TEXT,
    number INTEGER,
    title TEXT,
    url TEXT,
    payload_json TEXT,
    -- Lifecycle
    state TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    -- Timing
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at TEXT,
    completed_at TEXT,
    -- Claim
    claimed_by TEXT,
    -- Batching
    batch_id TEXT,
    -- Result
    result TEXT,
    result_detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_state ON events(state);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_key, created_at);
CREATE INDEX IF NOT EXISTS idx_events_priority ON events(state, priority DESC);
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
    3. Workspace env vars: ``BOB_WORKSPACE``, ``AGENT_WORKSPACE``, ``WORKSPACE``
    4. Git root discovered from ``cwd`` (or the current directory)
    5. ``cwd``-relative fallback outside a git repo

    The workspace env var check (3) sits before the git-root discovery so that
    scripts running from inside a submodule (e.g. ``gptme-contrib/``) do not
    inadvertently write state into the submodule directory instead of the brain
    repo.  Any agent that sets ``BOB_WORKSPACE`` or ``AGENT_WORKSPACE`` gets the
    correct DB path regardless of its working directory.
    """

    environment = os.environ if env is None else env
    if env_path := environment.get("COORDINATION_DB"):
        return Path(env_path)

    if repo_root is not None:
        return Path(repo_root) / DEFAULT_DB_PATH

    # Check agent workspace env vars before running git rev-parse.  A script
    # running with CWD inside a submodule (e.g. gptme-contrib) would otherwise
    # resolve the git root to the submodule root and write state there.
    for var in ("BOB_WORKSPACE", "AGENT_WORKSPACE", "WORKSPACE"):
        if workspace := environment.get(var):
            return Path(workspace) / DEFAULT_DB_PATH

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
