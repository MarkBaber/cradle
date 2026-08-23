"""Event and result records. All timestamps are timezone-aware UTC datetimes.

Signatures are API contracts (see CLAUDE.md); changing a field requires
architect sign-off recorded in the touching task's notes.
"""

from dataclasses import dataclass
from datetime import date, datetime

from cradle.models.enums import (
    ActivityCategory,
    AlertSeverity,
    BatchState,
    BottleColour,
    BreastSide,
    FeedMethod,
    GrowthMeasure,
    MilkStore,
    NappyKind,
    Sex,
    StoolColour,
    StoolConsistency,
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
    consistency: StoolConsistency = StoolConsistency.UNSET


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
class ExpressionEvent(_EventBase):
    """One pumping session. ts is when it started; volume and duration are
    post-hoc edits (U10/T1) so expressing stays a two-tap log."""

    side: BreastSide = BreastSide.BOTH
    volume_ml: int | None = None
    duration_min: int | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class MilkBatch:
    """One physical bottle of expressed milk.

    Not an event: it has a lifecycle rather than a single ts. `stored_at` is
    deliberately separate from `expressed_at` - the storage clock the expiry
    rules (A11) run on starts when the bottle goes in to cool, not when it was
    expressed at the cot side an hour earlier. `state` is stored rather than
    derived from the timestamps because a bottle can be discarded for reasons
    no timestamp shows.
    """

    batch_id: int | None  # None until persisted
    baby_id: int
    expressed_at: datetime
    stored_at: datetime
    store: MilkStore
    colour: BottleColour
    volume_ml: int
    state: BatchState = BatchState.STORED
    logged_by: str = ""
    thawed_at: datetime | None = None
    opened_at: datetime | None = None
    used_at: datetime | None = None
    expression_id: int | None = None  # the session it was poured from, if known


@dataclass(frozen=True, slots=True)
class Note(_EventBase):
    text: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActivityEvent(_EventBase):
    """One developmental-activity session (task M2), measured in minutes.

    ts is when it started; duration_min is a post-hoc edit like ExpressionEvent's,
    so starting tummy time stays a two-tap log. Best-practice guidance per
    category is display copy in rules_config.toml's [activity_targets], not an
    alert condition - nothing here is scored or gated.
    """

    category: ActivityCategory = ActivityCategory.TUMMY_TIME
    duration_min: int | None = None
    note: str = ""


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
    acknowledged_at: datetime | None = None
