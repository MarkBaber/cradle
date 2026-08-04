"""B1: backup produces a readable copy with row parity, and prunes."""

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from _helpers import NOW, clock, make_db, make_repo  # noqa: E402
from backup import backup, prune  # noqa: E402

from cradle.models import FeedMethod  # noqa: E402
from cradle.services.logging_service import LoggingService  # noqa: E402


def _db_with_rows(count: int = 5):
    db = make_db()
    log = LoggingService(make_repo(db), clock())
    for _ in range(count):
        log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    return db


def test_backup_restores_with_row_parity() -> None:
    db = _db_with_rows(5)
    source = Path(db.conn.execute("PRAGMA database_list").fetchone()["file"])
    dest = Path(tempfile.mkdtemp())

    target = backup(source, dest)
    assert target.exists()

    copy = sqlite3.connect(target)
    assert copy.execute("SELECT COUNT(*) FROM feed").fetchone()[0] == 5
    assert copy.execute("SELECT COUNT(*) FROM baby").fetchone()[0] == 1
    copy.close()


def test_backup_of_missing_database_raises() -> None:
    try:
        backup(Path("/nonexistent/cradle.db"), Path(tempfile.mkdtemp()))
    except FileNotFoundError:
        return
    raise AssertionError("missing database must raise, not create an empty backup")


def test_retention_keeps_the_newest_n() -> None:
    dest = Path(tempfile.mkdtemp())
    names = [f"cradle-2026071{i:01d}T000000Z.db" for i in range(9)]
    for n in names:
        (dest / n).touch()

    removed = prune(dest, retain=3)
    remaining = sorted(p.name for p in dest.glob("cradle-*.db"))
    assert len(remaining) == 3
    assert remaining == sorted(names)[-3:], "the newest are kept"
    assert len(removed) == 6


def test_retention_is_a_noop_below_the_limit() -> None:
    dest = Path(tempfile.mkdtemp())
    (dest / "cradle-20260715T000000Z.db").touch()
    assert prune(dest, retain=30) == []
