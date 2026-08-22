"""C1/C4: daily series and pattern ribbon, including midnight-crossing sleep.

C5: daily-series target tuples (feed volume, nappy ranges, sleep hours) and
age_days field.
"""

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

from _helpers import DOB, NOW, clock, make_db, make_repo

from cradle.models import FeedMethod, GrowthMeasure, NappyKind, to_local
from cradle.repos.baby_repo import BabyRepo
from cradle.services.logging_service import LoggingService
from cradle.services.series_service import MAX_DAYS, SeriesService
from cradle.services.today_service import _wet_expected

# ---- shared helpers ----

CONFIG_PATH = Path(__file__).resolve().parents[2] / "rules_config.toml"


def _build(
    config_path: Path = CONFIG_PATH,
    dob: date = DOB,
) -> tuple[LoggingService, SeriesService, BabyRepo]:
    db = make_db(dob=dob)
    repo = make_repo(db)
    baby = BabyRepo(db)
    return (
        LoggingService(repo, clock()),
        SeriesService(repo, baby, clock(), config_path),
        baby,
    )


def _build_legacy() -> tuple[LoggingService, SeriesService]:
    """Build with config for backward-compatible existing tests."""
    log, series, _ = _build()
    return log, series


def _write_config(tmpdir: Path, extra: str = "") -> Path:
    """Copy rules_config.toml into tmpdir and optionally append extra TOML."""
    dst = tmpdir / "rules_config.toml"
    shutil.copy(CONFIG_PATH, dst)
    if extra:
        with dst.open("a") as f:
            f.write("\n" + extra + "\n")
    return dst


# ================================================================= C1 tests
# (Backward-compatible; these predate C5 and must keep passing.)


def test_days_are_contiguous_and_end_today() -> None:
    _, series = _build_legacy()
    d = series.daily(days=7)
    assert len(d.days) == 7
    assert d.days[-1] == to_local(NOW).date()
    for a, b in zip(d.days, d.days[1:], strict=False):
        assert (b - a).days == 1, "no gaps, so the chart x-axis is honest"


def test_feed_counts_and_volumes_bucket_by_day() -> None:
    log, series = _build_legacy()
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=2))
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=90, ts=NOW - timedelta(hours=4))
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(days=1))
    d = series.daily(days=3)
    assert d.feeds[-1] == 2
    assert d.bottle_ml[-1] == 150
    assert d.feeds[-2] == 1
    assert d.bottle_ml[-2] == 0


def test_nappy_counts_split_wet_and_dirty() -> None:
    log, series = _build_legacy()
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=1))
    log.log_nappy(NappyKind.MIXED, ts=NOW - timedelta(hours=2))
    log.log_nappy(NappyKind.DIRTY, ts=NOW - timedelta(hours=3))
    d = series.daily(days=2)
    assert d.wet[-1] == 2, "mixed counts as both"
    assert d.dirty[-1] == 2


def test_sleep_crossing_midnight_is_split_across_days() -> None:
    log, series = _build_legacy()
    start = to_local(NOW).replace(hour=22, minute=0, second=0, microsecond=0)
    start -= timedelta(days=1)
    sid = log.toggle_sleep(ts=start)
    log.edit("sleep", sid, {"ts_end": (start + timedelta(hours=4)).isoformat()})

    d = series.daily(days=3)
    assert abs(d.sleep_hours[-2] - 2.0) < 0.01, "22:00-00:00 lands on the first day"
    assert abs(d.sleep_hours[-1] - 2.0) < 0.01, "00:00-02:00 lands on the next"
    assert abs(sum(d.sleep_hours) - 4.0) < 0.01, "no hours invented or lost"


def test_longest_sleep_tracked_separately() -> None:
    log, series = _build_legacy()
    base = to_local(NOW).replace(hour=9, minute=0, second=0, microsecond=0)
    for offset, length in ((0, 1), (3, 3)):
        sid = log.toggle_sleep(ts=base + timedelta(hours=offset))
        log.edit("sleep", sid, {"ts_end": (base + timedelta(hours=offset + length)).isoformat()})
    d = series.daily(days=1)
    assert abs(d.sleep_hours[-1] - 4.0) < 0.01
    assert abs(d.longest_sleep_hours[-1] - 3.0) < 0.01


def test_night_wakings_counted_only_overnight() -> None:
    log, series = _build_legacy()
    base = to_local(NOW).replace(hour=2, minute=0, second=0, microsecond=0)
    night = log.toggle_sleep(ts=base - timedelta(hours=2))
    log.edit("sleep", night, {"ts_end": base.isoformat()})
    noon = to_local(NOW).replace(hour=13, minute=0, second=0, microsecond=0)
    nap = log.toggle_sleep(ts=noon - timedelta(hours=1))
    log.edit("sleep", nap, {"ts_end": noon.isoformat()})

    d = series.daily(days=1)
    assert d.night_wakings[-1] == 1, "a 13:00 wake-up is not a night waking"


def test_running_sleep_counts_up_to_now() -> None:
    log, series = _build_legacy()
    log.toggle_sleep(ts=NOW - timedelta(minutes=90))
    d = series.daily(days=1)
    assert abs(d.sleep_hours[-1] - 1.5) < 0.01


