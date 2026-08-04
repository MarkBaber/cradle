"""A3: feed rules, including config-driven thresholds."""

from datetime import timedelta

import _facts as F


def test_feed_gap_fires_beyond_threshold() -> None:
    f = F.fire("FEED_GAP", F.facts(feeds=(F.feed(5),)))
    assert f is not None
    assert f.rule_id == "FEED_GAP"
    assert "5 hours" in f.message


def test_feed_gap_silent_within_threshold() -> None:
    assert F.fire("FEED_GAP", F.facts(feeds=(F.feed(3.9),))) is None


def test_feed_gap_boundary_is_exclusive() -> None:
    assert F.fire("FEED_GAP", F.facts(feeds=(F.feed(4.0),))) is None
    assert F.fire("FEED_GAP", F.facts(feeds=(F.feed(4.01),))) is not None


def test_feed_gap_ignored_past_max_age() -> None:
    old = F.DOB.replace(month=5)  # ~2 months old
    assert F.fire("FEED_GAP", F.facts(dob=old, feeds=(F.feed(9),))) is None


def test_feed_gap_silent_with_no_feeds_at_all() -> None:
    """An empty log is MEASUREMENT_GAP's business, not a feeding claim."""
    assert F.fire("FEED_GAP", F.facts()) is None


def test_feed_gap_fingerprint_is_per_episode() -> None:
    """Two sweeps during one gap must share a fingerprint, so it notifies once."""
    a = F.fire("FEED_GAP", F.facts(feeds=(F.feed(5),)))
    later = F.facts(now=F.NOW + timedelta(minutes=30), feeds=(F.feed(5),))  # same feed, later sweep
    b = F.fire("FEED_GAP", later)
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint


def test_feed_count_low_fires() -> None:
    f = F.fire("FEED_COUNT_LOW", F.facts(feeds=F.feeds_every(5, 5)))
    assert f is not None
    assert "5 feeds" in f.message


def test_feed_count_boundary_at_eight() -> None:
    assert F.fire("FEED_COUNT_LOW", F.facts(feeds=F.feeds_every(2.5, 8))) is None
    assert F.fire("FEED_COUNT_LOW", F.facts(feeds=F.feeds_every(2.5, 7))) is not None


def test_feed_count_only_counts_last_24h() -> None:
    inside = F.feeds_every(2, 6)
    outside = tuple(F.feed(30 + i) for i in range(6))
    assert F.fire("FEED_COUNT_LOW", F.facts(feeds=inside + outside)) is not None


def test_thresholds_come_from_config_not_code() -> None:
    relaxed = {
        "feed_count_low": {"min_feeds_24h": 3, "max_age_days": 28},
        "feed_gap": {"max_gap_hours": 12.0, "max_age_days": 28},
    }
    facts = F.facts(feeds=F.feeds_every(3, 5))
    assert F.fire("FEED_COUNT_LOW", facts) is not None, "fires under shipped config"
    assert F.fire("FEED_COUNT_LOW", facts, relaxed) is None, "config must win"
