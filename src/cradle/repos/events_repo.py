"""Per-domain event repositories (tasks P1, P2, M1, M2).

All event tables share the shape defined in migrations/0001_init.sql:
    id, baby_id, ts, logged_by, <domain columns>, created_at, edited_at, deleted_at

milk_batch (0003) is the exception: one row per physical bottle, keyed on a
lifecycle of timestamps rather than a single ts.

Reads exclude soft-deleted rows. Timestamps are stored as UTC ISO-8601 text.
"""

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from sqlite3 import Row

from cradle.models import (
    ActivityCategory,
    ActivityEvent,
    BatchState,
    BottleColour,
    BreastSide,
    ExpressionEvent,
    FeedEvent,
    FeedMethod,
    GrowthEvent,
    GrowthMeasure,
    JournalEntry,
    JournalPhoto,
    Milestone,
    MilkBatch,
    MilkStore,
    NappyEvent,
    NappyKind,
    Note,
    SleepEvent,
    StoolColour,
    StoolConsistency,
    TemperatureEvent,
    UneditableFieldError,
    UnknownTableError,
)
from cradle.repos.db import Db

# Tables an editor/deleter may target, and the columns they may set (P2 allow-list).
EDITABLE: dict[str, frozenset[str]] = {
    "feed": frozenset({"ts", "method", "duration_min", "volume_ml", "note"}),
    "nappy": frozenset({"ts", "kind", "stool_colour", "consistency"}),
    "sleep": frozenset({"ts", "ts_end", "location"}),
    "growth": frozenset({"ts", "measure", "value", "source"}),
    "temperature": frozenset({"ts", "temp_c", "site"}),
    "milestone": frozenset({"ts", "category", "title", "note"}),
    "note": frozenset({"ts", "text", "tags"}),
    "expression": frozenset({"ts", "side", "volume_ml", "duration_min", "note"}),
    "milk_batch": frozenset(
        {
            "expressed_at",
            "stored_at",
            "store",
            "colour",
            "volume_ml",
            "state",
            "thawed_at",
            "opened_at",
            "used_at",
            "expression_id",
        }
    ),
    "activity": frozenset({"ts", "category", "duration_min", "note"}),
    "journal": frozenset({"ts", "title", "story", "temperament"}),
}

