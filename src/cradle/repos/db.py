"""SQLite connection + migration runner (SPEC 5.3).

Migrations are ordered SQL files in repos/migrations/, applied at startup and
tracked in schema_version. WAL mode is enabled on every connection.

FastAPI dispatches sync route handlers to a worker-thread pool, so the single
shared connection is opened with check_same_thread=False. This is safe because
sqlite3.threadsafety == 3 (SQLite built in serialized mode) on this platform.
"""

import sqlite3
from importlib import resources
from pathlib import Path


class Db:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self.conn = sqlite3.connect(self._path, detect_types=0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def migrate(self) -> int:
        """Apply pending migrations in filename order. Returns applied count."""
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY)")
        done = {r["version"] for r in self.conn.execute("SELECT version FROM schema_version")}
        applied = 0
        mig_dir = resources.files("cradle.repos") / "migrations"
        for entry in sorted(mig_dir.iterdir(), key=lambda e: e.name):
            if not entry.name.endswith(".sql") or entry.name in done:
                continue
            self.conn.executescript(entry.read_text(encoding="utf-8"))
            self.conn.execute("INSERT INTO schema_version VALUES (?)", (entry.name,))
            applied += 1
        self.conn.commit()
        return applied

    def close(self) -> None:
        self.conn.close()
