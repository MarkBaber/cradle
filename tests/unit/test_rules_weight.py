"""A5: weight rules and their boundaries."""

import datetime

import _facts as F


def test_weight_loss_fires_at_ten_percent() -> None:
    f = F.fire("WEIGHT_LOSS_10PC", F.facts(growths=(F.growth(0, 3059),)))
    assert f is not None
    assert f.severity.value == "red"
    assert "call 111" in f.message


def test_weight_loss_boundary() -> None:
    """3060g is exactly 90% of 3400g: at the line, not past it."""
    assert F.fire("WEIGHT_LOSS_10PC", F.facts(growths=(F.growth(0, 3060),))) is None
    assert F.fire("WEIGHT_LOSS_10PC", F.facts(growths=(F.growth(0, 3059),))) is not None


def test_weight_loss_uses_latest_measurement() -> None:
    recovering = (F.growth(5, 3000), F.growth(0, 3300))
    assert F.fire("WEIGHT_LOSS_10PC", F.facts(growths=recovering)) is None


def test_weight_loss_silent_without_measurements() -> None:
    assert F.fire("WEIGHT_LOSS_10PC", F.facts()) is None


def test_not_regained_fires_from_day_14() -> None:
    day14 = F.NOW.date() - datetime.timedelta(days=13)
    f = F.fire("WEIGHT_NOT_REGAINED",
               F.facts(dob=day14, growths=(F.growth(0, 3300),)))
    assert f is not None
    assert "day 14" in f.message


def test_not_regained_silent_before_day_14() -> None:
    day13 = F.NOW.date() - datetime.timedelta(days=12)
    facts = F.facts(dob=day13, growths=(F.growth(0, 3300),))
    assert F.fire("WEIGHT_NOT_REGAINED", facts) is None


def test_not_regained_silent_once_back_to_birth_weight() -> None:
    facts = F.facts(growths=(F.growth(0, 3400),))
    assert F.fire("WEIGHT_NOT_REGAINED", facts) is None


def test_centile_cross_fires_past_threshold() -> None:
    facts = F.facts(growths=(F.growth(0, 3300),),
                    baseline_weight_z=0.5, latest_weight_z=-0.9)
    f = F.fire("CENTILE_CROSS", facts)
    assert f is not None
    assert "1.40" in f.message


def test_centile_cross_boundary_at_1_33() -> None:
    below = F.facts(growths=(F.growth(0, 3300),),
                    baseline_weight_z=0.0, latest_weight_z=-1.32)
    at = F.facts(growths=(F.growth(0, 3300),),
                 baseline_weight_z=0.0, latest_weight_z=-1.33)
    assert F.fire("CENTILE_CROSS", below) is None
    assert F.fire("CENTILE_CROSS", at) is not None


def test_centile_cross_silent_without_reference_data() -> None:
    """R2 is blocked: no z pair means no claim, rather than a guess."""
    facts = F.facts(growths=(F.growth(0, 2000),))
    assert F.fire("CENTILE_CROSS", facts) is None
    half = F.facts(growths=(F.growth(0, 2000),), baseline_weight_z=1.0)
    assert F.fire("CENTILE_CROSS", half) is None


def test_centile_cross_ignores_upward_movement() -> None:
    facts = F.facts(growths=(F.growth(0, 3900),),
                    baseline_weight_z=-1.5, latest_weight_z=0.5)
    assert F.fire("CENTILE_CROSS", facts) is None


def test_weight_rules_ignore_other_measures() -> None:
    """A9: a length in mm must never be read as a weight in grams.

    550mm against a 3400g birth weight looks like an 84% loss, which fired a
    red "call 111" push at a baby who had just gained weight.
    """
    from cradle.models import GrowthMeasure

    healthy = F.growth(2, 3600)
    length = F.growth(1, 550, GrowthMeasure.LENGTH)
    head = F.growth(1, 350, GrowthMeasure.HEAD_CIRC)
    facts = F.facts(growths=(healthy, length, head))
    assert F.fire("WEIGHT_LOSS_10PC", facts) is None
    assert F.fire("WEIGHT_NOT_REGAINED", facts) is None


def test_weight_rules_still_fire_when_other_measures_are_present() -> None:
    """The filter must not silence a genuine loss logged alongside a length."""
    from cradle.models import GrowthMeasure

    facts = F.facts(growths=(F.growth(2, 3059),
                             F.growth(1, 550, GrowthMeasure.LENGTH)))
    assert F.fire("WEIGHT_LOSS_10PC", facts) is not None
