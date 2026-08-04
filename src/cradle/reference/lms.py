"""UK-WHO LMS growth reference (tasks R1, R3).

    z = ((x/M)^L - 1) / (L*S)      L != 0
    z = ln(x/M) / S                L == 0

and the inverse, used to draw centile curves:

    x = M * (1 + L*S*z)^(1/L)      L != 0
    x = M * exp(S*z)               L == 0

L, M and S are linearly interpolated between the two bracketing age rows.

This module is pure: models + stdlib only, no I/O beyond reading the vendored
table on first use. If the table is absent the loader raises
ReferenceDataMissingError — it never falls back to approximations, because a
wrong centile here is worse than no centile at all.
"""

import csv
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from math import exp, log
from pathlib import Path
from statistics import NormalDist

from cradle.models import GrowthMeasure, ReferenceDataMissingError, Sex, ZResult

DATA_DIR = Path(__file__).parent / "data"
TABLE_PATH = DATA_DIR / "ukwho_lms.csv"
VERSION_PATH = Path(__file__).parent / "VERSION"

TERM_GESTATION_DAYS = 40 * 7      # a due date is, by definition, 40 weeks
PRETERM_THRESHOLD_DAYS = 37 * 7   # born before this gestation => correct age (D5)
CORRECTION_UNTIL_DAYS = 365 * 2   # RCPCH: correct until 2 years


def gestation_at_birth_days(dob: date, due_date: date) -> int:
    """40 weeks minus however early (or late) the birth was."""
    return TERM_GESTATION_DAYS - (due_date - dob).days

_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class LmsRow:
    age_days: int
    L: float
    M: float
    S: float


class LmsTable:
    """Immutable LMS lookup. Construct directly in tests with synthetic rows."""

    def __init__(self, rows: dict[tuple[str, str], list[LmsRow]], version: str) -> None:
        self._rows = {k: sorted(v, key=lambda r: r.age_days) for k, v in rows.items()}
        self._ages = {k: [r.age_days for r in v] for k, v in self._rows.items()}
        self.version = version

    def keys(self) -> list[tuple[str, str]]:
        return sorted(self._rows)

    def age_range(self, measure: GrowthMeasure, sex: Sex) -> tuple[int, int]:
        rows = self._series(measure, sex)
        return rows[0].age_days, rows[-1].age_days

    def _series(self, measure: GrowthMeasure, sex: Sex) -> list[LmsRow]:
        rows = self._rows.get((measure.value, sex.value))
        if not rows:
            raise ReferenceDataMissingError(
                f"no LMS rows for {measure.value}/{sex.value} in table {self.version}"
            )
        return rows

    def lms(self, measure: GrowthMeasure, sex: Sex, age_days: int) -> LmsRow:
        """Interpolate L, M, S at age_days. Raises LookupError outside the table."""
        rows = self._series(measure, sex)
        ages = self._ages[(measure.value, sex.value)]
        if age_days < ages[0] or age_days > ages[-1]:
            raise LookupError(
                f"age {age_days}d outside {measure.value}/{sex.value} table "
                f"[{ages[0]}, {ages[-1]}]"
            )
        i = bisect_left(ages, age_days)
        if ages[i] == age_days:
            return rows[i]
        lo, hi = rows[i - 1], rows[i]
        span = hi.age_days - lo.age_days
        t = (age_days - lo.age_days) / span
        return LmsRow(
            age_days=age_days,
            L=lo.L + t * (hi.L - lo.L),
            M=lo.M + t * (hi.M - lo.M),
            S=lo.S + t * (hi.S - lo.S),
        )

    def zscore(
        self,
        measure: GrowthMeasure,
        sex: Sex,
        age_days: int,
        value: float,
        *,
        corrected: bool = False,
    ) -> ZResult:
        # `corrected` is metadata: callers pass an already-corrected age_days.
        _ = corrected
        if value <= 0:
            raise ValueError("measurement must be positive")
        row = self.lms(measure, sex, age_days)
        if row.L == 0:
            z = log(value / row.M) / row.S
        else:
            z = ((value / row.M) ** row.L - 1) / (row.L * row.S)
        return ZResult(
            z=z,
            centile=_NORMAL.cdf(z) * 100.0,
            corrected_age_days=age_days,
            table_version=self.version,
        )

    def value_at_centile(
        self,
        measure: GrowthMeasure,
        sex: Sex,
        age_days: int,
        centile: float,
    ) -> float:
        """Inverse of zscore: the measurement sitting on `centile` at this age."""
        return self.value_at_z(measure, sex, age_days, z_for_centile(centile))

    def value_at_z(
        self,
        measure: GrowthMeasure,
        sex: Sex,
        age_days: int,
        z: float,
    ) -> float:
        row = self.lms(measure, sex, age_days)
        if row.L == 0:
            return row.M * exp(row.S * z)
        base = 1 + row.L * row.S * z
        if base <= 0:
            raise ValueError("centile outside the representable range for this age")
        return row.M * base ** (1 / row.L)


