"""A6: fever, weigh-in reminder, measurement gap."""

import datetime

import _facts as F


def test_fever_boundary_at_38() -> None:
    assert F.fire("FEVER_U3M", F.facts(temperatures=(F.temperature(1, 37.9),))) is None
    f = F.fire("FEVER_U3M", F.facts(temperatures=(F.temperature(1, 38.0),)))
    assert f is not None
    assert f.severity.value == "red"


def test_fever_age_boundary_at_90_days() -> None:
    day90 = F.NOW.date() - datetime.timedelta(days=90)
    day89 = F.NOW.date() - datetime.timedelta(days=89)
    hot = (F.temperature(1, 38.5),)
    assert F.fire("FEVER_U3M", F.facts(dob=day89, temperatures=hot)) is not None
    assert F.fire("FEVER_U3M", F.facts(dob=day90, temperatures=hot)) is None


def test_fever_uses_latest_reading() -> None:
    readings = (F.temperature(6, 38.9), F.temperature(1, 36.9))
    assert F.fire("FEVER_U3M", F.facts(temperatures=readings)) is None


def test_weigh_in_due_after_14_days() -> None:
    f = F.fire("WEIGH_IN_DUE", F.facts(growths=(F.growth(15, 3500),)))
    assert f is not None
    assert "15 days" in f.message


def test_weigh_in_not_due_within_cadence() -> None:
    assert F.fire("WEIGH_IN_DUE", F.facts(growths=(F.growth(13, 3500),))) is None


def test_weigh_in_falls_back_to_birth_when_never_weighed() -> None:
    """Day 15 with no weight ever recorded should prompt."""
    assert F.fire("WEIGH_IN_DUE", F.facts()) is not None


def test_weigh_in_stops_after_six_months() -> None:
    old = F.NOW.date() - datetime.timedelta(days=200)
    assert F.fire("WEIGH_IN_DUE", F.facts(dob=old, growths=(F.growth(30, 7000),))) is None


def test_measurement_gap_fires_after_12h() -> None:
    f = F.fire("MEASUREMENT_GAP", F.facts(feeds=(F.feed(13),)))
    assert f is not None
    assert f.severity.value == "info"
    assert "about the log, not the baby" in f.message


def test_measurement_gap_considers_every_domain() -> None:
    """A recent nappy counts as activity even when feeds are stale."""
    facts = F.facts(feeds=(F.feed(20),), nappies=(F.nappy(2),))
    assert F.fire("MEASUREMENT_GAP", facts) is None


def test_healthy_timeline_fires_nothing() -> None:
    """The regression that matters most: a well-logged day must be silent."""
    from cradle.alerts import build_rules, evaluate

    facts = F.facts(**F.healthy_baseline())
    findings = evaluate(facts, build_rules(F.CONFIG))
    assert findings == [], [f.rule_id for f in findings]


def test_weigh_in_due_is_not_satisfied_by_a_length_measurement() -> None:
    """A9: only a weight resets the weigh-in cadence."""
    from cradle.models import GrowthMeasure

    facts = F.facts(growths=(F.growth(15, 3500),
                             F.growth(1, 550, GrowthMeasure.LENGTH)))
    assert F.fire("WEIGH_IN_DUE", facts) is not None
