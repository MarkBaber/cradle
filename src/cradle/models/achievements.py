"""Achievement/badge domain models (task U42).

Two badge families share one catalog+award shape: parent/engagement badges
(first use of each quick-entry domain, and other gentle usage milestones)
and baby "moment" badges (each new category='first' Milestone, SPEC 5.1/5.4).
Custom authoring adds a third source, hybrid rule-based or manual.

Rarity is an author-assigned static tier - there is no accounts/population
data to compute rarity from (SPEC: single baby, no auth, LAN-only) - so it
is a fixed field set when the achievement is defined, never derived. Catalog
entries are plain data (this module's dataclass +
services/achievements_service.py's PREDEFINED_CATALOG), not one branch per
badge, so appending Mark Baber's fuller predefined-copy list later (a
deferred fast-follow, task notes) is a pure data change.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Rarity(StrEnum):
    COMMON = "common"
    RARE = "rare"
    EPIC = "epic"
    LEGENDARY = "legendary"


class RuleType(StrEnum):
    """How a catalog entry is evaluated.

    COUNT/STREAK/SINGLE are rule-based (auto-evaluated, predefined or
    custom); MOMENT is the baby "first" family, keyed off Milestone
    category='first' rather than a quick-entry domain; MANUAL has no rule at
    all - the user taps a mark-earned control themselves.
    """

    COUNT = "count"  # a domain's all-time event count reaches threshold
    STREAK = "streak"  # threshold consecutive days with >=1 domain event
    SINGLE = "single"  # one logged event whose field equals match_value
    MOMENT = "moment"  # a new category='first' Milestone
    MANUAL = "manual"  # no rule; the user marks it earned themselves


class Source(StrEnum):
    PREDEFINED = "predefined"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class AchievementDef:
    """One catalog entry (task U42). Data, not code: rule params are plain
    fields so the catalog stays a flat table, the same latitude U31/U34/U36's
    wheel catalogs already had.
    """

    key: str
    name: str
    description: str
    rarity: Rarity
    rule_type: RuleType
    domain: str = ""  # feed|nappy|sleep|growth|temperature|activity; unused by MOMENT/MANUAL
    field: str = ""  # SINGLE only: the event field to match
    match_value: str = ""  # SINGLE only: the value that field must equal
    threshold: int = 1  # COUNT: events needed; STREAK: consecutive days needed
    repeatable: bool = False
    icon: str = "🏆"
    source: Source = Source.PREDEFINED
    celebrate_every: tuple[int, ...] = ()  # named count tiers that re-celebrate (task notes)


@dataclass(frozen=True, slots=True)
class AchievementAward:
    """One row per (baby_id, badge_key) - additive-only (task U42): count
    only ever increments, never decrements; nothing is ever deleted or
    marked overdue/expired."""

    baby_id: int
    badge_key: str
    count: int
    first_awarded_at: datetime
    last_awarded_at: datetime


@dataclass(frozen=True, slots=True)
class UnlockEvent:
    """What one evaluate_*/mark_earned call produced, for the caller
    (routers/api.py) to render the celebratory animation and decide whether
    to push (task U42)."""

    key: str
    name: str
    message: str
    icon: str
    rarity: Rarity
    count: int
    newly_unlocked: bool
    celebrate: bool
