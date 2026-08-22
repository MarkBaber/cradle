"""V1: service-level logging, sleep toggle branches, undo, adjust-time."""

from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import FeedMethod, GrowthMeasure, NappyKind, StoolColour, StoolConsistency
from cradle.services.logging_service import LoggingService


def _svc() -> LoggingService:
    return LoggingService(make_repo(make_db()), clock())


def test_clock_supplies_now_when_ts_omitted() -> None:
    svc = _svc()
    svc.log_feed(FeedMethod.BREAST_LEFT)
    assert svc.recent_feeds()[0].ts == NOW


def test_explicit_ts_wins_over_clock() -> None:
    svc = _svc()
    earlier = NOW - timedelta(hours=3)
    svc.log_feed(FeedMethod.BREAST_RIGHT, ts=earlier)
    assert svc.recent_feeds()[0].ts == earlier


def test_log_every_domain() -> None:
    svc = _svc()
    assert svc.log_feed(FeedMethod.BOTTLE_EXPRESSED, volume_ml=60) > 0
    assert svc.log_nappy(NappyKind.MIXED, StoolColour.GREEN) > 0
    assert svc.log_growth(GrowthMeasure.WEIGHT, 3550) > 0
    assert svc.log_temperature(36.9) > 0
    assert svc.log_milestone("motor", "Lifts head") > 0
    assert svc.log_note("slept through", ("win",)) > 0


def test_log_nappy_defaults_consistency_to_unset() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    svc.log_nappy(NappyKind.WET)
    assert repo.list_nappies()[0].consistency == StoolConsistency.UNSET


def test_log_nappy_persists_consistency() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    svc.log_nappy(NappyKind.DIRTY, StoolColour.GREEN, StoolConsistency.SEEDY)
    assert repo.list_nappies()[0].consistency == StoolConsistency.SEEDY


def test_toggle_sleep_starts_then_ends() -> None:
    svc = _svc()
    start_id = svc.toggle_sleep()
    running = svc.running_sleep()
    assert running is not None and running.event_id == start_id
    assert running.ts_end is None

    end_id = svc.toggle_sleep(ts=NOW + timedelta(minutes=50))
    assert end_id == start_id, "toggle must close the running sleep, not open a new one"
    assert svc.running_sleep() is None


def test_undo_soft_deletes() -> None:
    svc = _svc()
    fid = svc.log_feed(FeedMethod.BREAST_LEFT)
    svc.undo("feed", fid)
    assert svc.recent_feeds() == []


def test_adjust_time_persists() -> None:
    svc = _svc()
    fid = svc.log_feed(FeedMethod.BREAST_LEFT)
    corrected = NOW - timedelta(minutes=35)
    svc.adjust_time("feed", fid, corrected)
    assert svc.recent_feeds()[0].ts == corrected


# ------------------------------------------------------------ activity (V4)
def test_log_activity_stamps_from_clock_when_ts_omitted() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    from cradle.models.enums import ActivityCategory

    svc.log_activity(ActivityCategory.TUMMY_TIME, duration_min=3)
    assert repo.list_activities()[0].ts == NOW


def test_log_activity_explicit_ts_wins() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    from cradle.models.enums import ActivityCategory

    earlier = NOW - timedelta(hours=2)
    svc.log_activity(ActivityCategory.READING_TALKING, ts=earlier)
    assert repo.list_activities()[0].ts == earlier


def test_log_activity_persists_all_fields() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    from cradle.models.enums import ActivityCategory

    event_id = svc.log_activity(
        category=ActivityCategory.SENSORY_PLAY,
        duration_min=7,
        note="enjoyed it",
    )
    assert event_id > 0
    ev = repo.list_activities()[0]
    assert ev.category == ActivityCategory.SENSORY_PLAY
    assert ev.duration_min == 7
    assert ev.note == "enjoyed it"
