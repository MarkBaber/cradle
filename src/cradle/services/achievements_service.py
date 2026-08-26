"""Achievement/badge evaluation (task U42).

Two badge families share one catalog+award shape: rule-based (predefined or
custom - COUNT/STREAK/SINGLE, auto-evaluated) and the baby "moment" family
(MOMENT, keyed off Milestone category='first', SPEC 5.1/5.4), plus MANUAL
(no rule - the user taps a mark-earned control themselves). Evaluation is
synchronous, called from routers/api.py right after each
svc.logging.log_*()/log_milestone() call succeeds (this task's notes: the
same choke point U2's undo-toast is already assembled at) - never a
scheduler sweep, which is what keeps this feeling like a reward rather than
another thing being monitored.

Deliberately does NOT reuse alerts/ (rules.py/engine.py/messages.py/
alert_log): a positive-reinforcement badge is not a health signal, and this
domain keeps its own catalog/award tables and dedup entirely here, only
borrowing ports.notifier.Notifier.send() (existing signature, not changed)
to reuse the ntfy transport for the celebratory push.

Repeat-increment celebration default (task notes, implementer's judgement
call, documented as required): a repeatable achievement celebrates
(animation + push) on its first unlock only; further qualifying events
increment the award's count silently, unless the achievement's own
celebrate_every names count tiers to celebrate again at. This avoids
notification fatigue on a frequently-repeating condition while still
surfacing genuinely new unlocks.
"""

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cradle.models import (
    AchievementAward,
    AchievementDef,
    AchievementSource,
    AlertSeverity,
    Finding,
    Rarity,
    RuleType,
    UnlockEvent,
)
from cradle.ports.clock import Clock
from cradle.ports.notifier import Notifier
from cradle.repos.badges_repo import BadgesRepo
from cradle.repos.events_repo import EventsRepo

BABY_ID = 1  # single-baby v1 (D11), matches LoggingService's own constant

# EventsRepo.list_*'s limit caps a page, not "give me everything" - there is
# no unbounded variant, and events_repo.py is outside this task's touches
# (CLAUDE.md: "need to change another file? append a new task"). A single
# household's whole history never approaches this many rows of one domain,
# so it is effectively unbounded for this purpose.
_ALL = 1_000_000


class _HasTs(Protocol):
    """Read-only (a property, not a plain attribute) so every frozen event
    dataclass structurally satisfies it - a plain `ts: datetime` attribute
    protocol also demands settability, which frozen dataclasses reject."""

    @property
    def ts(self) -> datetime: ...

