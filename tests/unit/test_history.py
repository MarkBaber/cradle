"""U4: unified history merge, ordering, filtering."""

from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import FeedMethod, GrowthMeasure, NappyKind
from cradle.services.history_service import HistoryService
from cradle.services.logging_service import LoggingService


def _build() -> tuple[LoggingService, HistoryService]:
    repo = make_repo(make_db())
    return LoggingService(repo, clock()), HistoryService(repo)


def test_merged_and_ordered_newest_first() -> None:
    log, hist = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(hours=3))
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=1))
    log.log_growth(GrowthMeasure.WEIGHT, 3600, ts=NOW - timedelta(hours=2))

    rows = hist.rows()
    assert [r.table for r in rows] == ["nappy", "growth", "feed"]


def test_domain_filter() -> None:
    log, hist = _build()
    log.log_feed(FeedMethod.BREAST_LEFT)
    log.log_nappy(NappyKind.DIRTY)
    rows = hist.rows(domains=("nappy",))
    assert len(rows) == 1 and rows[0].table == "nappy"


def test_date_window_applies_to_all_domains() -> None:
    log, hist = _build()
    log.log_milestone("first", "First smile", ts=NOW - timedelta(days=10))
    log.log_milestone("first", "First bath", ts=NOW - timedelta(hours=2))
    rows = hist.rows(since=NOW - timedelta(days=1))
    assert len(rows) == 1 and "First bath" in rows[0].detail


def test_deleted_rows_absent() -> None:
    log, hist = _build()
    fid = log.log_feed(FeedMethod.BREAST_LEFT)
    log.undo("feed", fid)
    assert hist.rows() == []


def test_running_sleep_rendered_distinctly() -> None:
    log, hist = _build()
    log.toggle_sleep(ts=NOW - timedelta(minutes=20))
    (row,) = hist.rows()
    assert "running" in row.detail
