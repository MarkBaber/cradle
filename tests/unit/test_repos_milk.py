"""M1: expression-events and milk-batch repo coverage (round-trip, uniqueness,
migration, edit allow-list)."""

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from _helpers import NOW, make_db, make_repo

from cradle.models import (
    LIVE_BATCH_STATES,
    BatchState,
    BottleColour,
    BreastSide,
    ExpressionEvent,
    MilkBatch,
    MilkStore,
    UneditableFieldError,
)
from cradle.repos.db import Db
from cradle.repos.events_repo import EDITABLE

BASE = {"event_id": None, "baby_id": 1, "logged_by": "phone"}


def _batch(
    colour: BottleColour,
    state: BatchState = BatchState.STORED,
    store: MilkStore = MilkStore.FRIDGE,
    expressed_at: datetime = NOW - timedelta(hours=1),
    stored_at: datetime = NOW,
    thawed_at: datetime | None = None,
    opened_at: datetime | None = None,
    used_at: datetime | None = None,
) -> MilkBatch:
    return MilkBatch(
        batch_id=None,
        baby_id=1,
        expressed_at=expressed_at,
        stored_at=stored_at,
        store=store,
        colour=colour,
        volume_ml=60,
        state=state,
        logged_by="phone",
        thawed_at=thawed_at,
        opened_at=opened_at,
        used_at=used_at,
    )


# --------------------------------------------------------------- criterion 1
def test_expression_roundtrip() -> None:
    repo = make_repo(make_db())
    repo.insert_expression(
        ExpressionEvent(
            ts=NOW,
            side=BreastSide.LEFT,
            volume_ml=45,
            duration_min=12,
            note="both sides sore",
            **BASE,
        )
    )
    (e,) = repo.list_expressions()
    assert e.side is BreastSide.LEFT
    assert e.volume_ml == 45
    assert e.duration_min == 12
    assert e.note == "both sides sore"
    assert e.ts == NOW


def test_milk_batch_roundtrip_every_state() -> None:
    repo = make_repo(make_db())
    expressed_at = NOW - timedelta(hours=1)
    stored_at = NOW

    stored_id = repo.insert_milk_batch(
        _batch(BottleColour.BLUE, BatchState.STORED, expressed_at=expressed_at, stored_at=stored_at)
    )
    thawed_id = repo.insert_milk_batch(
        _batch(
            BottleColour.GREEN,
            BatchState.THAWED,
            expressed_at=expressed_at,
            stored_at=stored_at,
            thawed_at=NOW + timedelta(hours=2),
        )
    )
    opened_id = repo.insert_milk_batch(
        _batch(
            BottleColour.RED,
            BatchState.OPENED,
            expressed_at=expressed_at,
            stored_at=stored_at,
            thawed_at=NOW + timedelta(hours=2),
            opened_at=NOW + timedelta(hours=3),
        )
    )
    used_id = repo.insert_milk_batch(
        _batch(
            BottleColour.RED,
            BatchState.USED,
            expressed_at=expressed_at,
            stored_at=stored_at,
            used_at=NOW + timedelta(hours=4),
        )
    )
    discarded_id = repo.insert_milk_batch(
        _batch(
            BottleColour.RED,
            BatchState.DISCARDED,
            expressed_at=expressed_at,
            stored_at=stored_at,
        )
    )

    batches = {b.batch_id: b for b in repo.list_milk_batches()}
    assert set(batches) == {stored_id, thawed_id, opened_id, used_id, discarded_id}

    stored = batches[stored_id]
    assert stored.state is BatchState.STORED
    assert stored.expressed_at == expressed_at
    assert stored.stored_at == stored_at
    assert stored.expressed_at != stored.stored_at
    assert stored.expressed_at.tzinfo is not None
    assert stored.stored_at.tzinfo is not None

    thawed = batches[thawed_id]
    assert thawed.state is BatchState.THAWED
    assert thawed.thawed_at == NOW + timedelta(hours=2)
    assert thawed.thawed_at is not None and thawed.thawed_at.tzinfo is not None

    opened = batches[opened_id]
    assert opened.state is BatchState.OPENED
    assert opened.thawed_at == NOW + timedelta(hours=2)
    assert opened.opened_at == NOW + timedelta(hours=3)

    used = batches[used_id]
    assert used.state is BatchState.USED
    assert used.used_at == NOW + timedelta(hours=4)

    discarded = batches[discarded_id]
    assert discarded.state is BatchState.DISCARDED


