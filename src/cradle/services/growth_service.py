"""Growth assessment and centile chart series (tasks G1, U5).

The LMS table is injected so the service degrades honestly: if reference data
is missing the assessment still returns raw measurements and weight-loss
percentages (which need no reference table), and marks centiles unavailable
rather than inventing them.
"""

from dataclasses import dataclass
from datetime import date, datetime

from cradle.models import (
    Baby,
    GrowthEvent,
    GrowthMeasure,
    ReferenceDataMissingError,
    to_local,
)
from cradle.reference.lms import LmsTable, corrected_age_days, is_preterm
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.events_repo import EventsRepo

# UK-WHO printed charts carry these curves.
CENTILES = (0.4, 2.0, 9.0, 25.0, 50.0, 75.0, 91.0, 98.0, 99.6)
UNITS = {
    GrowthMeasure.WEIGHT: "g",
    GrowthMeasure.LENGTH: "mm",
    GrowthMeasure.HEAD_CIRC: "mm",
}


@dataclass(frozen=True, slots=True)
class MeasureAssessment:
    measure: GrowthMeasure
    ts: datetime
    value: int
    unit: str
    age_days: int
    corrected_age_days: int
    z: float | None
    centile: float | None
    unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class GrowthAssessment:
    baby: Baby
    corrected: bool
    table_version: str | None
    measures: tuple[MeasureAssessment, ...]
    weight_loss_pct: float | None  # positive = below birth weight
    regained_birth_weight: bool | None
    latest_weight_z: float | None
    baseline_weight_z: float | None  # earliest weight z, for CENTILE_CROSS (A5)


@dataclass(frozen=True, slots=True)
class ChartSeries:
    measure: GrowthMeasure
    unit: str
    ages: tuple[int, ...]
    curves: dict[str, tuple[float, ...]]  # centile label -> values
    trajectory: tuple[tuple[int, float, str], ...]  # (age_days, value, date)
    frames: tuple[int, ...]  # trajectory prefix lengths (C3)
    unavailable_reason: str | None


ChartCacheKey = tuple[
    int, str, date, date, tuple[tuple[int | None, datetime, int], ...]
]