def z_for_centile(centile: float) -> float:
    if not 0 < centile < 100:
        raise ValueError("centile must be strictly between 0 and 100")
    return _NORMAL.inv_cdf(centile / 100.0)


# --------------------------------------------------------------- table loading

def load_table(path: Path = TABLE_PATH, version_path: Path = VERSION_PATH) -> LmsTable:
    """Parse the vendored CSV. Raises ReferenceDataMissingError if unusable.

    Deliberately strict: an empty or malformed table must stop growth features
    dead rather than yield a plausible-looking wrong centile (task R2).
    """
    if not path.exists():
        raise ReferenceDataMissingError(
            f"LMS table not found at {path}. Run task R2 to vendor UK-WHO reference data."
        )
    rows: dict[tuple[str, str], list[LmsRow]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for record in csv.DictReader(fh):
            if not record or not (record.get("measure") or "").strip():
                continue
            if record["measure"].lstrip().startswith("#"):
                continue
            try:
                key = (record["measure"].strip(), record["sex"].strip())
                rows.setdefault(key, []).append(LmsRow(
                    age_days=int(record["age_days"]),
                    L=float(record["L"]), M=float(record["M"]), S=float(record["S"]),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise ReferenceDataMissingError(f"malformed LMS row {record!r}") from exc
    if not rows:
        raise ReferenceDataMissingError(
            f"LMS table at {path} contains no data rows. Task R2 is outstanding: "
            "UK-WHO reference data must be vendored from the RCPCH/WHO source files."
        )
    for key, series in rows.items():
        ages = sorted(r.age_days for r in series)
        if len(set(ages)) != len(ages):
            raise ReferenceDataMissingError(f"duplicate ages for {key}")
        if len(ages) < 2:
            raise ReferenceDataMissingError(f"{key} needs at least two age rows")
    version = (
        version_path.read_text(encoding="utf-8").strip().splitlines()[0]
        if version_path.exists() else "unknown"
    )
    return LmsTable(rows, version)


_cached: LmsTable | None = None


def default_table() -> LmsTable:
    global _cached
    if _cached is None:
        _cached = load_table()
    return _cached


def reset_cache() -> None:
    """Test hook: drop the memoised table."""
    global _cached
    _cached = None


# ------------------------------------------------------------------ public API

def zscore(
    measure: GrowthMeasure,
    sex: Sex,
    age_days: int,
    value: float,
    *,
    corrected: bool = False,
) -> ZResult:
    """SPEC 5.1 contract. Delegates to the vendored default table."""
    return default_table().zscore(measure, sex, age_days, value, corrected=corrected)


def corrected_age_days(dob: date, due_date: date, on: date) -> int:
    """Corrected age in days (task R3, decision D5).

    Chronological age minus the prematurity offset (due_date - dob), applied
    only when the baby was born before 37 completed weeks and only until the
    corrected age reaches two years. Never returns a negative age.
    """
    chronological = (on - dob).days
    if not is_preterm(dob, due_date) or chronological > CORRECTION_UNTIL_DAYS:
        return chronological
    return max(0, chronological - (due_date - dob).days)


def is_preterm(dob: date, due_date: date) -> bool:
    """Born before 37 completed weeks."""
    return gestation_at_birth_days(dob, due_date) < PRETERM_THRESHOLD_DAYS
