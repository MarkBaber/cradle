"""Trailing-24h summary for the /today strip (task U3).

Expected counts come from rules_config.toml — the same thresholds the alert
engine uses, so the strip and the alerts can never disagree.
"""

import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cradle.models import Baby, NappyKind, SleepEvent
from cradle.ports.clock import Clock
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.events_repo import EventsRepo

WINDOW = timedelta(hours=24)
# Sleeps that START before the window may still OVERLAP it, so candidates are
# fetched from further back and clipped. A running sleep is always a candidate.
SLEEP_LOOKBACK = timedelta(hours=48)


@dataclass(frozen=True, slots=True)
class TodaySummary:
    as_of: datetime
    age_days: int
    feeds_24h: int
    feeds_expected_min: int | None
    wet_24h: int
    wet_expected_min: int | None
    dirty_24h: int
    since_last_feed: timedelta | None
    sleep_total_24h: timedelta
    running_sleep: SleepEvent | None
    bottle_volume_ml_24h: int


def _wet_expected(cfg: dict[str, object], age_days: int) -> int | None:
    """NHS day-of-life wet-nappy table. age_days 0 == day 1 of life."""
    table = cfg.get("wet_nappy_low", {})
    if not isinstance(table, dict):
        return None
    by_day = table.get("by_day_of_life", {})
    if not isinstance(by_day, dict):
        return None
    day_of_life = age_days + 1
    key = str(day_of_life) if day_of_life <= 4 else "5plus"
    value = by_day.get(key)
    return int(value) if isinstance(value, int) else None


class TodayService:
    def __init__(
        self,
        repo: EventsRepo,
        baby_repo: BabyRepo,
        clock: Clock,
        config_path: Path,
    ) -> None:
        self._repo = repo
        self._baby_repo = baby_repo
        self._clock = clock
        self._config_path = config_path

    def _config(self) -> dict[str, object]:
        if not self._config_path.exists():
            return {}
        with self._config_path.open("rb") as fh:
            return tomllib.load(fh)

    def summary(self) -> TodaySummary | None:
        """None when no baby profile exists yet (first run -> /settings, U7)."""
        baby: Baby | None = self._baby_repo.get()
        if baby is None:
            return None

        now = self._clock.now()
        since = now - WINDOW
        cfg = self._config()

        feeds = self._repo.list_feeds(limit=500, since=since)
        nappies = self._repo.list_nappies(limit=500, since=since)
        sleeps = self._repo.list_sleeps(limit=500, since=now - SLEEP_LOOKBACK)
        running = self._repo.running_sleep()
        if running is not None and all(s.event_id != running.event_id for s in sleeps):
            sleeps = [*sleeps, running]
        all_feeds = self._repo.list_feeds(limit=1)

        wet = sum(1 for n in nappies if n.kind in (NappyKind.WET, NappyKind.MIXED))
        dirty = sum(1 for n in nappies if n.kind in (NappyKind.DIRTY, NappyKind.MIXED))

        total_sleep = timedelta()
        for s in sleeps:
            start = max(s.ts, since)
            end = min(s.ts_end if s.ts_end is not None else now, now)
            if end > start:
                total_sleep += end - start

        feed_cfg = cfg.get("feed_count_low", {})
        feeds_min = (
            int(feed_cfg["min_feeds_24h"])
            if isinstance(feed_cfg, dict) and "min_feeds_24h" in feed_cfg
            else None
        )

        age_days = (now.date() - baby.dob).days

        return TodaySummary(
            as_of=now,
            age_days=age_days,
            feeds_24h=len(feeds),
            feeds_expected_min=feeds_min,
            wet_24h=wet,
            wet_expected_min=_wet_expected(cfg, age_days),
            dirty_24h=dirty,
            since_last_feed=(now - all_feeds[0].ts) if all_feeds else None,
            sleep_total_24h=total_sleep,
            running_sleep=running,
            bottle_volume_ml_24h=sum(f.volume_ml or 0 for f in feeds),
        )
