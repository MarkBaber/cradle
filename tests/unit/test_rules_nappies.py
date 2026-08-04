"""A4: nappy rules, day-of-life table, stool colour flags."""

import _facts as F
from cradle.models import NappyKind, StoolColour


def test_wet_low_fires_on_day_15() -> None:
    f = F.fire("WET_NAPPY_LOW", F.facts(nappies=F.nappies_every(6, 3)))
    assert f is not None
    assert "day 15" in f.message


def test_wet_expectation_by_day_of_life() -> None:
    """NHS table: day 1 expects 1, day 4 expects 4, day 5+ expects 6."""
    cases = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 15: 6}
    for day, expected in cases.items():
        dob = F.NOW.date() - __import__("datetime").timedelta(days=day - 1)
        at_expected = F.facts(dob=dob, nappies=F.nappies_every(2, expected))
        below = F.facts(dob=dob, nappies=F.nappies_every(2, expected - 1))
        assert F.fire("WET_NAPPY_LOW", at_expected) is None, f"day {day}"
        assert F.fire("WET_NAPPY_LOW", below) is not None, f"day {day}"


def test_mixed_nappy_counts_as_wet() -> None:
    facts = F.facts(nappies=F.nappies_every(2, 6, NappyKind.MIXED))
    assert F.fire("WET_NAPPY_LOW", facts) is None


def test_stool_absent_fires_after_24h_in_window() -> None:
    facts = F.facts(nappies=(F.nappy(30, NappyKind.DIRTY),))
    f = F.fire("STOOL_ABSENT", facts)
    assert f is not None
    assert "30 hours" in f.message


def test_stool_absent_silent_before_day_three() -> None:
    import datetime

    dob = F.NOW.date() - datetime.timedelta(days=1)  # day 2
    facts = F.facts(dob=dob, nappies=(F.nappy(30, NappyKind.DIRTY),))
    assert F.fire("STOOL_ABSENT", facts) is None


def test_stool_absent_silent_within_24h() -> None:
    facts = F.facts(nappies=(F.nappy(20, NappyKind.DIRTY),))
    assert F.fire("STOOL_ABSENT", facts) is None


def test_red_stool_colours_fire() -> None:
    for colour in (StoolColour.PALE_CHALKY, StoolColour.RED):
        facts = F.facts(nappies=(F.nappy(1, NappyKind.DIRTY, colour),))
        f = F.fire("STOOL_COLOUR", facts)
        assert f is not None, colour
        assert f.severity.value == "red"
        assert "call 111" in f.message


def test_normal_colours_do_not_fire() -> None:
    for colour in (StoolColour.YELLOW, StoolColour.GREEN, StoolColour.BROWN,
                   StoolColour.MECONIUM):
        facts = F.facts(nappies=(F.nappy(1, NappyKind.DIRTY, colour),))
        assert F.fire("STOOL_COLOUR", facts) is None, colour


def test_black_stool_normal_early_flagged_later() -> None:
    """Meconium is black and normal in the first days; day 6 black is not."""
    import datetime

    day4 = F.NOW.date() - datetime.timedelta(days=3)
    day6 = F.NOW.date() - datetime.timedelta(days=5)
    early = F.facts(dob=day4, nappies=(F.nappy(1, NappyKind.DIRTY, StoolColour.BLACK),))
    late = F.facts(dob=day6, nappies=(F.nappy(1, NappyKind.DIRTY, StoolColour.BLACK),))
    assert F.fire("STOOL_COLOUR", early) is None
    assert F.fire("STOOL_COLOUR", late) is not None


def test_stool_colour_fingerprint_is_per_event() -> None:
    a = F.nappy(1, NappyKind.DIRTY, StoolColour.RED)
    b = F.nappy(2, NappyKind.DIRTY, StoolColour.RED)
    fa = F.fire("STOOL_COLOUR", F.facts(nappies=(a,)))
    fb = F.fire("STOOL_COLOUR", F.facts(nappies=(b,)))
    assert fa is not None and fb is not None
    assert fa.fingerprint != fb.fingerprint, "each nappy alerts on its own"