# --------------------------------------------------------------- criterion 2
def test_second_live_batch_same_colour_same_store_rejected() -> None:
    repo = make_repo(make_db())
    repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.STORED, store=MilkStore.FRIDGE))
    try:
        repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.STORED, store=MilkStore.FRIDGE))
    except sqlite3.IntegrityError:
        return
    raise AssertionError("duplicate live colour in same store was accepted")


def test_second_live_batch_same_colour_other_store_rejected() -> None:
    """The bottle is physical: one colour is one bottle regardless of which
    store it currently sits in."""
    repo = make_repo(make_db())
    repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.STORED, store=MilkStore.FREEZER))
    try:
        repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.STORED, store=MilkStore.FRIDGE))
    except sqlite3.IntegrityError:
        return
    raise AssertionError("duplicate live colour across stores was accepted")


# --------------------------------------------------------------- criterion 3
def test_colour_reusable_after_used() -> None:
    repo = make_repo(make_db())
    bid = repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.STORED))
    repo.set_batch_state(bid, BatchState.USED, NOW + timedelta(hours=1))
    new_id = repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.STORED))
    assert new_id != bid


def test_colour_reusable_after_discarded() -> None:
    repo = make_repo(make_db())
    bid = repo.insert_milk_batch(_batch(BottleColour.GREEN, BatchState.STORED))
    repo.set_batch_state(bid, BatchState.DISCARDED, NOW + timedelta(hours=1))
    new_id = repo.insert_milk_batch(_batch(BottleColour.GREEN, BatchState.STORED))
    assert new_id != bid


def test_colour_reusable_after_soft_delete() -> None:
    repo = make_repo(make_db())
    bid = repo.insert_milk_batch(_batch(BottleColour.PURPLE, BatchState.STORED))
    repo.soft_delete("milk_batch", bid)
    new_id = repo.insert_milk_batch(_batch(BottleColour.PURPLE, BatchState.STORED))
    assert new_id != bid


# --------------------------------------------------------------- criterion 4
def test_migration_0003_applies_on_top_of_0001() -> None:
    root = Path(__file__).resolve().parents[2]
    init_sql = (root / "src" / "cradle" / "repos" / "migrations" / "0001_init.sql").read_text(
        encoding="utf-8"
    )

    db = Db(Path(tempfile.mkdtemp()) / "t.db")
    db.conn.executescript(init_sql)
    db.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY)")
    db.conn.execute("INSERT INTO schema_version VALUES (?)", ("0001_init.sql",))
    db.conn.execute(
        "INSERT INTO baby (baby_id, name, sex, dob, due_date, birth_weight_g)"
        " VALUES (1, 'Test', 'female', '2026-07-01', '2026-07-01', 3400)"
    )
    db.conn.commit()

    applied = db.migrate()
    assert applied >= 1

    versions = {r["version"] for r in db.conn.execute("SELECT version FROM schema_version")}
    assert "0003_milk.sql" in versions

    now_iso = NOW.isoformat()
    db.conn.execute(
        "INSERT INTO expression (baby_id, ts, logged_by, side, volume_ml, duration_min, note,"
        " created_at) VALUES (1, ?, 'phone', 'both', NULL, NULL, '', ?)",
        (now_iso, now_iso),
    )
    db.conn.execute(
        "INSERT INTO milk_batch (baby_id, expressed_at, stored_at, store, colour, volume_ml,"
        " state, logged_by, created_at)"
        " VALUES (1, ?, ?, 'fridge', 'blue', 60, 'stored', 'phone', ?)",
        (now_iso, now_iso, now_iso),
    )
    db.conn.commit()
    assert db.conn.execute("SELECT COUNT(*) c FROM expression").fetchone()["c"] == 1
    assert db.conn.execute("SELECT COUNT(*) c FROM milk_batch").fetchone()["c"] == 1

    assert db.migrate() == 0