def test_window_is_clamped() -> None:
    _, series = _build_legacy()
    assert len(series.daily(days=0).days) == 1
    assert len(series.daily(days=9999).days) == MAX_DAYS


def test_ribbon_places_events_at_local_hour() -> None:
    log, series = _build_legacy()
    at = to_local(NOW).replace(hour=6, minute=30, second=0, microsecond=0)
    log.log_feed(FeedMethod.BREAST_LEFT, ts=at)
    log.log_nappy(NappyKind.WET, ts=at + timedelta(minutes=15))
    r = series.ribbon(days=1)
    assert r.days[-1].feeds == (6.5,)
    assert r.days[-1].nappies == (6.75,)


def test_ribbon_sleep_span_clipped_at_midnight() -> None:
    log, series = _build_legacy()
    start = to_local(NOW).replace(hour=23, minute=0, second=0, microsecond=0)
    start -= timedelta(days=1)
    sid = log.toggle_sleep(ts=start)
    log.edit("sleep", sid, {"ts_end": (start + timedelta(hours=3)).isoformat()})

    r = series.ribbon(days=2)
    first, second = r.days[0], r.days[1]
    assert first.sleep == (
        __import__("cradle.services.series_service", fromlist=["RibbonSpan"]).RibbonSpan(
            23.0, 24.0
        ),
    )
    assert second.sleep[0].start_hour == 0.0
    assert abs(second.sleep[0].end_hour - 2.0) < 0.01


def test_ribbon_rows_sorted_within_a_day() -> None:
    log, series = _build_legacy()
    base = to_local(NOW).replace(hour=0, minute=0, second=0, microsecond=0)
    for hour in (17, 3, 11):
        log.log_feed(FeedMethod.BREAST_LEFT, ts=base + timedelta(hours=hour))
    r = series.ribbon(days=1)
    assert list(r.days[-1].feeds) == sorted(r.days[-1].feeds)


def test_empty_log_yields_zeroed_series_not_an_error() -> None:
    _, series = _build_legacy()
    d = series.daily(days=5)
    assert d.feeds == (0, 0, 0, 0, 0)
    assert d.sleep_hours == (0.0,) * 5
    r = series.ribbon(days=5)
    assert all(day.sleep == () and day.feeds == () for day in r.days)


# ================================================================ C5 tests


def test_targets_same_length_as_days() -> None:
    """DailySeries.targets carries tuples the same length as .days."""
    _, series, _ = _build()
    d = series.daily(days=7)
    n = len(d.days)
    assert n == 7
    assert len(d.targets.feed_volume_ml) == n
    assert len(d.targets.wet_min) == n
    assert len(d.targets.wet_max) == n
    assert len(d.targets.dirty_min) == n
    assert len(d.targets.dirty_max) == n
    assert len(d.targets.sleep_min_hours) == n
    assert len(d.targets.sleep_max_hours) == n
    assert len(d.age_days) == n


def test_age_days_is_chronological_not_corrected() -> None:
    """age_days equals (bucket_date - dob).days, using Baby.dob not corrected."""
    _, series, _ = _build()
    d = series.daily(days=7)
    for bucket_date, age in zip(d.days, d.age_days, strict=True):
        assert age == (bucket_date - DOB).days


def test_feed_volume_target_none_before_first_weight() -> None:
    """Feed volume target is None on every bucket before the first weight."""
    _, series, _ = _build()
    d = series.daily(days=5)
    # No weight has ever been logged.
    assert all(v is None for v in d.targets.feed_volume_ml)


def test_feed_volume_target_changes_after_new_weight() -> None:
    """Feed volume target recomputes when a new weight is logged mid-window.

    Synthetic timeline: 5kg at day-7, 6kg at day-3 (relative to NOW).
    The formula is weight_g * ml_per_kg_per_day / 1000 = weight_g * 150 / 1000.
    """
    log, series, _ = _build()

    # Log weight of 5000g at 7 days ago.
    log.log_growth(GrowthMeasure.WEIGHT, 5000, ts=NOW - timedelta(days=7))
    # Log weight of 6000g at 3 days ago.
    log.log_growth(GrowthMeasure.WEIGHT, 6000, ts=NOW - timedelta(days=3))

    d = series.daily(days=10)

    # Bucket-by-bucket: find the index of the weight-change days.
    weight1_date = to_local(NOW - timedelta(days=7)).date()
    weight2_date = to_local(NOW - timedelta(days=3)).date()

    target_5kg = int(5000 * 150 / 1000)  # 750
    target_6kg = int(6000 * 150 / 1000)  # 900

    for bucket_date, vol in zip(d.days, d.targets.feed_volume_ml, strict=True):
        age = (bucket_date - DOB).days
        if age > 180:
            assert vol is None, f"past max_age_days on {bucket_date}"
        elif bucket_date < weight1_date:
            assert vol is None, f"before first weight on {bucket_date}"
        elif bucket_date < weight2_date:
            assert vol == target_5kg, f"should use 5kg on {bucket_date}, got {vol}"
        else:
            assert vol == target_6kg, f"should use 6kg on {bucket_date}, got {vol}"


