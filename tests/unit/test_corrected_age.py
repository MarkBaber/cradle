"""R3: corrected age. A due date is 40 weeks; preterm is <37 completed weeks."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.reference.lms import (  # noqa: E402
    corrected_age_days,
    gestation_at_birth_days,
    is_preterm,
)

DOB = date(2026, 7, 1)


def _due(weeks_early: float) -> date:
    return DOB + timedelta(days=round(weeks_early * 7))


def test_gestation_derived_from_due_date() -> None:
    assert gestation_at_birth_days(DOB, _due(0)) == 280
    assert gestation_at_birth_days(DOB, _due(4)) == 280 - 28


def test_term_baby_is_not_corrected() -> None:
    on = DOB + timedelta(days=30)
    assert corrected_age_days(DOB, _due(0), on) == 30


def test_born_two_weeks_early_is_still_term() -> None:
    """38 weeks is term: no correction, despite due_date > dob."""
    assert not is_preterm(DOB, _due(2))
    assert corrected_age_days(DOB, _due(2), DOB + timedelta(days=30)) == 30


def test_37_week_boundary() -> None:
    assert not is_preterm(DOB, _due(3)), "exactly 37 weeks is term"
    assert is_preterm(DOB, _due(3) + timedelta(days=1)), "one day under 37w is preterm"


def test_preterm_offset_applied() -> None:
    due = _due(8)  # born at 32 weeks
    on = DOB + timedelta(days=100)
    assert is_preterm(DOB, due)
    assert corrected_age_days(DOB, due, on) == 100 - 56


def test_correction_never_negative() -> None:
    due = _due(8)
    on = DOB + timedelta(days=10)
    assert corrected_age_days(DOB, due, on) == 0


def test_correction_stops_at_two_years() -> None:
    due = _due(8)
    on = DOB + timedelta(days=365 * 2 + 1)
    assert corrected_age_days(DOB, due, on) == 365 * 2 + 1


def test_post_term_birth_not_corrected() -> None:
    """Born after the due date: chronological age stands."""
    due = DOB - timedelta(days=5)
    assert not is_preterm(DOB, due)
    assert corrected_age_days(DOB, due, DOB + timedelta(days=20)) == 20
