"""C1/C4: daily series and pattern ribbon, including midnight-crossing sleep."""

from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import FeedMethod, NappyKind, to_local
from cradle.repos.baby_repo import BabyRepo
from cradle.services.logging_service import LoggingService
from cradle.services.series_service import MAX_DAYS, SeriesService


def _build():
    db = make_db()
    repo = make_repo(db)
    return LoggingService(repo, clock()), SeriesService(repo, BabyRepo(db), clock())


def test_days_are_contiguous_and_end_today() -> None:
    _, series = _build()
    d = series.daily(days=7)
    assert len(d.days) == 7
    assert d.days[-1] == to_local(NOW).date()
    for a, b in zip(d.days, d.days[1:], strict=False):
        assert (b - a).days == 1, "no gaps, so the chart x-axis is honest"


def test_feed_counts_and_volumes_bucket_by_day() -> None:
    log, series = _build()
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=2))
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=90, ts=NOW - timedelta(hours=4))
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(days=1))
    d = series.daily(days=3)
    assert d.feeds[-1] == 2
    assert d.bottle_ml[-1] == 150
    assert d.feeds[-2] == 1
    assert d.bottle_ml[-2] == 0


def test_nappy_counts_split_wet_and_dirty() -> None:
    log, series = _build()
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=1))
    log.log_nappy(NappyKind.MIXED, ts=NOW - timedelta(hours=2))
    log.log_nappy(NappyKind.DIRTY, ts=NOW - timedelta(hours=3))
    d = series.daily(days=2)
    assert d.wet[-1] == 2, "mixed counts as both"
    assert d.dirty[-1] == 2


def test_sleep_crossing_midnight_is_split_across_days() -> None:
    log, series = _build()
    start = to_local(NOW).replace(hour=22, minute=0, second=0, microsecond=0)
    start -= timedelta(days=1)
    sid = log.toggle_sleep(ts=start)
    log.edit("sleep", sid, {"ts_end": (start + timedelta(hours=4)).isoformat()})

    d = series.daily(days=3)
    assert abs(d.sleep_hours[-2] - 2.0) < 0.01, "22:00-00:00 lands on the first day"
    assert abs(d.sleep_hours[-1] - 2.0) < 0.01, "00:00-02:00 lands on the next"
    assert abs(sum(d.sleep_hours) - 4.0) < 0.01, "no hours invented or lost"


def test_longest_sleep_tracked_separately() -> None:
    log, series = _build()
    base = to_local(NOW).replace(hour=9, minute=0, second=0, microsecond=0)
    for offset, length in ((0, 1), (3, 3)):
        sid = log.toggle_sleep(ts=base + timedelta(hours=offset))
        log.edit("sleep", sid,
                 {"ts_end": (base + timedelta(hours=offset + length)).isoformat()})
    d = series.daily(days=1)
    assert abs(d.sleep_hours[-1] - 4.0) < 0.01
    assert abs(d.longest_sleep_hours[-1] - 3.0) < 0.01


def test_night_wakings_counted_only_overnight() -> None:
    log, series = _build()
    base = to_local(NOW).replace(hour=2, minute=0, second=0, microsecond=0)
    night = log.toggle_sleep(ts=base - timedelta(hours=2))
    log.edit("sleep", night, {"ts_end": base.isoformat()})
    noon = to_local(NOW).replace(hour=13, minute=0, second=0, microsecond=0)
    nap = log.toggle_sleep(ts=noon - timedelta(hours=1))
    log.edit("sleep", nap, {"ts_end": noon.isoformat()})

    d = series.daily(days=1)
    assert d.night_wakings[-1] == 1, "a 13:00 wake-up is not a night waking"


def test_running_sleep_counts_up_to_now() -> None:
    log, series = _build()
    log.toggle_sleep(ts=NOW - timedelta(minutes=90))
    d = series.daily(days=1)
    assert abs(d.sleep_hours[-1] - 1.5) < 0.01


def test_window_is_clamped() -> None:
    _, series = _build()
    assert len(series.daily(days=0).days) == 1
    assert len(series.daily(days=9999).days) == MAX_DAYS


def test_ribbon_places_events_at_local_hour() -> None:
    log, series = _build()
    at = to_local(NOW).replace(hour=6, minute=30, second=0, microsecond=0)
    log.log_feed(FeedMethod.BREAST_LEFT, ts=at)
    log.log_nappy(NappyKind.WET, ts=at + timedelta(minutes=15))
    r = series.ribbon(days=1)
    assert r.days[-1].feeds == (6.5,)
    assert r.days[-1].nappies == (6.75,)


def test_ribbon_sleep_span_clipped_at_midnight() -> None:
    log, series = _build()
    start = to_local(NOW).replace(hour=23, minute=0, second=0, microsecond=0)
    start -= timedelta(days=1)
    sid = log.toggle_sleep(ts=start)
    log.edit("sleep", sid, {"ts_end": (start + timedelta(hours=3)).isoformat()})

    r = series.ribbon(days=2)
    first, second = r.days[0], r.days[1]
    assert first.sleep == (__import__("cradle.services.series_service",
                                      fromlist=["RibbonSpan"]).RibbonSpan(23.0, 24.0),)
    assert second.sleep[0].start_hour == 0.0
    assert abs(second.sleep[0].end_hour - 2.0) < 0.01


def test_ribbon_rows_sorted_within_a_day() -> None:
    log, series = _build()
    base = to_local(NOW).replace(hour=0, minute=0, second=0, microsecond=0)
    for hour in (17, 3, 11):
        log.log_feed(FeedMethod.BREAST_LEFT, ts=base + timedelta(hours=hour))
    r = series.ribbon(days=1)
    assert list(r.days[-1].feeds) == sorted(r.days[-1].feeds)


def test_empty_log_yields_zeroed_series_not_an_error() -> None:
    _, series = _build()
    d = series.daily(days=5)
    assert d.feeds == (0, 0, 0, 0, 0)
    assert d.sleep_hours == (0.0,) * 5
    r = series.ribbon(days=5)
    assert all(day.sleep == () and day.feeds == () for day in r.days)
