"""V4: ActivityService - trailing-24h cumulative minutes vs target."""

import tempfile
from datetime import timedelta
from pathlib import Path

from _helpers import NOW, clock, make_db, make_repo

from cradle.models.enums import ActivityCategory
from cradle.models.events import ActivityEvent
from cradle.services.activity_service import ActivityService

# The real rules_config.toml that M2 populated with [activity_targets].
REAL_CONFIG = Path(__file__).resolve().parents[2] / "rules_config.toml"

BASE_EV = {"event_id": None, "baby_id": 1, "logged_by": ""}


def _svc(config_path: Path = REAL_CONFIG) -> tuple[ActivityService, object]:
    db = make_db()
    repo = make_repo(db)
    return ActivityService(repo, clock(), config_path), repo


# ----------------------------------------------------- criterion 1: log persists
def test_log_activity_via_logging_service_persists_ts_category_duration_note() -> None:
    """log_activity is on LoggingService but its persistence is exercised here via
    EventsRepo directly so this test stays inside V4's own touches."""
    db = make_db()
    repo = make_repo(db)
    from cradle.services.logging_service import LoggingService

    svc = LoggingService(repo, clock())
    event_id = svc.log_activity(
        category=ActivityCategory.TUMMY_TIME,
        duration_min=5,
        note="good session",
    )
    assert event_id > 0
    rows = repo.list_activities()
    assert len(rows) == 1
    ev = rows[0]
    assert ev.ts == NOW
    assert ev.category == ActivityCategory.TUMMY_TIME
    assert ev.duration_min == 5
    assert ev.note == "good session"


def test_log_activity_explicit_ts_wins_over_clock() -> None:
    db = make_db()
    repo = make_repo(db)
    from cradle.services.logging_service import LoggingService

    svc = LoggingService(repo, clock())
    earlier = NOW - timedelta(hours=3)
    svc.log_activity(ActivityCategory.READING_TALKING, ts=earlier)
    assert repo.list_activities()[0].ts == earlier


# ----------------------------------------- criterion 2: trailing-24h cumulative
def test_trailing_24h_cumulative_minutes_and_session_count() -> None:
    db = make_db()
    repo = make_repo(db)
    svc = ActivityService(repo, clock(), REAL_CONFIG)

    # Two tummy-time sessions inside the window, one outside
    repo.insert_activity(
        ActivityEvent(
            ts=NOW - timedelta(hours=1),
            category=ActivityCategory.TUMMY_TIME,
            duration_min=3,
            **BASE_EV,
        )
    )
    repo.insert_activity(
        ActivityEvent(
            ts=NOW - timedelta(hours=5),
            category=ActivityCategory.TUMMY_TIME,
            duration_min=4,
            **BASE_EV,
        )
    )
    repo.insert_activity(
        ActivityEvent(
            ts=NOW - timedelta(hours=25),
            category=ActivityCategory.TUMMY_TIME,
            duration_min=99,
            **BASE_EV,
        )
    )
    # One reading session
    repo.insert_activity(
        ActivityEvent(
            ts=NOW - timedelta(hours=2),
            category=ActivityCategory.READING_TALKING,
            duration_min=10,
            **BASE_EV,
        )
    )

    summaries = {s.category: s for s in svc.summaries()}

    tt = summaries[ActivityCategory.TUMMY_TIME]
    assert tt.duration_min == 7, "3 + 4; the 25h-old event is outside the window"
    assert tt.session_count == 2

    rt = summaries[ActivityCategory.READING_TALKING]
    assert rt.duration_min == 10
    assert rt.session_count == 1


# ----------------------------------------- criterion 3: target text verbatim
def test_target_text_carried_verbatim_from_config() -> None:
    db = make_db()
    repo = make_repo(db)
    svc = ActivityService(repo, clock(), REAL_CONFIG)

    summaries = {s.category: s for s in svc.summaries()}

    # These strings must match rules_config.toml exactly - not reworded, not truncated.
    assert summaries[ActivityCategory.TUMMY_TIME].target_text == (
        "Start at 1-2 min, build to 10-15 min, several short sessions across the day"
    )
    assert summaries[ActivityCategory.READING_TALKING].target_text == (
        "15-20 min cumulative per day"
    )
    assert summaries[ActivityCategory.SENSORY_PLAY].target_text == ("1-2 brief sessions of 2-5 min")
    # foreign_language is the UNVERIFIED entry - check verbatim anyway
    assert "10-15 min" in summaries[ActivityCategory.FOREIGN_LANGUAGE].target_text


def test_target_text_absent_key_returns_empty_string() -> None:
    """If the config has no [activity_targets] at all, target_text is ''."""
    with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as fh:
        fh.write(b'[display]\ntimezone = "UTC"\n')
        empty_config = Path(fh.name)

    db = make_db()
    repo = make_repo(db)
    svc = ActivityService(repo, clock(), empty_config)

    for s in svc.summaries():
        assert s.target_text == "", f"expected empty string for {s.category}, got {s.target_text!r}"


# ----------------------------------------- criterion 4: zero-event categories
def test_category_with_no_events_reports_zero_not_error() -> None:
    """All four categories are always present; those with no events show zeros."""
    db = make_db()
    repo = make_repo(db)
    svc = ActivityService(repo, clock(), REAL_CONFIG)

    # Log only one category
    repo.insert_activity(
        ActivityEvent(
            ts=NOW - timedelta(hours=1),
            category=ActivityCategory.SENSORY_PLAY,
            duration_min=2,
            **BASE_EV,
        )
    )

    summaries = {s.category: s for s in svc.summaries()}

    # Sensory play has events
    assert summaries[ActivityCategory.SENSORY_PLAY].duration_min == 2
    assert summaries[ActivityCategory.SENSORY_PLAY].session_count == 1

    # The rest have none - must be zero, not absent, not an error
    for cat in (
        ActivityCategory.TUMMY_TIME,
        ActivityCategory.READING_TALKING,
        ActivityCategory.FOREIGN_LANGUAGE,
    ):
        assert summaries[cat].duration_min == 0, f"{cat} should be 0 minutes"
        assert summaries[cat].session_count == 0, f"{cat} should be 0 sessions"


def test_summaries_always_covers_all_four_categories() -> None:
    """summaries() returns one entry per ActivityCategory member, even with no data."""
    db = make_db()
    repo = make_repo(db)
    svc = ActivityService(repo, clock(), REAL_CONFIG)

    result = svc.summaries()
    assert len(result) == len(ActivityCategory)
    categories = {s.category for s in result}
    assert categories == set(ActivityCategory)


def test_none_duration_counts_as_zero_minutes() -> None:
    """An activity logged without a duration contributes 0 to the total."""
    db = make_db()
    repo = make_repo(db)
    svc = ActivityService(repo, clock(), REAL_CONFIG)

    repo.insert_activity(
        ActivityEvent(
            ts=NOW - timedelta(hours=1),
            category=ActivityCategory.TUMMY_TIME,
            duration_min=None,
            **BASE_EV,
        )
    )

    summaries = {s.category: s for s in svc.summaries()}
    assert summaries[ActivityCategory.TUMMY_TIME].duration_min == 0
    assert summaries[ActivityCategory.TUMMY_TIME].session_count == 1, "session still counted"
