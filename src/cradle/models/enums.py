"""Closed vocabularies. Values are stored in SQLite as their .value strings."""

from enum import StrEnum


class Sex(StrEnum):
    MALE = "male"
    FEMALE = "female"


class FeedMethod(StrEnum):
    BREAST_LEFT = "breast_left"
    BREAST_RIGHT = "breast_right"
    BOTTLE_EXPRESSED = "bottle_expressed"
    BOTTLE_FORMULA = "bottle_formula"


class NappyKind(StrEnum):
    WET = "wet"
    DIRTY = "dirty"
    MIXED = "mixed"


class StoolColour(StrEnum):
    """Amber-flag colours per NHS guidance; UNSET for wet-only nappies."""

    UNSET = "unset"
    YELLOW = "yellow"
    GREEN = "green"
    BROWN = "brown"
    MECONIUM = "meconium"  # normal days 1-2
    PALE_CHALKY = "pale_chalky"  # red flag
    RED = "red"  # red flag
    BLACK = "black"  # red flag after day 5


class StoolConsistency(StrEnum):
    """How the stool looked, in the plain words a parent would use to a midwife.

    Descriptive only. No alert rule reads this field and none should be added
    against it here: a consistency that becomes an alert condition needs
    messages.py copy and therefore goes through A8. UNSET for wet-only nappies
    and for anything logged without a description.
    """

    UNSET = "unset"
    STICKY = "sticky"  # tarry and hard to wipe off, as meconium is
    SEEDY = "seedy"  # loose with seed-like specks through it
    SOFT = "soft"  # pasty, holds its shape only loosely
    FORMED = "formed"  # firm and shaped
    RUNNY = "runny"  # watery, soaks into the nappy
    HARD = "hard"  # dry pellets or small hard lumps
    MUCOUSY = "mucousy"  # visible slime or stringy threads


class SleepLocation(StrEnum):
    COT = "cot"
    PRAM = "pram"
    ARMS = "arms"
    OTHER = "other"


class GrowthMeasure(StrEnum):
    WEIGHT = "weight"  # grams
    LENGTH = "length"  # millimetres
    HEAD_CIRC = "head_circ"  # millimetres


class AlertSeverity(StrEnum):
    INFO = "info"
    REMINDER = "reminder"
    AMBER = "amber"
    RED = "red"


class BreastSide(StrEnum):
    """Which breast a pumping session drew from (task M1)."""

    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"


class MilkStore(StrEnum):
    """Where a bottle physically sits. The storage clock (A11) runs per store."""

    FRIDGE = "fridge"
    FREEZER = "freezer"
    ROOM = "room"


class BottleColour(StrEnum):
    """The bottle's own colour, which is how a parent identifies it in the kitchen.

    Always named in text alongside any colour chip: colour is never the sole
    carrier of meaning.
    """

    BLUE = "blue"
    GREEN = "green"
    RED = "red"
    YELLOW = "yellow"
    ORANGE = "orange"
    PURPLE = "purple"
    PINK = "pink"
    WHITE = "white"


class BatchState(StrEnum):
    """Lifecycle of one bottle. Stored, not derived: a batch can be discarded
    for reasons no timestamp shows (dropped, left out, refused)."""

    STORED = "stored"
    THAWED = "thawed"
    OPENED = "opened"
    USED = "used"
    DISCARDED = "discarded"


#: States in which a bottle still physically exists and holds milk. One colour
#: may have at most one live batch; migration 0003 enforces it with a partial
#: unique index over exactly these values.
LIVE_BATCH_STATES: frozenset[BatchState] = frozenset(
    {BatchState.STORED, BatchState.THAWED, BatchState.OPENED}
)
