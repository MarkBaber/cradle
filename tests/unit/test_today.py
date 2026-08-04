"""U3: trailing-24h counts against a synthetic day."""

from datetime import date, timedelta
from pathlib import Path

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import FeedMethod, NappyKind
from cradle.repos.baby_repo import BabyRepo
from cradle.services.logging_service import LoggingService
from cradle.services.today_service import TodayService

CONFIG = Path(__file__).resolve().parents[2] / "rules_config.toml"


def _build(dob: date = date(2026, 7, 1)) -> tuple[LoggingService, TodayService]:
    db = make_db(dob=dob)
    repo = make_repo(db)
    return (LoggingService(repo, clock()),
            TodayService(repo, BabyRepo(db), clock(), CONFIG))


def test_counts_only_trailing_24h() -> None:
    log, today = _build()
    for h in (1, 4, 7, 10):
        log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(hours=h))
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(hours=30))  # outside window
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=2))
    log.log_nappy(NappyKind.MIXED, ts=NOW - timedelta(hours=3))

    s = today.summary()
    assert s is not None
    assert s.feeds_24h == 4
    assert s.wet_24h == 2, "MIXED counts as both wet and dirty"
    assert s.dirty_24h == 1


def test_since_last_feed_and_expected_thresholds() -> None:
    log, today = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(minutes=90))
    s = today.summary()
    assert s is not None
    assert s.since_last_feed == timedelta(minutes=90)
    assert s.feeds_expected_min == 8, "expectations come from rules_config.toml"
    assert s.wet_expected_min == 6, "day 15 of life -> 5plus row"


def test_wet_expectation_follows_day_of_life() -> None:
    _, today = _build(dob=NOW.date())  # day 1
    s = today.summary()
    assert s is not None
    assert s.age_days == 0
    assert s.wet_expected_min == 1


def test_running_sleep_counts_up_to_now() -> None:
    log, today = _build()
    log.toggle_sleep(ts=NOW - timedelta(minutes=40))
    s = today.summary()
    assert s is not None
    assert s.running_sleep is not None
    assert s.sleep_total_24h == timedelta(minutes=40)


def test_no_profile_returns_none() -> None:
    db = make_db(seed_baby=False)
    today = TodayService(make_repo(db), BabyRepo(db), clock(), CONFIG)
    assert today.summary() is None


def test_sleep_window_is_clipped_at_24h_boundary() -> None:
    log, today = _build()
    start = NOW - timedelta(hours=30)
    sid = log.toggle_sleep(ts=start)
    log.edit("sleep", sid, {"ts_end": (NOW - timedelta(hours=23)).isoformat()})
    s = today.summary()
    assert s is not None
    assert s.sleep_total_24h == timedelta(hours=1), "only the in-window portion counts"


def test_bottle_volume_summed() -> None:
    log, today = _build()
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=1))
    log.log_feed(FeedMethod.BOTTLE_EXPRESSED, volume_ml=45, ts=NOW - timedelta(hours=5))
    s = today.summary()
    assert s is not None
    assert s.bottle_volume_ml_24h == 105
