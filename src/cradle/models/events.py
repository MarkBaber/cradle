"""Event and result records. All timestamps are timezone-aware UTC datetimes.

Signatures are API contracts (see CLAUDE.md); changing a field requires
architect sign-off recorded in the touching task's notes.
"""

from dataclasses import dataclass
from datetime import date, datetime

from cradle.models.enums import (
    AlertSeverity,
    FeedMethod,
    GrowthMeasure,
    NappyKind,
    Sex,
    StoolColour,
)


@dataclass(frozen=True, slots=True)
class Baby:
    baby_id: int
    name: str
    sex: Sex
    dob: date
    due_date: date  # gestational-age / corrected-age basis
    birth_weight_g: int


@dataclass(frozen=True, slots=True)
class _EventBase:
    event_id: int | None  # None until persisted
    baby_id: int
    ts: datetime
    logged_by: str


@dataclass(frozen=True, slots=True)
class FeedEvent(_EventBase):
    method: FeedMethod = FeedMethod.BREAST_LEFT
    duration_min: int | None = None  # breast feeds
    volume_ml: int | None = None  # bottle feeds
    note: str = ""


@dataclass(frozen=True, slots=True)
class NappyEvent(_EventBase):
    kind: NappyKind = NappyKind.WET
    stool_colour: StoolColour = StoolColour.UNSET


@dataclass(frozen=True, slots=True)
class SleepEvent(_EventBase):
    ts_end: datetime | None = None  # None while sleep is running
    location: str = "cot"


@dataclass(frozen=True, slots=True)
class GrowthEvent(_EventBase):
    measure: GrowthMeasure = GrowthMeasure.WEIGHT
    value: int = 0  # g or mm per measure
    source: str = "home"  # home | midwife


@dataclass(frozen=True, slots=True)
class TemperatureEvent(_EventBase):
    temp_c: float = 0.0
    site: str = "axilla"


@dataclass(frozen=True, slots=True)
class Milestone(_EventBase):
    category: str = "first"  # motor | social | communication | first
    title: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class Note(_EventBase):
    text: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ZResult:
    """Output of the UK-WHO LMS engine (SPEC 5.1)."""

    z: float
    centile: float
    corrected_age_days: int
    table_version: str


@dataclass(frozen=True, slots=True)
class Finding:
    """Output of the alerts engine (SPEC 5.2). fingerprint de-duplicates notifications."""

    rule_id: str
    severity: AlertSeverity
    message: str
    fingerprint: str
    ts: datetime
