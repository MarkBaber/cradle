"""M2: developmental-activity domain coverage (closed enum, round-trip,
migration, edit allow-list, and the [activity_targets] display copy)."""

import tempfile
import tomllib
from enum import StrEnum
from pathlib import Path

from _helpers import NOW, make_db, make_repo

from cradle.models import UneditableFieldError
from cradle.models.enums import ActivityCategory
from cradle.models.events import ActivityEvent
from cradle.repos.db import Db
from cradle.repos.events_repo import EDITABLE

BASE = {"event_id": None, "baby_id": 1, "logged_by": "phone"}
ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "rules_config.toml"


def _activity_targets() -> dict[str, object]:
    with CONFIG.open("rb") as fh:
        table = tomllib.load(fh)["activity_targets"]
    assert isinstance(table, dict)
    return table


# --------------------------------------------------------------- criterion 1
def test_activity_category_is_a_closed_str_enum_of_four() -> None:
    assert issubclass(ActivityCategory, StrEnum)
    assert [c.name for c in ActivityCategory] == [
        "TUMMY_TIME",
        "READING_TALKING",
        "SENSORY_PLAY",
        "FOREIGN_LANGUAGE",
    ]


def test_activity_category_values() -> None:
    assert [c.value for c in ActivityCategory] == [
        "tummy_time",
        "reading_talking",
        "sensory_play",
        "foreign_language",
    ]


def test_activity_category_rejects_a_word_outside_the_vocabulary() -> None:
    try:
        ActivityCategory("outdoor_walk")
    except ValueError:
        pass
    else:
        raise AssertionError("ActivityCategory is not a closed vocabulary")


def test_activity_category_stored_by_value() -> None:
    """The column holds .value, not the member name."""
    db = make_db()
    repo = make_repo(db)
    repo.insert_activity(ActivityEvent(ts=NOW, category=ActivityCategory.FOREIGN_LANGUAGE, **BASE))
    row = db.conn.execute("SELECT category FROM activity").fetchone()
    assert row["category"] == "foreign_language"


# --------------------------------------------------------------- criterion 2
def test_activity_roundtrip_every_category() -> None:
    repo = make_repo(make_db())
    for i, category in enumerate(ActivityCategory):
        repo.insert_activity(
            ActivityEvent(
                ts=NOW,
                category=category,
                duration_min=i + 1,
                note=f"session {i}",
                **BASE,
            )
        )

    got = {a.category: a for a in repo.list_activities()}
    assert set(got) == set(ActivityCategory)
    for i, category in enumerate(ActivityCategory):
        ev = got[category]
        assert isinstance(ev.category, ActivityCategory)
        assert ev.duration_min == i + 1
        assert ev.note == f"session {i}"
        assert ev.ts == NOW
        assert ev.baby_id == 1
        assert ev.logged_by == "phone"
        assert ev.event_id is not None


def test_activity_duration_is_optional_for_a_two_tap_log() -> None:
    """Logged as the session starts; the minutes are a post-hoc edit."""
    repo = make_repo(make_db())
    repo.insert_activity(ActivityEvent(ts=NOW, category=ActivityCategory.TUMMY_TIME, **BASE))
    (ev,) = repo.list_activities()
    assert ev.duration_min is None
    assert ev.note == ""


# --------------------------------------------------------------- criterion 3
def test_migration_0005_applies_on_top_of_an_existing_database() -> None:
    mig_dir = ROOT / "src" / "cradle" / "repos" / "migrations"
    earlier = sorted(p for p in mig_dir.glob("*.sql") if p.name < "0005_activity.sql")

    db = Db(Path(tempfile.mkdtemp()) / "t.db")
    db.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY)")
    for path in earlier:
        db.conn.executescript(path.read_text(encoding="utf-8"))
        db.conn.execute("INSERT INTO schema_version VALUES (?)", (path.name,))
    db.conn.execute(
        "INSERT INTO baby (baby_id, name, sex, dob, due_date, birth_weight_g)"
        " VALUES (1, 'Test', 'female', '2026-07-01', '2026-07-01', 3400)"
    )
    db.conn.commit()

    applied = db.migrate()
    assert applied >= 1

    versions = {r["version"] for r in db.conn.execute("SELECT version FROM schema_version")}
    assert "0005_activity.sql" in versions

    now_iso = NOW.isoformat()
    db.conn.execute(
        "INSERT INTO activity (baby_id, ts, logged_by, category, duration_min, note,"
        " created_at) VALUES (1, ?, 'phone', 'tummy_time', 3, '', ?)",
        (now_iso, now_iso),
    )
    db.conn.commit()
    assert db.conn.execute("SELECT COUNT(*) c FROM activity").fetchone()["c"] == 1

    # Re-running is a no-op: the runner tracks applied files by filename.
    assert db.migrate() == 0


def test_activity_table_has_the_shared_event_columns() -> None:
    db = make_db()
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(activity)")}
    assert {"id", "baby_id", "ts", "logged_by", "created_at", "edited_at", "deleted_at"} <= cols
    assert {"category", "duration_min", "note"} <= cols


# --------------------------------------------------------------- criterion 4
def test_editable_contains_activity_with_a_column_allow_list() -> None:
    assert EDITABLE["activity"] == frozenset({"ts", "category", "duration_min", "note"})


def test_activity_edit_allow_list_rejects_a_column_outside_it() -> None:
    repo = make_repo(make_db())
    aid = repo.insert_activity(
        ActivityEvent(ts=NOW, category=ActivityCategory.SENSORY_PLAY, **BASE)
    )
    repo.edit_event("activity", aid, {"duration_min": 4, "note": "crinkly book"})
    (ev,) = repo.list_activities()
    assert ev.duration_min == 4
    assert ev.note == "crinkly book"

    for column, value in (("created_at", NOW.isoformat()), ("baby_id", 99)):
        try:
            repo.edit_event("activity", aid, {column: value})
        except UneditableFieldError:
            pass
        else:
            raise AssertionError(f"activity column allow-list let {column} through")


def test_activity_soft_delete_hides_the_row() -> None:
    repo = make_repo(make_db())
    aid = repo.insert_activity(ActivityEvent(ts=NOW, category=ActivityCategory.TUMMY_TIME, **BASE))
    repo.soft_delete("activity", aid)
    assert repo.list_activities() == []


# --------------------------------------------------------------- criterion 5
def test_activity_targets_has_one_entry_per_category() -> None:
    assert set(_activity_targets()) == {c.value for c in ActivityCategory}


def test_activity_targets_are_non_empty_display_strings() -> None:
    for category, text in _activity_targets().items():
        assert isinstance(text, str), category
        assert text.strip(), category


def test_activity_targets_carry_no_alert_wiring() -> None:
    """[activity_targets] is display copy, not a rule: it has no severity."""
    assert not {"severity", "rule_id", "fingerprint"} & set(_activity_targets())
