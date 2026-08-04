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
    MECONIUM = "meconium"        # normal days 1-2
    PALE_CHALKY = "pale_chalky"  # red flag
    RED = "red"                  # red flag
    BLACK = "black"              # red flag after day 5


class SleepLocation(StrEnum):
    COT = "cot"
    PRAM = "pram"
    ARMS = "arms"
    OTHER = "other"


class GrowthMeasure(StrEnum):
    WEIGHT = "weight"        # grams
    LENGTH = "length"        # millimetres
    HEAD_CIRC = "head_circ"  # millimetres


class AlertSeverity(StrEnum):
    INFO = "info"
    REMINDER = "reminder"
    AMBER = "amber"
    RED = "red"