# The timestamp a transition stamps on the batch. DISCARDED stamps none: the
# bottle is gone, and why it went is a note, not a clock reading.
_BATCH_STAMP: dict[BatchState, str] = {
    BatchState.THAWED: "thawed_at",
    BatchState.OPENED: "opened_at",
    BatchState.USED: "used_at",
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
            ("baby_id", "ts", "logged_by", "kind", "stool_colour", "consistency"),
            (
                ev.baby_id,
                ev.ts.isoformat(),
                ev.logged_by,
                ev.kind.value,
                ev.stool_colour.value,
                ev.consistency.value,
            ),
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
                consistency=StoolConsistency(r["consistency"]),
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

    # ------------------------------------------------------------ expression
    def insert_expression(self, ev: ExpressionEvent) -> int:
        return self._insert(
            "expression",
            ("baby_id", "ts", "logged_by", "side", "volume_ml", "duration_min", "note"),
            (
                ev.baby_id,
                ev.ts.isoformat(),
                ev.logged_by,
                ev.side.value,
                ev.volume_ml,
                ev.duration_min,
                ev.note,
            ),
        )

    def list_expressions(
        self, limit: int = 200, since: datetime | None = None, until: datetime | None = None
    ) -> list[ExpressionEvent]:
        return [
            ExpressionEvent(
                event_id=r["id"],
                baby_id=r["baby_id"],
                ts=_require_dt(r["ts"]),
                logged_by=r["logged_by"],
                side=BreastSide(r["side"]),
                volume_ml=r["volume_ml"],
                duration_min=r["duration_min"],
                note=r["note"],
            )
            for r in self._rows("expression", limit, since, until)
        ]

    # ------------------------------------------------------------ milk batch
    def insert_milk_batch(self, batch: MilkBatch) -> int:
        """Store one bottle.

        A colour already live (LIVE_BATCH_STATES, not soft-deleted) is rejected
        by the partial unique index in migration 0003, which surfaces here as
        sqlite3.IntegrityError.
        """
        return self._insert(
            "milk_batch",
            (
                "baby_id",
                "expressed_at",
                "stored_at",
                "store",
                "colour",
                "volume_ml",
                "state",
                "thawed_at",
                "opened_at",
                "used_at",
                "expression_id",
                "logged_by",
            ),
            (
                batch.baby_id,
                batch.expressed_at.isoformat(),
                batch.stored_at.isoformat(),
                batch.store.value,
                batch.colour.value,
                batch.volume_ml,
                batch.state.value,
                batch.thawed_at.isoformat() if batch.thawed_at else None,
                batch.opened_at.isoformat() if batch.opened_at else None,
                batch.used_at.isoformat() if batch.used_at else None,
                batch.expression_id,
                batch.logged_by,
            ),
        )

    def list_milk_batches(
        self,
        states: Collection[BatchState] | None = None,
        store: MilkStore | None = None,
        limit: int = 200,
    ) -> list[MilkBatch]:
        """Batches newest-stored first. states=None means every state."""
        sql = "SELECT * FROM milk_batch WHERE deleted_at IS NULL"
        params: list[object] = []
        if states is not None:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params.extend(s.value for s in states)
        if store is not None:
            sql += " AND store = ?"
            params.append(store.value)
        sql += " ORDER BY stored_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [
            MilkBatch(
                batch_id=r["id"],
                baby_id=r["baby_id"],
                expressed_at=_require_dt(r["expressed_at"]),
                stored_at=_require_dt(r["stored_at"]),
                store=MilkStore(r["store"]),
                colour=BottleColour(r["colour"]),
                volume_ml=r["volume_ml"],
                state=BatchState(r["state"]),
                logged_by=r["logged_by"],
                thawed_at=_dt(r["thawed_at"]),
                opened_at=_dt(r["opened_at"]),
                used_at=_dt(r["used_at"]),
                expression_id=r["expression_id"],
            )
            for r in self._db.conn.execute(sql, params).fetchall()
        ]

    def set_batch_state(self, batch_id: int, state: BatchState, at: datetime) -> None:
        """Persist a transition and stamp its timestamp. Which transitions are
        legal is the service's call (V2); this only records the outcome."""
        stamp = _BATCH_STAMP.get(state)
        extra = f", {stamp} = ?" if stamp else ""
        params: list[object] = [state.value]
        if stamp:
            params.append(at.isoformat())
        self._db.conn.execute(
            f"UPDATE milk_batch SET state = ?{extra}, edited_at = ?"
            " WHERE id = ? AND deleted_at IS NULL",
            (*params, _now_iso(), batch_id),
        )
        self._db.conn.commit()

    # -------------------------------------------------------------- activity
    def insert_activity(self, ev: ActivityEvent) -> int:
        return self._insert(
            "activity",
            ("baby_id", "ts", "logged_by", "category", "duration_min", "note"),
            (
                ev.baby_id,
                ev.ts.isoformat(),
                ev.logged_by,
                ev.category.value,
                ev.duration_min,
                ev.note,
            ),
        )

    def list_activities(
        self, limit: int = 200, since: datetime | None = None, until: datetime | None = None
    ) -> list[ActivityEvent]:
        return [
            ActivityEvent(
                event_id=r["id"],
                baby_id=r["baby_id"],
                ts=_require_dt(r["ts"]),
                logged_by=r["logged_by"],
                category=ActivityCategory(r["category"]),
                duration_min=r["duration_min"],
                note=r["note"],
            )
            for r in self._rows("activity", limit, since, until)
        ]

    # --------------------------------------------------------------- journal
    def insert_journal_entry(self, ev: JournalEntry) -> int:
        return self._insert(
            "journal",
            ("baby_id", "ts", "logged_by", "title", "story", "temperament"),
            (
                ev.baby_id,
                ev.ts.isoformat(),
                ev.logged_by,
                ev.title,
                ev.story,
                ",".join(ev.temperament),
            ),
        )

    def list_journal_entries(self, limit: int = 200) -> list[JournalEntry]:
        return [self._journal_entry_of(r) for r in self._rows("journal", limit)]

    def get_journal_entry(self, entry_id: int) -> JournalEntry | None:
        r = self._db.conn.execute(
            "SELECT * FROM journal WHERE id = ? AND deleted_at IS NULL", (entry_id,)
        ).fetchone()
        return None if r is None else self._journal_entry_of(r)

    @staticmethod
    def _journal_entry_of(r: Row) -> JournalEntry:
        return JournalEntry(
            event_id=r["id"],
            baby_id=r["baby_id"],
            ts=_require_dt(r["ts"]),
            logged_by=r["logged_by"],
            title=r["title"],
            story=r["story"],
            temperament=tuple(t for t in r["temperament"].split(",") if t),
        )

    # ------------------------------------------------------- journal photo
    def insert_journal_photo(self, photo: JournalPhoto) -> int:
        cur = self._db.conn.execute(
            "INSERT INTO journal_photo"
            " (journal_entry_id, ts, content_type, caption, image, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                photo.journal_entry_id,
                photo.ts.isoformat(),
                photo.content_type,
                photo.caption,
                photo.image,
                _now_iso(),
            ),
        )
        self._db.conn.commit()
        return int(cur.lastrowid or 0)

    def list_journal_photos(self, entry_id: int) -> list[JournalPhoto]:
        rows = self._db.conn.execute(
            "SELECT * FROM journal_photo WHERE journal_entry_id = ? ORDER BY id", (entry_id,)
        ).fetchall()
        return [self._journal_photo_of(r) for r in rows]

    def list_journal_photo_refs(self, entry_id: int) -> list[tuple[int, str]]:
        """(photo_id, caption) pairs only, for a listing page's thumbnails -
        avoids pulling every photo's full image bytes into memory just to
        render a page of `<img src="/api/journal/photo/{id}">` tags."""
        rows = self._db.conn.execute(
            "SELECT id, caption FROM journal_photo WHERE journal_entry_id = ? ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [(r["id"], r["caption"]) for r in rows]

    def get_journal_photo(self, photo_id: int) -> JournalPhoto | None:
        r = self._db.conn.execute(
            "SELECT * FROM journal_photo WHERE id = ?", (photo_id,)
        ).fetchone()
        return None if r is None else self._journal_photo_of(r)

    @staticmethod
    def _journal_photo_of(r: Row) -> JournalPhoto:
        return JournalPhoto(
            photo_id=r["id"],
            journal_entry_id=r["journal_entry_id"],
            ts=_require_dt(r["ts"]),
            content_type=r["content_type"],
            caption=r["caption"],
            image=r["image"],
        )

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
