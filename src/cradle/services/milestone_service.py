"""Milestone timeline (task MS1).

Typical-age windows are shown as context only. Nothing here scores a baby or
flags them as behind: development varies enormously and a tracking app is the
wrong place to imply otherwise (SPEC 1.1). Ages are corrected for prematurity,
matching how the growth charts present age.
"""

from dataclasses import dataclass
from datetime import datetime

from cradle.models import Baby
from cradle.reference.lms import corrected_age_days, is_preterm
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.events_repo import EventsRepo

CATEGORIES = ("first", "motor", "social", "communication")

# Illustrative windows in weeks, shown next to a logged milestone for interest.
# Wide by design; they are not thresholds and never drive an alert.
TYPICAL_WINDOWS: dict[str, tuple[int, int]] = {
    "first smile": (6, 12),
    "holds head up": (6, 16),
    "rolls over": (16, 28),
    "sits unaided": (24, 36),
    "first tooth": (16, 52),
    "crawls": (28, 48),
    "first word": (40, 64),
    "first steps": (36, 72),
}


@dataclass(frozen=True, slots=True)
class MilestoneCard:
    event_id: int
    ts: datetime
    category: str
    title: str
    note: str
    age_days: int
    corrected_age_days: int
    typical_weeks: tuple[int, int] | None


def typical_window(title: str) -> tuple[int, int] | None:
    key = title.strip().lower()
    for known, window in TYPICAL_WINDOWS.items():
        if known in key:
            return window
    return None


class MilestoneService:
    def __init__(self, repo: EventsRepo, baby_repo: BabyRepo) -> None:
        self._repo = repo
        self._baby_repo = baby_repo

    def timeline(self) -> tuple[MilestoneCard, ...] | None:
        baby: Baby | None = self._baby_repo.get()
        if baby is None:
            return None
        cards = []
        for m in self._repo.list_milestones(limit=500):
            if m.event_id is None:
                continue
            on = m.ts.date()
            cards.append(MilestoneCard(
                event_id=m.event_id, ts=m.ts, category=m.category, title=m.title,
                note=m.note, age_days=(on - baby.dob).days,
                corrected_age_days=corrected_age_days(baby.dob, baby.due_date, on),
                typical_weeks=typical_window(m.title),
            ))
        cards.sort(key=lambda c: c.ts, reverse=True)
        return tuple(cards)

    def uses_corrected_age(self) -> bool:
        baby = self._baby_repo.get()
        return baby is not None and is_preterm(baby.dob, baby.due_date)