class GrowthService:
    def __init__(
        self,
        repo: EventsRepo,
        baby_repo: BabyRepo,
        table: LmsTable | None = None,
        table_error: str | None = None,
    ) -> None:
        self._repo = repo
        self._baby_repo = baby_repo
        self._table = table
        self._table_error = table_error
        # centile_chart_series (task U41) re-runs the LMS interpolation loop
        # (~9 centiles x up to 120 ages) on every call unless memoised here.
        # Keyed on everything that can change the result cheaply: the baby's
        # identity/sex/dob/due_date and, per event, (id, ts, value) - so a new
        # event, an edited value, an undo, or a profile change all miss the
        # cache, while an unchanged history hits it.
        self._chart_cache: dict[GrowthMeasure, tuple[ChartCacheKey, ChartSeries]] = {}

    # ------------------------------------------------------------- internals
    def _age(self, baby: Baby, on: date) -> tuple[int, int]:
        chronological = (on - baby.dob).days
        return chronological, corrected_age_days(baby.dob, baby.due_date, on)

    def _z(
        self, baby: Baby, measure: GrowthMeasure, age_days: int, value: int
    ) -> tuple[float | None, float | None, str | None]:
        if self._table is None:
            return None, None, self._table_error or "reference data not installed"
        try:
            result = self._table.zscore(measure, baby.sex, age_days, float(value), corrected=True)
        except (LookupError, ReferenceDataMissingError, ValueError) as exc:
            return None, None, str(exc)
        return result.z, result.centile, None

    # ------------------------------------------------------------ assessment
    def assessment(self) -> GrowthAssessment | None:
        baby = self._baby_repo.get()
        if baby is None:
            return None

        measures: list[MeasureAssessment] = []
        for measure in GrowthMeasure:
            events = self._repo.list_growth(measure)
            if not events:
                continue
            latest = events[0]
            chronological, corrected = self._age(baby, latest.ts.date())
            z, centile, reason = self._z(baby, measure, corrected, latest.value)
            measures.append(
                MeasureAssessment(
                    measure=measure,
                    ts=latest.ts,
                    value=latest.value,
                    unit=UNITS[measure],
                    age_days=chronological,
                    corrected_age_days=corrected,
                    z=z,
                    centile=centile,
                    unavailable_reason=reason,
                )
            )

        weights = sorted(self._repo.list_growth(GrowthMeasure.WEIGHT), key=lambda e: e.ts)
        loss_pct: float | None = None
        regained: bool | None = None
        if weights and baby.birth_weight_g > 0:
            latest_w = weights[-1].value
            loss_pct = (baby.birth_weight_g - latest_w) / baby.birth_weight_g * 100.0
            regained = latest_w >= baby.birth_weight_g

        latest_z = baseline_z = None
        if weights:
            _, c_last = self._age(baby, weights[-1].ts.date())
            latest_z, _, _ = self._z(baby, GrowthMeasure.WEIGHT, c_last, weights[-1].value)
            _, c_first = self._age(baby, weights[0].ts.date())
            baseline_z, _, _ = self._z(baby, GrowthMeasure.WEIGHT, c_first, weights[0].value)

        return GrowthAssessment(
            baby=baby,
            corrected=is_preterm(baby.dob, baby.due_date),
            table_version=self._table.version if self._table else None,
            measures=tuple(measures),
            weight_loss_pct=loss_pct,
            regained_birth_weight=regained,
            latest_weight_z=latest_z,
            baseline_weight_z=baseline_z,
        )

    # ---------------------------------------------------------- chart series
    def centile_chart_series(self, measure: GrowthMeasure) -> ChartSeries | None:
        baby = self._baby_repo.get()
        if baby is None:
            return None

        events: list[GrowthEvent] = sorted(self._repo.list_growth(measure), key=lambda e: e.ts)

        cache_key: ChartCacheKey = (
            baby.baby_id,
            baby.sex.value,
            baby.dob,
            baby.due_date,
            tuple((e.event_id, e.ts, e.value) for e in events),
        )
        cached = self._chart_cache.get(measure)
        if cached is not None and cached[0] == cache_key:
            return cached[1]

        # Pre-formatted (not a date/datetime): the tuple is also JSON-serialised
        # verbatim by the /api/charts/{measure} route, whose plain json.dumps
        # can't encode date objects.
        trajectory = tuple(
            (self._age(baby, e.ts.date())[1], float(e.value), to_local(e.ts).strftime("%d %b %Y"))
            for e in events
        )

        if self._table is None:
            series = ChartSeries(
                measure=measure,
                unit=UNITS[measure],
                ages=(),
                curves={},
                trajectory=trajectory,
                frames=tuple(range(1, len(trajectory) + 1)),
                unavailable_reason=self._table_error or "reference data not installed",
            )
            self._chart_cache[measure] = (cache_key, series)
            return series

        try:
            lo, hi = self._table.age_range(measure, baby.sex)
        except ReferenceDataMissingError as exc:
            series = ChartSeries(
                measure=measure,
                unit=UNITS[measure],
                ages=(),
                curves={},
                trajectory=trajectory,
                frames=tuple(range(1, len(trajectory) + 1)),
                unavailable_reason=str(exc),
            )
            self._chart_cache[measure] = (cache_key, series)
            return series

        # Cover the plotted range, extended to include every logged point.
        if trajectory:
            hi = min(hi, max(int(max(a for a, _, _ in trajectory)) + 30, 60))
        else:
            hi = min(hi, 365)
        step = max(1, (hi - lo) // 120)
        ages = tuple(range(lo, hi + 1, step))

        curves: dict[str, tuple[float, ...]] = {}
        for centile in CENTILES:
            try:
                curves[f"{centile:g}"] = tuple(
                    self._table.value_at_centile(measure, baby.sex, a, centile) for a in ages
                )
            except (LookupError, ValueError):
                continue

        series = ChartSeries(
            measure=measure,
            unit=UNITS[measure],
            ages=ages,
            curves=curves,
            trajectory=trajectory,
            frames=tuple(range(1, len(trajectory) + 1)),
            unavailable_reason=None,
        )
        self._chart_cache[measure] = (cache_key, series)
        return series
