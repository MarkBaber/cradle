"""MS1: milestone timeline, ages, and the no-scoring rule."""

from datetime import date, timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.repos.baby_repo import BabyRepo
from cradle.services.logging_service import LoggingService
from cradle.services.milestone_service import (
    TYPICAL_WINDOWS,
    MilestoneService,
    typical_window,
)

DOB = date(2026, 5, 1)


def _build(due: date = DOB):
    db = make_db(dob=DOB, due=due)
    repo = make_repo(db)
    return LoggingService(repo, clock()), MilestoneService(repo, BabyRepo(db))


def test_no_profile_returns_none() -> None:
    db = make_db(seed_baby=False)
    assert MilestoneService(make_repo(db), BabyRepo(db)).timeline() is None


def test_cards_newest_first_with_age() -> None:
    log, svc = _build()
    log.log_milestone("social", "First smile", ts=NOW - timedelta(days=10))
    log.log_milestone("motor", "Holds head up", ts=NOW)
    cards = svc.timeline()
    assert cards is not None
    assert [c.title for c in cards] == ["Holds head up", "First smile"]
    assert cards[0].age_days == (NOW.date() - DOB).days


def test_typical_window_attached_when_recognised() -> None:
    log, svc = _build()
    log.log_milestone("social", "First smile today!", ts=NOW)
    cards = svc.timeline()
    assert cards is not None
    assert cards[0].typical_weeks == TYPICAL_WINDOWS["first smile"]


def test_unrecognised_milestone_has_no_window() -> None:
    log, svc = _build()
    log.log_milestone("first", "Met the cat", ts=NOW)
    cards = svc.timeline()
    assert cards is not None
    assert cards[0].typical_weeks is None


def test_window_matching_is_case_insensitive() -> None:
    assert typical_window("FIRST STEPS") == TYPICAL_WINDOWS["first steps"]
    assert typical_window("  rolls over  ") == TYPICAL_WINDOWS["rolls over"]


def test_corrected_age_shown_for_preterm() -> None:
    log, svc = _build(due=DOB + timedelta(weeks=8))
    log.log_milestone("social", "First smile", ts=NOW)
    cards = svc.timeline()
    assert cards is not None
    assert svc.uses_corrected_age() is True
    assert cards[0].corrected_age_days == cards[0].age_days - 56


def test_term_baby_ages_match() -> None:
    log, svc = _build()
    log.log_milestone("social", "First smile", ts=NOW)
    cards = svc.timeline()
    assert cards is not None
    assert svc.uses_corrected_age() is False
    assert cards[0].corrected_age_days == cards[0].age_days


def test_windows_are_wide_and_never_scored() -> None:
    """A card carries context, never a pass/fail verdict."""
    for name, (lo, hi) in TYPICAL_WINDOWS.items():
        assert hi > lo, name
        assert hi - lo >= 6, f"{name} window too narrow to be non-judgemental"
    log, svc = _build()
    log.log_milestone("motor", "Rolls over", ts=NOW)
    card = svc.timeline()[0]
    assert not hasattr(card, "on_track")
    assert not hasattr(card, "delayed")


def test_deleted_milestones_absent() -> None:
    log, svc = _build()
    mid = log.log_milestone("first", "Oops", ts=NOW)
    log.undo("milestone", mid)
    assert svc.timeline() == ()
