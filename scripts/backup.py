#!/usr/bin/env python3
"""Timestamped SQLite backup via VACUUM INTO, with retention (task B1).

Uses the stdlib sqlite3 module rather than the sqlite3 CLI, which is not
installed on a minimal Raspberry Pi OS image.
"""

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

RETAIN = 30


def backup(db_path: Path, dest_dir: Path, retain: int = RETAIN) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"cradle-{stamp}.db"

    conn = sqlite3.connect(db_path)
    try:
        # VACUUM INTO produces a consistent copy without stopping writers.
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()

    prune(dest_dir, retain)
    return target


def prune(dest_dir: Path, retain: int = RETAIN) -> list[Path]:
    backups = sorted(dest_dir.glob("cradle-*.db"), key=lambda p: p.name, reverse=True)
    removed = backups[retain:]
    for old in removed:
        old.unlink()
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the CRADLE database")
    parser.add_argument("--db", default="data/cradle.db", type=Path)
    parser.add_argument("--dest", default="backups", type=Path)
    parser.add_argument("--retain", default=RETAIN, type=int)
    args = parser.parse_args()
    try:
        target = backup(args.db, args.dest, args.retain)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup written: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
