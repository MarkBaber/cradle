"""Timeseries and pattern views (tasks C1, C4, C5).

Architect note: SPEC placed C1 in growth_service. Split into its own service
because these series span every domain and have nothing to do with the LMS
reference; growth_service stays about centiles.

Days are bucketed in LOCAL time. A 00:30 feed belongs to the night the parent
experienced, not to whichever UTC date it landed on.

C5: DailySeries.targets carries per-bucket recommended/expected values so C6
can draw reference lines.  Feed-volume target is only meaningful against
bottle-fed volume (DailySeries.bottle_ml) — breastfed intake has no ml figure.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from cradle.models import GrowthMeasure, NappyKind, to_local
from cradle.ports.clock import Clock
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.events_repo import EventsRepo
from cradle.services.alerts_service import load_config

DEFAULT_DAYS = 14
MAX_DAYS = 120


@dataclass(frozen=True, slots=True)
class DailyTargets:
    """Per-bucket recommended/expected values (task C5).

    Each tuple has the same length as DailySeries.days.  A None entry means
    "no target applies for this bucket" (age past the table's max, or no
    weight ever logged for feed_volume_ml).
    """

    feed_volume_ml: tuple[int | None, ...]
    wet_min: tuple[int | None, ...]
    wet_max: tuple[int | None, ...]
    dirty_min: tuple[int | None, ...]
    dirty_max: tuple[int | None, ...]
    sleep_min_hours: tuple[float | None, ...]
    sleep_max_hours: tuple[float | None, ...]


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
    age_days: tuple[int, ...]
    targets: DailyTargets


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


def _wet_min_for_age(cfg: Mapping[str, object], age_days: int) -> int | None:
    """NHS day-of-life wet-nappy table — same logic as today_service._wet_expected.

    age_days 0 == day 1 of life.  Returns None when the table is absent or the
    age exceeds its range.
    """
    table = cfg.get("wet_nappy_low", {})
    if not isinstance(table, Mapping):
        return None
    by_day = table.get("by_day_of_life", {})
    if not isinstance(by_day, Mapping):
        return None
    day_of_life = age_days + 1
    key = str(day_of_life) if day_of_life <= 4 else "5plus"
    value = by_day.get(key)
    return int(value) if isinstance(value, int) else None


def _int_or_none(table: Mapping[str, object], key: str) -> int | None:
    v = table.get(key)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _float_or_none(table: Mapping[str, object], key: str) -> float | None:
    v = table.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class SeriesService:
    def __init__(
        self, repo: EventsRepo, baby_repo: BabyRepo, clock: Clock, config_path: Path
    ) -> None:
        self._repo = repo
        self._baby_repo = baby_repo
        self._clock = clock
        self._cfg = load_config(config_path)

    def _window(self, days: int) -> tuple[list[date], datetime]:
        days = _clamp_days(days)
        today = to_local(self._clock.now()).date()
        start_day = today - timedelta(days=days - 1)
        # Fetch from one day earlier so sleeps starting the previous evening
        # are available for clipping into the first bucket.
        since = self._clock.now() - timedelta(days=days + 1)
        return [start_day + timedelta(days=i) for i in range(days)], since

    # --------------------------------------------------------- C5: targets
    def _build_targets(self, buckets: list[date], age_per_bucket: list[int]) -> DailyTargets:
        cfg = self._cfg

        # --- Feed volume target (ml_per_kg_per_day * weight_kg) ---
        fvt = cfg.get("feed_volume_target", {})
        fvt = fvt if isinstance(fvt, Mapping) else {}
        ml_per_kg = _int_or_none(fvt, "ml_per_kg_per_day")
        fvt_max_age = _int_or_none(fvt, "max_age_days")

        # Build a date→weight_g map from WEIGHT growth events, bucketed on
        # the same to_local(ts).date() the rest of daily() uses.
        weights_by_date: dict[date, int] = {}
        for ev in self._repo.list_growth(GrowthMeasure.WEIGHT):
            d = to_local(ev.ts).date()
            # list_growth is ts DESC; keep the latest entry per date.
            if d not in weights_by_date:
                weights_by_date[d] = ev.value

        # Sorted (date, weight_g) ascending for carry-forward lookup.
        weight_points = sorted(weights_by_date.items())

        def _weight_as_of(bucket: date) -> int | None:
            """Most recent weight with date <= bucket, carried forward."""
            result: int | None = None
            for wd, wg in weight_points:
                if wd > bucket:
                    break
                result = wg
            return result

        feed_vol: list[int | None] = []
        for i, bucket in enumerate(buckets):
            age = age_per_bucket[i]
            if ml_per_kg is None or (fvt_max_age is not None and age > fvt_max_age):
                feed_vol.append(None)
                continue
            w = _weight_as_of(bucket)
            if w is None:
                feed_vol.append(None)
            else:
                feed_vol.append(int(w * ml_per_kg / 1000))

        # --- Nappy targets ---
        nt = cfg.get("nappy_targets", {})
        nt = nt if isinstance(nt, Mapping) else {}
        nt_wet_max = _int_or_none(nt, "wet_max")
        nt_dirty_min = _int_or_none(nt, "dirty_min")
        nt_dirty_max = _int_or_none(nt, "dirty_max")
        nt_max_age = _int_or_none(nt, "max_age_days")

        wet_min_vals: list[int | None] = []
        wet_max_vals: list[int | None] = []
        dirty_min_vals: list[int | None] = []
        dirty_max_vals: list[int | None] = []

        for age in age_per_bucket:
            # wet_min from the existing NHS table (never duplicated into [nappy_targets])
            wm = _wet_min_for_age(cfg, age)
            wet_min_vals.append(wm)
            # wet_max from [nappy_targets] — None if past max_age_days or absent
            if nt_wet_max is not None and (nt_max_age is None or age <= nt_max_age):
                wet_max_vals.append(nt_wet_max)
            else:
                wet_max_vals.append(None)
            # dirty_min/max from [nappy_targets]
            if nt_max_age is not None and age > nt_max_age:
                dirty_min_vals.append(None)
                dirty_max_vals.append(None)
            else:
                dirty_min_vals.append(nt_dirty_min)
                dirty_max_vals.append(nt_dirty_max)

        # --- Sleep targets ---
        st = cfg.get("sleep_hours_target", {})
        st = st if isinstance(st, Mapping) else {}
        st_min = _float_or_none(st, "min_hours")
        st_max = _float_or_none(st, "max_hours")
        st_max_age = _int_or_none(st, "max_age_days")

        sleep_min_vals: list[float | None] = []
        sleep_max_vals: list[float | None] = []
        for age in age_per_bucket:
            if st_max_age is not None and age > st_max_age:
                sleep_min_vals.append(None)
                sleep_max_vals.append(None)
            else:
                sleep_min_vals.append(st_min)
                sleep_max_vals.append(st_max)

        return DailyTargets(
            feed_volume_ml=tuple(feed_vol),
            wet_min=tuple(wet_min_vals),
            wet_max=tuple(wet_max_vals),
            dirty_min=tuple(dirty_min_vals),
            dirty_max=tuple(dirty_max_vals),
            sleep_min_hours=tuple(sleep_min_vals),
            sleep_max_hours=tuple(sleep_max_vals),
        )

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

        # C5: chronological age per bucket (NOT corrected age).
        baby = self._baby_repo.get()
        age_per_bucket = [(bucket - baby.dob).days for bucket in buckets] if baby else [0] * size
        targets = (
            self._build_targets(buckets, age_per_bucket)
            if baby
            else DailyTargets(
                feed_volume_ml=(None,) * size,
                wet_min=(None,) * size,
                wet_max=(None,) * size,
                dirty_min=(None,) * size,
                dirty_max=(None,) * size,
                sleep_min_hours=(None,) * size,
                sleep_max_hours=(None,) * size,
            )
        )

        return DailySeries(
            days=tuple(buckets),
            feeds=tuple(feeds),
            bottle_ml=tuple(bottle),
            wet=tuple(wet),
            dirty=tuple(dirty),
            sleep_hours=tuple(round(h, 2) for h in sleep),
            longest_sleep_hours=tuple(round(h, 2) for h in longest),
            night_wakings=tuple(wakings),
            age_days=tuple(age_per_bucket),
            targets=targets,
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
