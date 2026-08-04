"""FactSet: the immutable snapshot the engine evaluates.

Assembled by services.alerts_service from repos; the engine itself never
touches the DB or wall clock (D6).
"""

from dataclasses import dataclass
from datetime import datetime

from cradle.models import (
    Baby,
    FeedEvent,
    GrowthEvent,
    NappyEvent,
    SleepEvent,
    TemperatureEvent,
)


@dataclass(frozen=True, slots=True)
class FactSet:
    baby: Baby
    feeds: tuple[FeedEvent, ...]
    nappies: tuple[NappyEvent, ...]
    sleeps: tuple[SleepEvent, ...]
    growth: tuple[GrowthEvent, ...]
    temperatures: tuple[TemperatureEvent, ...]
    latest_weight_z: float | None  # supplied by growth service for CENTILE_CROSS
    baseline_weight_z: float | None
    as_of: datetime  # snapshot time; the engine's only "now"
