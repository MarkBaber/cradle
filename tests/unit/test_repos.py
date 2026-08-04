"""P1: round-trip coverage for every event domain."""

from datetime import UTC, datetime, timedelta

from _helpers import NOW, make_db, make_repo

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
)

BASE = {"event_id": None, "baby_id": 1, "logged_by": "phone"}


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
        NappyEvent(ts=NOW, kind=NappyKind.DIRTY, stool_colour=StoolColour.YELLOW, **BASE)
    )
    (n,) = repo.list_nappies()
    assert n.kind is NappyKind.DIRTY
    assert n.stool_colour is StoolColour.YELLOW


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
