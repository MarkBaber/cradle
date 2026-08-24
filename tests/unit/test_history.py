"""U4: unified history merge, ordering, filtering."""

from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import FeedMethod, GrowthMeasure, NappyKind, StoolColour, StoolConsistency
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


def test_history_row_carries_raw_fields() -> None:
    log, hist = _build()
    log.log_feed(FeedMethod.BOTTLE_EXPRESSED, volume_ml=60)
    log.log_feed(FeedMethod.BREAST_LEFT, duration_min=15)
    log.log_nappy(
        NappyKind.DIRTY, stool_colour=StoolColour.GREEN, consistency=StoolConsistency.SEEDY
    )
    log.log_nappy(NappyKind.WET)

    rows = hist.rows()
    wet_row = [r for r in rows if r.table == "nappy" and r.kind == "wet"][0]
    dirty_row = [r for r in rows if r.table == "nappy" and r.kind == "dirty"][0]
    bottle_row = [r for r in rows if r.table == "feed" and r.method == "bottle_expressed"][0]
    breast_row = [r for r in rows if r.table == "feed" and r.method == "breast_left"][0]

    assert wet_row.kind == "wet"
    assert dirty_row.kind == "dirty"
    assert dirty_row.stool_colour == "green"
    assert dirty_row.consistency == "seedy"
    assert bottle_row.method == "bottle_expressed"
    assert bottle_row.volume_ml == 60
    assert breast_row.method == "breast_left"
    assert breast_row.duration_min == 15


def test_history_row_carries_every_editable_field_across_domains() -> None:
    """U43: the history page's Edit/Clone panel pre-fills from HistoryRow
    alone (no by-id repo lookup was added), so every field EventsRepo.
    EDITABLE lists for a table must round-trip onto the row, not just the
    handful test_history_row_carries_raw_fields already covers."""
    log, hist = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, note="fussy")
    log.toggle_sleep()
    log.log_growth(GrowthMeasure.WEIGHT, 3600, source="midwife")
    log.log_temperature(37.2, site="ear")
    log.log_milestone("motor", "Rolled over", note="so proud")
    log.log_note("hello", tags=("a", "b"))

    rows = {r.table: r for r in hist.rows()}
    assert rows["feed"].note == "fussy"
    assert rows["sleep"].ts_end is None
    assert rows["sleep"].location == "cot"
    assert rows["growth"].measure == "weight"
    assert rows["growth"].value == 3600
    assert rows["growth"].source == "midwife"
    assert rows["temperature"].temp_c == 37.2
    assert rows["temperature"].site == "ear"
    assert rows["milestone"].category == "motor"
    assert rows["milestone"].title == "Rolled over"
    assert rows["milestone"].note == "so proud"
    assert rows["note"].text == "hello"
    assert rows["note"].tags == ("a", "b")


def test_get_row_finds_one_event_by_table_and_id() -> None:
    log, hist = _build()
    fid = log.log_feed(FeedMethod.BREAST_LEFT, duration_min=12)
    log.log_nappy(NappyKind.WET)

    row = hist.get_row("feed", fid)
    assert row is not None
    assert row.table == "feed" and row.event_id == fid and row.duration_min == 12


def test_get_row_returns_none_for_unknown_event_deleted_event_or_bad_table() -> None:
    log, hist = _build()
    fid = log.log_feed(FeedMethod.BREAST_LEFT)

    assert hist.get_row("feed", fid + 1) is None
    assert hist.get_row("not_a_table", fid) is None

    log.undo("feed", fid)
    assert hist.get_row("feed", fid) is None, "a soft-deleted event must not resurface"

