"""Timeseries and pattern views (tasks C1, C4).

Architect note: SPEC placed C1 in growth_service. Split into its own service
because these series span every domain and have nothing to do with the LMS
reference; growth_service stays about centiles.

Days are bucketed in LOCAL time. A 00:30 feed belongs to the night the parent
experienced, not to whichever UTC date it landed on.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from cradle.models import NappyKind, to_local
from cradle.ports.clock import Clock
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.events_repo import EventsRepo

DEFAULT_DAYS = 14
MAX_DAYS = 120


@dataclass(frozen=True, slots=True)
class DailySeries:
    """One value per day, oldest first, with no gaps."""

    days: tuple[date, ...]
    feeds: tuple[int, ...]
    bottle_ml: tuple[int, ...]
    wet: tuple[int, ...]
    dirty: tuple[int, ...]
    sleep_hours: tuple[float, ...]
    longest_sleep_hours: tuple[float, ...]
    night_wakings: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RibbonSpan:
    start_hour: float
    end_hour: float


@dataclass(frozen=True, slots=True)
class RibbonDay:
    day: date
    sleep: tuple[RibbonSpan, ...]
    feeds: tuple[float, ...]  # local hour-of-day, 0-24
    nappies: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Ribbon:
    """Day rows by hour columns: the 'is a pattern forming yet' view."""

    days: tuple[RibbonDay, ...]
    night_start: int
    night_end: int


NIGHT_START = 19  # local hour the night block begins
NIGHT_END = 7


def _clamp_days(days: int) -> int:
    return max(1, min(days, MAX_DAYS))


def _hour_of(moment: datetime) -> float:
    local = to_local(moment)
    return local.hour + local.minute / 60.0


class SeriesService:
    def __init__(self, repo: EventsRepo, baby_repo: BabyRepo, clock: Clock) -> None:
        self._repo = repo
        self._baby_repo = baby_repo
        self._clock = clock

    def _window(self, days: int) -> tuple[list[date], datetime]:
        days = _clamp_days(days)
        today = to_local(self._clock.now()).date()
        start_day = today - timedelta(days=days - 1)
        # Fetch from one day earlier so sleeps starting the previous evening
        # are available for clipping into the first bucket.
        since = self._clock.now() - timedelta(days=days + 1)
        return [start_day + timedelta(days=i) for i in range(days)], since

    # ------------------------------------------------------------ C1: daily
    def daily(self, days: int = DEFAULT_DAYS) -> DailySeries:
        buckets, since = self._window(days)
        index = {d: i for i, d in enumerate(buckets)}
        size = len(buckets)

        feeds = [0] * size
        bottle = [0] * size
        wet = [0] * size
        dirty = [0] * size
        sleep = [0.0] * size
        longest = [0.0] * size
        wakings = [0] * size

        for f in self._repo.list_feeds(limit=5000, since=since):
            i = index.get(to_local(f.ts).date())
            if i is not None:
                feeds[i] += 1
                bottle[i] += f.volume_ml or 0

        for n in self._repo.list_nappies(limit=5000, since=since):
            i = index.get(to_local(n.ts).date())
            if i is None:
                continue
            if n.kind in (NappyKind.WET, NappyKind.MIXED):
                wet[i] += 1
            if n.kind in (NappyKind.DIRTY, NappyKind.MIXED):
                dirty[i] += 1

        now = self._clock.now()
        for s in self._repo.list_sleeps(limit=5000, since=since):
            end = s.ts_end or now
            if end <= s.ts:
                continue
            # A sleep crossing midnight is split across both days.
            for bucket_index, hours in self._split_across_days(s.ts, end, index):
                sleep[bucket_index] += hours
                longest[bucket_index] = max(longest[bucket_index], hours)
            if s.ts_end is not None:
                i = index.get(to_local(s.ts_end).date())
                hour = _hour_of(s.ts_end)
                if i is not None and (hour < NIGHT_END or hour >= NIGHT_START):
                    wakings[i] += 1

        return DailySeries(
            days=tuple(buckets),
            feeds=tuple(feeds),
            bottle_ml=tuple(bottle),
            wet=tuple(wet),
            dirty=tuple(dirty),
            sleep_hours=tuple(round(h, 2) for h in sleep),
            longest_sleep_hours=tuple(round(h, 2) for h in longest),
            night_wakings=tuple(wakings),
        )

    @staticmethod
    def _split_across_days(
        start: datetime, end: datetime, index: dict[date, int]
    ) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        cursor = to_local(start)
        finish = to_local(end)
        while cursor < finish:
            midnight = datetime.combine(
                cursor.date() + timedelta(days=1),
                cursor.time().replace(hour=0, minute=0, second=0, microsecond=0),
                tzinfo=cursor.tzinfo,
            )
            segment_end = min(midnight, finish)
            i = index.get(cursor.date())
            if i is not None:
                out.append((i, (segment_end - cursor).total_seconds() / 3600.0))
            cursor = segment_end
        return out

    # ----------------------------------------------------------- C4: ribbon
    def ribbon(self, days: int = DEFAULT_DAYS) -> Ribbon:
        buckets, since = self._window(days)
        index = {d: i for i, d in enumerate(buckets)}
        sleep_rows: list[list[RibbonSpan]] = [[] for _ in buckets]
        feed_rows: list[list[float]] = [[] for _ in buckets]
        nappy_rows: list[list[float]] = [[] for _ in buckets]

        now = self._clock.now()
        for s in self._repo.list_sleeps(limit=5000, since=since):
            end = s.ts_end or now
            if end <= s.ts:
                continue
            for i, span in self._spans_across_days(s.ts, end, index):
                sleep_rows[i].append(span)

        # Named apart from the `i` above, which _spans_across_days binds as a
        # plain int: reusing it would fix its type and hide the None case here.
        for f in self._repo.list_feeds(limit=5000, since=since):
            row = index.get(to_local(f.ts).date())
            if row is not None:
                feed_rows[row].append(round(_hour_of(f.ts), 2))

        for n in self._repo.list_nappies(limit=5000, since=since):
            row = index.get(to_local(n.ts).date())
            if row is not None:
                nappy_rows[row].append(round(_hour_of(n.ts), 2))

        return Ribbon(
            days=tuple(
                RibbonDay(
                    day=d,
                    sleep=tuple(sorted(sleep_rows[i], key=lambda s: s.start_hour)),
                    feeds=tuple(sorted(feed_rows[i])),
                    nappies=tuple(sorted(nappy_rows[i])),
                )
                for i, d in enumerate(buckets)
            ),
            night_start=NIGHT_START,
            night_end=NIGHT_END,
        )

    @staticmethod
    def _spans_across_days(
        start: datetime, end: datetime, index: dict[date, int]
    ) -> list[tuple[int, RibbonSpan]]:
        out: list[tuple[int, RibbonSpan]] = []
        cursor = to_local(start)
        finish = to_local(end)
        while cursor < finish:
            midnight = datetime.combine(
                cursor.date() + timedelta(days=1),
                cursor.time().replace(hour=0, minute=0, second=0, microsecond=0),
                tzinfo=cursor.tzinfo,
            )
            segment_end = min(midnight, finish)
            i = index.get(cursor.date())
            if i is not None:
                start_hour = cursor.hour + cursor.minute / 60.0
                end_hour = (
                    24.0
                    if segment_end == midnight
                    else (segment_end.hour + segment_end.minute / 60.0)
                )
                out.append((i, RibbonSpan(round(start_hour, 2), round(end_hour, 2))))
            cursor = segment_end
        return out
