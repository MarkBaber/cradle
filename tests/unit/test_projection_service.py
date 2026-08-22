"""V3: when the next feed and the next mess are due."""

import tempfile
from datetime import timedelta
from pathlib import Path

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import FeedMethod, NappyKind
from cradle.services.logging_service import LoggingService
from cradle.services.projection_service import ProjectionService

CONFIG = Path(__file__).resolve().parents[2] / "rules_config.toml"


def _build(config_path: Path = CONFIG) -> tuple[LoggingService, ProjectionService]:
    db = make_db()
    repo = make_repo(db)
    return (LoggingService(repo, clock()), ProjectionService(repo, clock(), config_path))


def _projections_config(**overrides: object) -> Path:
    lines = ["[projections]"]
    lines += [f"{k} = {v}" for k, v in overrides.items()]
    path = Path(tempfile.mkdtemp()) / "cfg.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_synthetic_timeline_yields_feed_and_mess_due_times() -> None:
    log, proj = _build()
    for h in (13, 10, 7, 4, 1):
        log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=90, ts=NOW - timedelta(hours=h))
    for h in (21, 15, 9, 3):
        log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=h))

    r = proj.projections()

    assert r.feed_due_at == NOW + timedelta(hours=2)
    assert r.mess_due_at == NOW + timedelta(hours=3)


def test_smaller_last_bottle_is_due_sooner_at_same_rate_and_start() -> None:
    def _with_last_volume(volume_ml: int) -> ProjectionService:
        log, proj = _build()
        for h in (9, 7, 5):
            log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=h))
        log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=volume_ml, ts=NOW - timedelta(hours=3))
        return proj

    small_due = _with_last_volume(30).projections().feed_due_at
    large_due = _with_last_volume(90).projections().feed_due_at
    assert small_due is not None
    assert large_due is not None
    assert small_due < large_due


def test_breast_feed_between_bottles_contributes_no_rate_sample() -> None:
    log, proj = _build()
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=15))
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=13))
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=11))
    log.log_feed(FeedMethod.BREAST_LEFT, duration_min=10, ts=NOW - timedelta(hours=9))
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=90, ts=NOW - timedelta(hours=3))

    r = proj.projections()

    # Only 2 genuine bottle-to-bottle pairs exist (below MIN_SAMPLES=3), so the
    # rate cannot be trusted and the projection falls back to the feed-to-feed
    # gap median. If the breast-adjacent pair had wrongly counted as a rate
    # sample, the rate would be available and this would project differently.
    assert r.feed_due_at == NOW - timedelta(hours=1)
    assert r.feed_overdue is True


def test_breast_feed_as_last_feed_falls_back_to_median_gap() -> None:
    log, proj = _build()
    for h in (9, 7, 5, 3):
        log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=h))
    log.log_feed(FeedMethod.BREAST_LEFT, duration_min=15, ts=NOW - timedelta(hours=1))

    r = proj.projections()

    assert r.feed_due_at == NOW + timedelta(hours=1)
    assert r.feed_overdue is False


def test_breast_both_feed_between_bottles_contributes_no_rate_sample() -> None:
    """U32: a breast_both feed must be treated exactly like breast_left/
    breast_right - skipped as a rate sample, not folded in as a bottle."""
    log, proj = _build()
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=15))
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=13))
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=11))
    log.log_feed(FeedMethod.BREAST_BOTH, duration_min=10, ts=NOW - timedelta(hours=9))
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=90, ts=NOW - timedelta(hours=3))

    r = proj.projections()

    # Only 2 genuine bottle-to-bottle pairs exist (below MIN_SAMPLES=3), so the
    # rate cannot be trusted and the projection falls back to the feed-to-feed
    # gap median. If the breast-adjacent pair had wrongly counted as a rate
    # sample, the rate would be available and this would project differently.
    assert r.feed_due_at == NOW - timedelta(hours=1)
    assert r.feed_overdue is True


def test_breast_both_feed_as_last_feed_falls_back_to_median_gap() -> None:
    """U32: a breast_both feed as the LAST feed must fall back to the median
    feed-to-feed gap, not be treated as a bottle feed (which would require
    volume_ml; breast feeds carry duration_min instead)."""
    log, proj = _build()
    for h in (9, 7, 5, 3):
        log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=h))
    log.log_feed(FeedMethod.BREAST_BOTH, duration_min=15, ts=NOW - timedelta(hours=1))

    r = proj.projections()

    assert r.feed_due_at == NOW + timedelta(hours=1)
    assert r.feed_overdue is False


def test_combined_mess_uses_every_kind_dirty_hint_uses_dirty_and_mixed_only() -> None:
    log, proj = _build()
    log.log_nappy(NappyKind.DIRTY, ts=NOW - timedelta(hours=24))
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=20))
    log.log_nappy(NappyKind.DIRTY, ts=NOW - timedelta(hours=16))
    log.log_nappy(NappyKind.MIXED, ts=NOW - timedelta(hours=11))
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=7))
    log.log_nappy(NappyKind.DIRTY, ts=NOW - timedelta(hours=3))

    r = proj.projections()

    assert r.mess_due_at == NOW + timedelta(hours=1)
    assert r.dirty_due_at == NOW + timedelta(hours=5)


def test_override_replaces_computed_rate_and_mess_interval() -> None:
    cfg = _projections_config(ml_per_hour=100, mess_interval_min=90)
    log, proj = _build(cfg)
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=200, ts=NOW - timedelta(hours=1))
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=1))

    r = proj.projections()

    assert r.feed_due_at == NOW + timedelta(hours=1)
    assert r.mess_due_at == NOW + timedelta(minutes=30)


def test_blank_or_zero_override_reverts_to_computed_value() -> None:
    cfg = _projections_config(ml_per_hour=0, mess_interval_min=0)
    log, proj = _build(cfg)
    for h in (13, 10, 7, 4, 1):
        log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=90, ts=NOW - timedelta(hours=h))
    for h in (21, 15, 9, 3):
        log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=h))

    r = proj.projections()

    assert r.feed_due_at == NOW + timedelta(hours=2)
    assert r.mess_due_at == NOW + timedelta(hours=3)


def test_cold_start_returns_no_due_time_when_below_min_samples() -> None:
    _, proj = _build()
    r = proj.projections()
    assert r.feed_due_at is None
    assert r.mess_due_at is None
    assert r.dirty_due_at is None
    assert r.feed_overdue is False
    assert r.mess_overdue is False
    assert r.hunger_fraction == 0.0
    assert r.mess_level_fraction == 0.0

    log, proj = _build()
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=2))
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=2))
    r = proj.projections()
    assert r.feed_due_at is None
    assert r.mess_due_at is None


def test_hunger_and_mess_fractions_clamp_at_one_with_overdue_as_own_field() -> None:
    cfg = _projections_config(ml_per_hour=30, typical_feed_ml=60, mess_interval_min=60)
    log, proj = _build(cfg)
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=60, ts=NOW - timedelta(hours=10))
    log.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=5))

    r = proj.projections()

    assert r.hunger_fraction == 1.0
    assert r.mess_level_fraction == 1.0
    assert r.mess_overdue is True