# Starter catalog (task U42 exit criteria): first use of each quick-entry
# domain, plus the baby "moment" family. Mark Baber's fuller predefined-copy
# list is a deferred fast-follow (task notes) - this stays a flat tuple of
# data so appending it later is a pure data change, not a code change.
PREDEFINED_CATALOG: tuple[AchievementDef, ...] = (
    AchievementDef(
        key="engagement.first_feed",
        name="First Feed Logged",
        description="Logged your very first feed.",
        rarity=Rarity.COMMON,
        rule_type=RuleType.COUNT,
        domain="feed",
        threshold=1,
        icon="🍼",
    ),
    AchievementDef(
        key="engagement.first_nappy",
        name="First Nappy Logged",
        description="Logged your very first nappy change.",
        rarity=Rarity.COMMON,
        rule_type=RuleType.COUNT,
        domain="nappy",
        threshold=1,
        icon="💧",
    ),
    AchievementDef(
        key="engagement.first_sleep",
        name="First Sleep Logged",
        description="Logged your very first sleep.",
        rarity=Rarity.COMMON,
        rule_type=RuleType.COUNT,
        domain="sleep",
        threshold=1,
        icon="☾",
    ),
    AchievementDef(
        key="engagement.first_growth",
        name="First Growth Measurement",
        description="Logged your very first growth measurement.",
        rarity=Rarity.COMMON,
        rule_type=RuleType.COUNT,
        domain="growth",
        threshold=1,
        icon="📏",
    ),
    AchievementDef(
        key="engagement.first_temperature",
        name="First Temperature Reading",
        description="Logged your very first temperature reading.",
        rarity=Rarity.COMMON,
        rule_type=RuleType.COUNT,
        domain="temperature",
        threshold=1,
        icon="🌡",
    ),
    AchievementDef(
        key="engagement.first_activity",
        name="First Activity Logged",
        description="Logged your very first developmental activity.",
        rarity=Rarity.COMMON,
        rule_type=RuleType.COUNT,
        domain="activity",
        threshold=1,
        icon="🧸",
    ),
    AchievementDef(
        key="moment.captured",
        name="Moment Captured",
        description="Captured a first-time moment - one of the baby's own milestones.",
        rarity=Rarity.RARE,
        rule_type=RuleType.MOMENT,
        domain="milestone",
        repeatable=True,
        icon="⭐",
    ),
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One /achievements tile's full view model (task U42)."""

    definition: AchievementDef
    earned: bool
    count: int
    first_awarded_at: datetime | None
    last_awarded_at: datetime | None
    progress_current: int | None  # only set for a locked COUNT-rule tile
    progress_target: int | None


class UnknownAchievementError(ValueError):
    """mark_earned referenced a badge key that isn't a MANUAL entry."""


class DuplicateAchievementKeyError(ValueError):
    """A custom achievement's name slugs to a key that already exists.

    Translates badges_repo.insert_custom's sqlite3.IntegrityError into a
    domain-specific exception at the service boundary, the same convention
    milk_service/journal_service already use (UnknownBatchError,
    UnsupportedPhotoTypeError, ...) rather than letting a router catch a raw
    sqlite3 exception two layers below it."""


def _slug_key(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return f"custom.{slug or 'achievement'}"


class AchievementsService:
    def __init__(
        self,
        repo: EventsRepo,
        badges: BadgesRepo,
        notifier: Notifier,
        clock: Clock,
    ) -> None:
        self._repo = repo
        self._badges = badges
        self._notifier = notifier
        self._clock = clock
        self._badges.seed_predefined(PREDEFINED_CATALOG)

    # ------------------------------------------------------------- catalog
    def catalog(self) -> list[CatalogEntry]:
        defs = self._badges.list_definitions()
        awards = self._badges.list_awards(BABY_ID)
        return [self._entry(d, awards.get(d.key)) for d in defs]

    def completion(self) -> tuple[int, int]:
        entries = self.catalog()
        return sum(1 for e in entries if e.earned), len(entries)

    def _entry(self, d: AchievementDef, award: AchievementAward | None) -> CatalogEntry:
        progress_current = progress_target = None
        if award is None and d.rule_type == RuleType.COUNT:
            progress_current = min(self._domain_count(d.domain), d.threshold)
            progress_target = d.threshold
        return CatalogEntry(
            definition=d,
            earned=award is not None,
            count=award.count if award else 0,
            first_awarded_at=award.first_awarded_at if award else None,
            last_awarded_at=award.last_awarded_at if award else None,
            progress_current=progress_current,
            progress_target=progress_target,
        )

    # ----------------------------------------------------------- authoring
    def create_custom_rule(
        self,
        name: str,
        description: str,
        icon: str,
        rarity: Rarity,
        rule_type: RuleType,
        domain: str = "",
        field: str = "",
        match_value: str = "",
        threshold: int = 1,
        repeatable: bool = False,
    ) -> AchievementDef:
        if rule_type not in (RuleType.COUNT, RuleType.STREAK, RuleType.SINGLE):
            raise ValueError(f"not a rule-based type: {rule_type}")
        d = AchievementDef(
            key=_slug_key(name),
            name=name,
            description=description,
            rarity=rarity,
            rule_type=rule_type,
            domain=domain,
            field=field,
            match_value=match_value,
            threshold=max(1, threshold),
            repeatable=repeatable,
            icon=icon or "🏆",
            source=AchievementSource.CUSTOM,
        )
        self._insert_custom(d)
        return d

    def create_custom_manual(
        self,
        name: str,
        description: str,
        icon: str,
        rarity: Rarity,
        repeatable: bool = False,
    ) -> AchievementDef:
        d = AchievementDef(
            key=_slug_key(name),
            name=name,
            description=description,
            rarity=rarity,
            rule_type=RuleType.MANUAL,
            repeatable=repeatable,
            icon=icon or "🏆",
            source=AchievementSource.CUSTOM,
        )
        self._insert_custom(d)
        return d

    def _insert_custom(self, d: AchievementDef) -> None:
        try:
            self._badges.insert_custom(d)
        except sqlite3.IntegrityError as exc:
            raise DuplicateAchievementKeyError(d.key) from exc

    def mark_earned(self, key: str) -> UnlockEvent:
        d = self._badges.get_definition(key)
        if d is None or d.rule_type != RuleType.MANUAL:
            raise UnknownAchievementError(key)
        existing = self._badges.get_award(BABY_ID, key)
        if existing is not None and not d.repeatable:
            # one-shot: already earned, further taps are no-ops (same
            # invariant _maybe_award enforces for the rule-based paths).
            return self._snapshot(d, existing, newly_unlocked=False, celebrate=False)
        return self._award(d, increment=1)

    # ---------------------------------------------------------- evaluation
    def evaluate_event(
        self, domain: str, field_values: Mapping[str, str] | None = None
    ) -> tuple[UnlockEvent, ...]:
        """Called from routers/api.py right after a log_*() call succeeds."""
        results: list[UnlockEvent] = []
        for d in self._badges.list_definitions():
            if d.domain == domain and self._rule_qualifies(d, domain, field_values or {}):
                results.extend(self._maybe_award(d))
        return tuple(results)

    def _rule_qualifies(
        self, d: AchievementDef, domain: str, field_values: Mapping[str, str]
    ) -> bool:
        if d.rule_type == RuleType.COUNT:
            return self._domain_count(domain) >= d.threshold
        if d.rule_type == RuleType.SINGLE:
            return field_values.get(d.field) == d.match_value
        if d.rule_type == RuleType.STREAK:
            return self._current_streak_days(domain) >= d.threshold
        return False

    def evaluate_milestone(self, category: str) -> tuple[UnlockEvent, ...]:
        """Called from routers/api.py right after log_milestone() succeeds,
        only the "moment" family fires - category must be 'first' (SPEC 5.4;
        the app cannot enumerate every possible first in advance, so this
        matches any new category='first' Milestone rather than a fixed
        title list)."""
        if category != "first":
            return ()
        results: list[UnlockEvent] = []
        for d in self._badges.list_definitions():
            if d.rule_type == RuleType.MOMENT:
                results.extend(self._maybe_award(d))
        return tuple(results)

    # --------------------------------------------------------------- award
    def _maybe_award(self, d: AchievementDef) -> tuple[UnlockEvent, ...]:
        existing = self._badges.get_award(BABY_ID, d.key)
        if existing is not None and not d.repeatable:
            return ()  # one-shot: already awarded, further events are no-ops
        return (self._award(d, increment=1),)

    def _award(self, d: AchievementDef, increment: int) -> UnlockEvent:
        existing = self._badges.get_award(BABY_ID, d.key)
        newly_unlocked = existing is None
        award = self._badges.record_award(BABY_ID, d.key, self._clock.now(), increment)
        celebrate = newly_unlocked or award.count in d.celebrate_every
        event = self._snapshot(d, award, newly_unlocked=newly_unlocked, celebrate=celebrate)
        if celebrate:
            self._notify(event)
        return event

    @staticmethod
    def _snapshot(
        d: AchievementDef, award: AchievementAward, *, newly_unlocked: bool, celebrate: bool
    ) -> UnlockEvent:
        return UnlockEvent(
            key=d.key,
            name=d.name,
            message=f"Achievement unlocked: {d.name} - {d.description}",
            icon=d.icon,
            rarity=d.rarity,
            count=award.count,
            newly_unlocked=newly_unlocked,
            celebrate=celebrate,
        )

    def _notify(self, event: UnlockEvent) -> None:
        finding = Finding(
            rule_id="achievement",
            severity=AlertSeverity.INFO,
            message=event.message,
            fingerprint=f"achievement:{event.key}:{event.count}",
            ts=self._clock.now(),
        )
        self._notifier.send(finding)

    # -------------------------------------------------------------- facts
    def _domain_events(self, domain: str) -> Sequence[_HasTs]:
        """One page-sized (_ALL) fetch per quick-entry domain. Direct
        per-return-statement typing, rather than a dict of lambdas, so mypy's
        structural Protocol check runs against each concrete event dataclass
        individually instead of trying to unify them through one Callable
        value type."""
        if domain == "feed":
            return self._repo.list_feeds(limit=_ALL)
        if domain == "nappy":
            return self._repo.list_nappies(limit=_ALL)
        if domain == "sleep":
            return self._repo.list_sleeps(limit=_ALL)
        if domain == "growth":
            return self._repo.list_growth(limit=_ALL)
        if domain == "temperature":
            return self._repo.list_temperatures(limit=_ALL)
        if domain == "activity":
            return self._repo.list_activities(limit=_ALL)
        return ()

    def _domain_count(self, domain: str) -> int:
        return len(self._domain_events(domain))

    def _current_streak_days(self, domain: str) -> int:
        dates = sorted({ev.ts.date() for ev in self._domain_events(domain)}, reverse=True)
        if not dates:
            return 0
        streak = 1
        for i in range(1, len(dates)):
            if (dates[i - 1] - dates[i]).days == 1:
                streak += 1
            else:
                break
        return streak