# --------------------------------------------------------------- criterion 5
def test_editable_contains_milk_tables() -> None:
    assert "expression" in EDITABLE
    assert "milk_batch" in EDITABLE


def test_expression_edit_allow_list() -> None:
    repo = make_repo(make_db())
    eid = repo.insert_expression(ExpressionEvent(ts=NOW, side=BreastSide.RIGHT, **BASE))
    repo.edit_event("expression", eid, {"note": "topped up"})
    (e,) = repo.list_expressions()
    assert e.note == "topped up"

    try:
        repo.edit_event("expression", eid, {"created_at": NOW.isoformat()})
    except UneditableFieldError:
        pass
    else:
        raise AssertionError("expression column allow-list not enforced")


def test_milk_batch_edit_allow_list() -> None:
    repo = make_repo(make_db())
    bid = repo.insert_milk_batch(_batch(BottleColour.ORANGE, BatchState.STORED))
    repo.edit_event("milk_batch", bid, {"volume_ml": 80})
    (b,) = repo.list_milk_batches()
    assert b.volume_ml == 80

    try:
        repo.edit_event("milk_batch", bid, {"baby_id": 99})
    except UneditableFieldError:
        pass
    else:
        raise AssertionError("milk_batch column allow-list not enforced")


# --------------------------------------------------------- optional extra
def test_set_batch_state_stamps_only_relevant_column() -> None:
    repo = make_repo(make_db())
    bid = repo.insert_milk_batch(_batch(BottleColour.YELLOW, BatchState.STORED))
    at = NOW + timedelta(hours=1)
    repo.set_batch_state(bid, BatchState.THAWED, at)
    (b,) = repo.list_milk_batches()
    assert b.state is BatchState.THAWED
    assert b.thawed_at == at
    assert b.opened_at is None
    assert b.used_at is None


def test_set_batch_state_discarded_stamps_no_timestamp() -> None:
    repo = make_repo(make_db())
    bid = repo.insert_milk_batch(_batch(BottleColour.PINK, BatchState.STORED))
    at = NOW + timedelta(hours=1)
    repo.set_batch_state(bid, BatchState.DISCARDED, at)
    (b,) = repo.list_milk_batches()
    assert b.state is BatchState.DISCARDED
    assert b.thawed_at is None
    assert b.opened_at is None
    assert b.used_at is None


def test_list_milk_batches_filters_by_state_and_store() -> None:
    """V2 reads stock through these filters, so they are part of the contract."""
    repo = make_repo(make_db())
    repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.STORED, store=MilkStore.FRIDGE))
    repo.insert_milk_batch(_batch(BottleColour.GREEN, BatchState.STORED, store=MilkStore.FREEZER))
    repo.insert_milk_batch(_batch(BottleColour.RED, BatchState.USED, store=MilkStore.FRIDGE))

    live = repo.list_milk_batches(states=LIVE_BATCH_STATES)
    assert {b.colour for b in live} == {BottleColour.BLUE, BottleColour.GREEN}

    fridge_live = repo.list_milk_batches(states=LIVE_BATCH_STATES, store=MilkStore.FRIDGE)
    assert [b.colour for b in fridge_live] == [BottleColour.BLUE]

    assert len(repo.list_milk_batches()) == 3


# ---------------------------------------------------------- sanity: import
def test_live_batch_states_constant_used_by_uniqueness_rule() -> None:
    expected = frozenset({BatchState.STORED, BatchState.THAWED, BatchState.OPENED})
    assert expected == LIVE_BATCH_STATES
