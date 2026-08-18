"""P1: round-trip coverage for every event domain."""

import tempfile
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

from _helpers import DOB, NOW, make_db, make_repo

from cradle.models import (
    Baby,
    FeedEvent,
    FeedMethod,
    GrowthEvent,
    GrowthMeasure,
    Milestone,
    NappyEvent,
    NappyKind,
    Note,
    Sex,
    SleepEvent,
    StoolColour,
    TemperatureEvent,
)
from cradle.models.enums import StoolConsistency
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.db import Db

BASE = {"event_id": None, "baby_id": 1, "logged_by": "phone"}

_STOOL_CONSISTENCY_MIGRATION = "0004_stool_consistency.sql"


def _pre_0004_db() -> Db:
    """A Db with every migration that sorts before 0004_stool_consistency.sql
    applied by hand, mirroring what Db.migrate() itself does - used to prove
    that 0004 lands cleanly on a database that predates it."""
    db = Db(Path(tempfile.mkdtemp()) / "t.db")
    db.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY)")
    mig_dir = resources.files("cradle.repos") / "migrations"
    for entry in sorted(mig_dir.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".sql") or entry.name >= _STOOL_CONSISTENCY_MIGRATION:
            continue
        db.conn.executescript(entry.read_text(encoding="utf-8"))
        db.conn.execute("INSERT INTO schema_version VALUES (?)", (entry.name,))
    db.conn.commit()
    BabyRepo(db).upsert(
        Baby(
            baby_id=1,
            name="Test",
            sex=Sex.FEMALE,
            dob=DOB,
            due_date=DOB,
            birth_weight_g=3400,
        )
    )
    return db


def test_feed_roundtrip() -> None:
    repo = make_repo(make_db())
    repo.insert_feed(FeedEvent(ts=NOW, method=FeedMethod.BOTTLE_FORMULA, volume_ml=90, **BASE))
    (f,) = repo.list_feeds()
    assert f.method is FeedMethod.BOTTLE_FORMULA
    assert f.volume_ml == 90
    assert f.ts == NOW


def test_nappy_roundtrip() -> None:
    repo = make_repo(make_db())
    repo.insert_nappy(
        NappyEvent(
            ts=NOW,
            kind=NappyKind.DIRTY,
            stool_colour=StoolColour.YELLOW,
            consistency=StoolConsistency.SEEDY,
            **BASE,
        )
    )
    (n,) = repo.list_nappies()
    assert n.kind is NappyKind.DIRTY
    assert n.stool_colour is StoolColour.YELLOW
    assert n.consistency is StoolConsistency.SEEDY


def test_stool_consistency_defaults_to_unset_and_stores_value() -> None:
    db = make_db()
    repo = make_repo(db)
    ev = NappyEvent(ts=NOW, kind=NappyKind.DIRTY, stool_colour=StoolColour.YELLOW, **BASE)
    assert ev.consistency is StoolConsistency.UNSET

    repo.insert_nappy(
        NappyEvent(
            ts=NOW,
            kind=NappyKind.DIRTY,
            stool_colour=StoolColour.YELLOW,
            consistency=StoolConsistency.SEEDY,
            **BASE,
        )
    )
    (row,) = db.conn.execute("SELECT consistency FROM nappy").fetchall()
    assert row["consistency"] == "seedy"


def test_migration_0004_applies_to_existing_database() -> None:
    db = _pre_0004_db()
    applied = db.migrate()
    assert applied >= 1

    versions = {r["version"] for r in db.conn.execute("SELECT version FROM schema_version")}
    assert _STOOL_CONSISTENCY_MIGRATION in versions

    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(nappy)")}
    assert "consistency" in cols


def test_pre_migration_nappy_rows_read_back_as_unset() -> None:
    db = _pre_0004_db()
    db.conn.execute(
        "INSERT INTO nappy (baby_id, ts, logged_by, kind, stool_colour, created_at)"
        " VALUES (1, ?, 'phone', 'dirty', 'yellow', ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    db.conn.commit()

    db.migrate()
    repo = make_repo(db)
    (n,) = repo.list_nappies()
    assert n.consistency is StoolConsistency.UNSET


def test_sleep_start_end_and_running() -> None:
    repo = make_repo(make_db())
    sid = repo.insert_sleep_start(SleepEvent(ts=NOW, ts_end=None, location="pram", **BASE))
    running = repo.running_sleep()
    assert running is not None and running.event_id == sid
    assert running.location == "pram"

    repo.end_sleep(sid, NOW + timedelta(minutes=45))
    assert repo.running_sleep() is None
    (s,) = repo.list_sleeps()
    assert s.ts_end == NOW + timedelta(minutes=45)


def test_growth_filter_by_measure() -> None:
    repo = make_repo(make_db())
    repo.insert_growth(GrowthEvent(ts=NOW, measure=GrowthMeasure.WEIGHT, value=3600, **BASE))
    repo.insert_growth(GrowthEvent(ts=NOW, measure=GrowthMeasure.LENGTH, value=510, **BASE))
    assert len(repo.list_growth()) == 2
    (w,) = repo.list_growth(GrowthMeasure.WEIGHT)
    assert w.value == 3600


def test_temperature_milestone_note_roundtrip() -> None:
    repo = make_repo(make_db())
    repo.insert_temperature(TemperatureEvent(ts=NOW, temp_c=37.4, **BASE))
    repo.insert_milestone(Milestone(ts=NOW, category="social", title="First smile", **BASE))
    repo.insert_note(Note(ts=NOW, text="vitamin D given", tags=("meds", "routine"), **BASE))
    assert repo.list_temperatures()[0].temp_c == 37.4
    assert repo.list_milestones()[0].title == "First smile"
    assert repo.list_notes()[0].tags == ("meds", "routine")


def test_since_until_window() -> None:
    repo = make_repo(make_db())
    old = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    repo.insert_feed(FeedEvent(ts=old, method=FeedMethod.BREAST_LEFT, **BASE))
    repo.insert_feed(FeedEvent(ts=NOW, method=FeedMethod.BREAST_RIGHT, **BASE))
    recent = repo.list_feeds(since=NOW - timedelta(hours=24))
    assert len(recent) == 1
    assert recent[0].method is FeedMethod.BREAST_RIGHT
