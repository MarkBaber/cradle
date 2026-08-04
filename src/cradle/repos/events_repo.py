"""Per-domain event repositories (tasks P1, P2).

All event tables share the shape defined in migrations/0001_init.sql:
    id, baby_id, ts, logged_by, <domain columns>, created_at, edited_at, deleted_at

Reads exclude soft-deleted rows. Timestamps are stored as UTC ISO-8601 text.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from sqlite3 import Row

from cradle.models import (
    FeedEvent,
    FeedMethod,
    GrowthEvent,
    GrowthMeasure,
    Milestone,
    NappyEvent,
    NappyKind,
    Note,
    SleepEvent,
    StoolColour,
    TemperatureEvent,
    UneditableFieldError,
    UnknownTableError,
)
from cradle.repos.db import Db

# Tables an editor/deleter may target, and the columns they may set (P2 allow-list).
EDITABLE: dict[str, frozenset[str]] = {
    "feed": frozenset({"ts", "method", "duration_min", "volume_ml", "note"}),
    "nappy": frozenset({"ts", "kind", "stool_colour"}),
    "sleep": frozenset({"ts", "ts_end", "location"}),
    "growth": frozenset({"ts", "measure", "value", "source"}),
    "temperature": frozenset({"ts", "temp_c", "site"}),
    "milestone": frozenset({"ts", "category", "title", "note"}),
    "note": frozenset({"ts", "text", "tags"}),
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _require_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class EventsRepo:
    def __init__(self, db: Db) -> None:
        self._db = db

    # ------------------------------------------------------------------ core
    def _insert(self, table: str, cols: Sequence[str], vals: Sequence[object]) -> int:
        placeholders = ",".join("?" * (len(cols) + 1))
        cur = self._db.conn.execute(
            f"INSERT INTO {table} ({','.join(cols)},created_at) VALUES ({placeholders})",
            (*vals, _now_iso()),
        )
        self._db.conn.commit()
        return int(cur.lastrowid or 0)

    def _rows(
        self,
        table: str,
        limit: int = 200,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Row]:
        sql = f"SELECT * FROM {table} WHERE deleted_at IS NULL"
        params: list[object] = []
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since.isoformat())
        if until is not None:
            sql += " AND ts < ?"
            params.append(until.isoformat())
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return list(self._db.conn.execute(sql, params).fetchall())

    # ------------------------------------------------------------------ feed
    def insert_feed(self, ev: FeedEvent) -> int:
        return self._insert(
            "feed",
            ("baby_id", "ts", "logged_by", "method", "duration_min", "volume_ml", "note"),
            (
                ev.baby_id,
                ev.ts.isoformat(),
                ev.logged_by,
                ev.method.value,
                ev.duration_min,
                ev.volume_ml,
                ev.note,
            ),
        )

    def list_feeds(
        self, limit: int = 200, since: datetime | None = None, until: datetime | None = None
    ) -> list[FeedEvent]:
        return [
            FeedEvent(
                event_id=r["id"],
                baby_id=r["baby_id"],
                ts=_require_dt(r["ts"]),
                logged_by=r["logged_by"],
                method=FeedMethod(r["method"]),
                duration_min=r["duration_min"],
                volume_ml=r["volume_ml"],
                note=r["note"],
            )
            for r in self._rows("feed", limit, since, until)
        ]

    # ----------------------------------------------------------------- nappy
    def insert_nappy(self, ev: NappyEvent) -> int:
        return self._insert(
            "nappy",
            ("baby_id", "ts", "logged_by", "kind", "stool_colour"),
            (ev.baby_id, ev.ts.isoformat(), ev.logged_by, ev.kind.value, ev.stool_colour.value),
        )

    def list_nappies(
        self, limit: int = 200, since: datetime | None = None, until: datetime | None = None
    ) -> list[NappyEvent]:
        return [
            NappyEvent(
                event_id=r["id"],
                baby_id=r["baby_id"],
                ts=_require_dt(r["ts"]),
                logged_by=r["logged_by"],
                kind=NappyKind(r["kind"]),
                stool_colour=StoolColour(r["stool_colour"]),
            )
            for r in self._rows("nappy", limit, since, until)
        ]

    # ----------------------------------------------------------------- sleep
    def insert_sleep_start(self, ev: SleepEvent) -> int:
        return self._insert(
            "sleep",
            ("baby_id", "ts", "ts_end", "logged_by", "location"),
            (
                ev.baby_id,
                ev.ts.isoformat(),
                ev.ts_end.isoformat() if ev.ts_end else None,
                ev.logged_by,
                ev.location,
            ),
        )

    def end_sleep(self, event_id: int, ts_end: datetime) -> None:
        self._db.conn.execute(
            "UPDATE sleep SET ts_end = ?, edited_at = ? WHERE id = ? AND deleted_at IS NULL",
            (ts_end.isoformat(), _now_iso(), event_id),
        )
        self._db.conn.commit()

    @staticmethod
    def _sleep_of(r: Row) -> SleepEvent:
        return SleepEvent(
            event_id=r["id"],
            baby_id=r["baby_id"],
            ts=_require_dt(r["ts"]),
            logged_by=r["logged_by"],
            ts_end=_dt(r["ts_end"]),
            location=r["location"],
        )

    def running_sleep(self) -> SleepEvent | None:
        r = self._db.conn.execute(
            "SELECT * FROM sleep WHERE ts_end IS NULL AND deleted_at IS NULL"
            " ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return None if r is None else self._sleep_of(r)

    def list_sleeps(
        self, limit: int = 200, since: datetime | None = None, until: datetime | None = None
    ) -> list[SleepEvent]:
        return [self._sleep_of(r) for r in self._rows("sleep", limit, since, until)]

    # ---------------------------------------------------------------- growth
    def insert_growth(self, ev: GrowthEvent) -> int:
        return self._insert(
            "growth",
            ("baby_id", "ts", "logged_by", "measure", "value", "source"),
            (ev.baby_id, ev.ts.isoformat(), ev.logged_by, ev.measure.value, ev.value, ev.source),
        )

    def list_growth(
        self, measure: GrowthMeasure | None = None, limit: int = 200
    ) -> list[GrowthEvent]:
        out = [
            GrowthEvent(
                event_id=r["id"],
                baby_id=r["baby_id"],
                ts=_require_dt(r["ts"]),
                logged_by=r["logged_by"],
                measure=GrowthMeasure(r["measure"]),
                value=r["value"],
                source=r["source"],
            )
            for r in self._rows("growth", limit)
        ]
        return [e for e in out if measure is None or e.measure is measure]

    # ----------------------------------------------------------- temperature
    def insert_temperature(self, ev: TemperatureEvent) -> int:
        return self._insert(
            "temperature",
            ("baby_id", "ts", "logged_by", "temp_c", "site"),
            (ev.baby_id, ev.ts.isoformat(), ev.logged_by, ev.temp_c, ev.site),
        )

    def list_temperatures(
        self, limit: int = 200, since: datetime | None = None, until: datetime | None = None
    ) -> list[TemperatureEvent]:
        return [
            TemperatureEvent(
                event_id=r["id"],
                baby_id=r["baby_id"],
                ts=_require_dt(r["ts"]),
                logged_by=r["logged_by"],
                temp_c=r["temp_c"],
                site=r["site"],
            )
            for r in self._rows("temperature", limit, since, until)
        ]

    # ------------------------------------------------------------- milestone
    def insert_milestone(self, ev: Milestone) -> int:
        return self._insert(
            "milestone",
            ("baby_id", "ts", "logged_by", "category", "title", "note"),
            (ev.baby_id, ev.ts.isoformat(), ev.logged_by, ev.category, ev.title, ev.note),
        )

    def list_milestones(self, limit: int = 200) -> list[Milestone]:
        return [
            Milestone(
                event_id=r["id"],
                baby_id=r["baby_id"],
                ts=_require_dt(r["ts"]),
                logged_by=r["logged_by"],
                category=r["category"],
                title=r["title"],
                note=r["note"],
            )
            for r in self._rows("milestone", limit)
        ]

    # ------------------------------------------------------------------ note
    def insert_note(self, ev: Note) -> int:
        return self._insert(
            "note",
            ("baby_id", "ts", "logged_by", "text", "tags"),
            (ev.baby_id, ev.ts.isoformat(), ev.logged_by, ev.text, ",".join(ev.tags)),
        )

    def list_notes(self, limit: int = 200) -> list[Note]:
        return [
            Note(
                event_id=r["id"],
                baby_id=r["baby_id"],
                ts=_require_dt(r["ts"]),
                logged_by=r["logged_by"],
                text=r["text"],
                tags=tuple(t for t in r["tags"].split(",") if t),
            )
            for r in self._rows("note", limit)
        ]

    # ------------------------------------------------------- export support
    def dump(self, table: str, include_deleted: bool = True) -> list[dict[str, object]]:
        """Every column of every row, for export and backup (task X1)."""
        if table not in EDITABLE:
            raise UnknownTableError(table)
        sql = f"SELECT * FROM {table}"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        return [dict(r) for r in self._db.conn.execute(sql + " ORDER BY id")]

    def restore(self, table: str, rows: list[dict[str, object]]) -> int:
        """Insert dumped rows verbatim, preserving ids. Target must be empty."""
        if table not in EDITABLE:
            raise UnknownTableError(table)
        if not rows:
            return 0
        cols = [c for c in rows[0]]
        placeholders = ",".join("?" * len(cols))
        self._db.conn.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            [tuple(r[c] for c in cols) for r in rows],
        )
        self._db.conn.commit()
        return len(rows)

    # --------------------------------------------------------- edit / delete
    def soft_delete(self, table: str, event_id: int) -> None:
        if table not in EDITABLE:
            raise UnknownTableError(table)
        self._db.conn.execute(
            f"UPDATE {table} SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (_now_iso(), event_id),
        )
        self._db.conn.commit()

    def edit_event(self, table: str, event_id: int, fields: dict[str, object]) -> None:
        if table not in EDITABLE:
            raise UnknownTableError(table)
        if not fields:
            return
        bad = set(fields) - EDITABLE[table]
        if bad:
            raise UneditableFieldError(f"{table}: {sorted(bad)}")
        assignments = ",".join(f"{c} = ?" for c in fields)
        vals = [v.isoformat() if isinstance(v, datetime) else v for v in fields.values()]
        self._db.conn.execute(
            f"UPDATE {table} SET {assignments}, edited_at = ? WHERE id = ? AND deleted_at IS NULL",
            (*vals, _now_iso(), event_id),
        )
        self._db.conn.commit()