def test_wet_min_agrees_with_today_service() -> None:
    """wet_min matches wet_nappy_low.by_day_of_life, same as _wet_expected.

    This ensures the chart and the /today strip can never disagree.
    """
    import tomllib

    cfg: dict[str, object] = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            cfg = tomllib.load(fh)

    _, series, _ = _build()
    d = series.daily(days=14)

    for age, wet_min in zip(d.age_days, d.targets.wet_min, strict=True):
        expected = _wet_expected(cfg, age)
        assert wet_min == expected, (
            f"disagrees at age_days={age}: series={wet_min}, today={expected}"
        )


def test_targets_none_past_max_age_days() -> None:
    """Every target field is None once the bucket's age_days exceeds max_age_days."""
    # Use a DOB far enough in the past that the baby is >180 days old.
    old_dob = date(2025, 12, 1)  # ~7 months before NOW=2026-07-15
    log, series, _ = _build(dob=old_dob)
    # Log a weight so feed_volume_ml isn't None just because of missing weight.
    log.log_growth(GrowthMeasure.WEIGHT, 8000, ts=NOW - timedelta(days=10))

    d = series.daily(days=5)

    for age, fv, _wmin, wmax, dmin, dmax, smin, smax in zip(
        d.age_days,
        d.targets.feed_volume_ml,
        d.targets.wet_min,
        d.targets.wet_max,
        d.targets.dirty_min,
        d.targets.dirty_max,
        d.targets.sleep_min_hours,
        d.targets.sleep_max_hours,
        strict=True,
    ):
        # feed_volume_target.max_age_days = 180
        if age > 180:
            assert fv is None, f"feed_volume_ml should be None at age {age}"
        # nappy_targets.max_age_days = 90
        if age > 90:
            assert wmax is None, f"wet_max should be None at age {age}"
            assert dmin is None, f"dirty_min should be None at age {age}"
            assert dmax is None, f"dirty_max should be None at age {age}"
        # sleep_hours_target.max_age_days = 90
        if age > 90:
            assert smin is None, f"sleep_min should be None at age {age}"
            assert smax is None, f"sleep_max should be None at age {age}"
        # wet_nappy_low has no explicit max_age_days but only covers days 1-5plus;
        # past day 5 of life (age_days >= 4) it returns the 5plus value (6), not None.
        # (The NHS table stays applicable indefinitely for the existing by_day_of_life
        # keys; it just stops growing.)


def test_targets_populated_within_age_range() -> None:
    """When baby is young enough and weight is logged, all targets are populated."""
    # DOB = 2026-07-01, NOW = 2026-07-15 12:00 UTC → age 14 days on today bucket.
    log, series, _ = _build()
    log.log_growth(GrowthMeasure.WEIGHT, 4000, ts=NOW - timedelta(days=10))

    d = series.daily(days=5)
    for fv, wmin, wmax, dmin, dmax, smin, smax in zip(
        d.targets.feed_volume_ml,
        d.targets.wet_min,
        d.targets.wet_max,
        d.targets.dirty_min,
        d.targets.dirty_max,
        d.targets.sleep_min_hours,
        d.targets.sleep_max_hours,
        strict=True,
    ):
        assert fv is not None, "feed_volume_ml should be populated"
        assert fv == int(4000 * 150 / 1000), f"expected 600, got {fv}"
        assert wmin is not None, "wet_min should be populated"
        assert wmax is not None, "wet_max should be populated"
        assert dmin is not None, "dirty_min should be populated"
        assert dmax is not None, "dirty_max should be populated"
        assert smin is not None, "sleep_min should be populated"
        assert smax is not None, "sleep_max should be populated"


def test_feed_volume_target_uses_local_date_bucketing() -> None:
    """Weight bucketing uses to_local(ts).date(), consistent with daily()."""
    # This is implicitly tested by the carry-forward test, but making it explicit.
    log, series, _ = _build()
    log.log_growth(GrowthMeasure.WEIGHT, 3500, ts=NOW - timedelta(days=2))
    d = series.daily(days=3)
    weight_bucket_date = to_local(NOW - timedelta(days=2)).date()
    for bucket_date, vol in zip(d.days, d.targets.feed_volume_ml, strict=True):
        if bucket_date >= weight_bucket_date:
            assert vol == int(3500 * 150 / 1000)
        else:
            assert vol is None


def test_missing_config_yields_none_targets() -> None:
    """When config_path does not exist, all targets are None."""
    tmpdir = Path(tempfile.mkdtemp())
    try:
        missing_config = tmpdir / "nonexistent.toml"
        db = make_db()
        repo = make_repo(db)
        baby = BabyRepo(db)
        series = SeriesService(repo, baby, clock(), missing_config)
        d = series.daily(days=3)
        assert all(v is None for v in d.targets.feed_volume_ml)
        assert all(v is None for v in d.targets.wet_min)
        assert all(v is None for v in d.targets.sleep_min_hours)
    finally:
        shutil.rmtree(tmpdir)
