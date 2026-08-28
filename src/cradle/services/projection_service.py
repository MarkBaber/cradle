"""When the next feed and the next mess are due (task V3).

Arithmetic over the household's own log, not a clinical feature: no rule
here feeds alerts/rules.py, no copy here belongs in alerts/messages.py, and
nothing here fires or suppresses an ntfy push - the existing feed-reminder
alerts keep firing on their own thresholds regardless of what this says.
Wording stays descriptive ("estimated from your own log"), never advisory.
"""

import statistics
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cradle.models import FeedEvent, FeedMethod, NappyEvent, NappyKind
from cradle.ports.clock import Clock
from cradle.repos.events_repo import EventsRepo

# How many recent samples (bottle-to-bottle pairs, or events for a gap
# median) feed each computation. A module constant and deliberately not
# user-configurable: a sample-size box on /settings would invite fiddling
# without improving the estimate.
N = 8

# Fewer usable samples than this and a projection is not trustworthy enough
# to show a due time - cold start instead of a fabricated one.
MIN_SAMPLES = 3

# How far back events are fetched to find N samples.
FETCH_LIMIT = 100

_BOTTLE_METHODS = (FeedMethod.BOTTLE_EXPRESSED, FeedMethod.BOTTLE_FORMULA)
_DIRTY_KINDS = (NappyKind.DIRTY, NappyKind.MIXED)


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    as_of: datetime
    feed_due_at: datetime | None
    feed_overdue: bool
    hunger_fraction: float
    mess_due_at: datetime | None
    mess_overdue: bool
    mess_level_fraction: float
    dirty_due_at: datetime | None
    window_max_h: float


def _is_bottle(feed: FeedEvent) -> bool:
    return feed.method in _BOTTLE_METHODS


def _override(table: dict[str, object], key: str) -> float | None:
    """Blank or zero means "compute it"; any other positive number overrides."""
    value = table.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _hours(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds() / 3600


def _bottle_rate_samples(feeds_chrono: Sequence[FeedEvent]) -> list[float]:
    """ml/hour implied by each consecutive bottle-to-bottle pair.

    A pair with a breast feed (or anything else) between the two bottles is
    not consecutive in this sequence, so it is never formed - the breast
    feed resets the clock but leaves no rate sample.
    """
    samples: list[float] = []
    for cur, nxt in zip(feeds_chrono, feeds_chrono[1:], strict=False):
        volume = cur.volume_ml
        if volume and _is_bottle(cur) and _is_bottle(nxt):
            hours = _hours(cur.ts, nxt.ts)
            if hours > 0:
                samples.append(volume / hours)
    return samples[-N:]


def _median_bottle_volume(feeds_chrono: Sequence[FeedEvent]) -> float | None:
    volumes = [f.volume_ml for f in feeds_chrono if _is_bottle(f) and f.volume_ml][-N:]
    if len(volumes) < MIN_SAMPLES:
        return None
    return statistics.median(volumes)


def _gap_median_hours(events_chrono: Sequence[FeedEvent | NappyEvent]) -> float | None:
    """Median gap, in hours, over the last N events of *events_chrono*."""
    window = events_chrono[-N:]
    gaps = [_hours(window[i].ts, window[i + 1].ts) for i in range(len(window) - 1)]
    gaps = [g for g in gaps if g > 0]
    if len(gaps) < MIN_SAMPLES:
        return None
    return statistics.median(gaps)


class ProjectionService:
    def __init__(self, repo: EventsRepo, clock: Clock, config_path: Path) -> None:
        self._repo = repo
        self._clock = clock
        self._config_path = config_path

    def _config(self) -> dict[str, object]:
        if not self._config_path.exists():
            return {}
        with self._config_path.open("rb") as fh:
            return tomllib.load(fh)

    def projections(self) -> ProjectionResult:
        now = self._clock.now()
        raw = self._config().get("projections", {})
        cfg: dict[str, object] = raw if isinstance(raw, dict) else {}

        feeds_chrono = list(reversed(self._repo.list_feeds(limit=FETCH_LIMIT)))
        nappies_chrono = list(reversed(self._repo.list_nappies(limit=FETCH_LIMIT)))

        rate = _override(cfg, "ml_per_hour")
        if rate is None:
            samples = _bottle_rate_samples(feeds_chrono)
            rate = statistics.median(samples) if len(samples) >= MIN_SAMPLES else None

        typical_ml = _override(cfg, "typical_feed_ml")
        if typical_ml is None:
            typical_ml = _median_bottle_volume(feeds_chrono)

        feed_due_at: datetime | None = None
        hunger_fraction = 0.0
        if feeds_chrono:
            last_feed = feeds_chrono[-1]
            if rate is not None and _is_bottle(last_feed) and last_feed.volume_ml:
                feed_due_at = last_feed.ts + timedelta(hours=last_feed.volume_ml / rate)
            else:
                gap = _gap_median_hours(feeds_chrono)
                if gap is not None:
                    feed_due_at = last_feed.ts + timedelta(hours=gap)

            if rate is not None and typical_ml:
                hours_since = _hours(last_feed.ts, now)
                if hours_since > 0:
                    hunger_fraction = min(1.0, (rate * hours_since) / typical_ml)

        feed_overdue = feed_due_at is not None and now > feed_due_at

        mess_interval_h = _override(cfg, "mess_interval_min")
        if mess_interval_h is not None:
            mess_interval_h /= 60

        mess_due_at: datetime | None = None
        mess_level_fraction = 0.0
        if nappies_chrono:
            last_nappy = nappies_chrono[-1]
            gap = (
                mess_interval_h
                if mess_interval_h is not None
                else _gap_median_hours(nappies_chrono)
            )
            if gap is not None:
                mess_due_at = last_nappy.ts + timedelta(hours=gap)
                elapsed = _hours(last_nappy.ts, now)
                if elapsed > 0:
                    mess_level_fraction = min(1.0, elapsed / gap)

        mess_overdue = mess_due_at is not None and now > mess_due_at

        dirty_chrono = [n for n in nappies_chrono if n.kind in _DIRTY_KINDS]
        dirty_due_at: datetime | None = None
        if dirty_chrono:
            dirty_gap = _gap_median_hours(dirty_chrono)
            if dirty_gap is not None:
                dirty_due_at = dirty_chrono[-1].ts + timedelta(hours=dirty_gap)

        window_max_h = _override(cfg, "window_max_h") or 2.0

        return ProjectionResult(
            as_of=now,
            feed_due_at=feed_due_at,
            feed_overdue=feed_overdue,
            hunger_fraction=hunger_fraction,
            mess_due_at=mess_due_at,
            mess_overdue=mess_overdue,
            mess_level_fraction=mess_level_fraction,
            dirty_due_at=dirty_due_at,
            window_max_h=window_max_h,
        )
