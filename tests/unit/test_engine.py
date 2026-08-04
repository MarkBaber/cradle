"""A2: determinism, isolation, and copy integrity."""

import _facts as F

from cradle.alerts import build_rules, evaluate
from cradle.alerts.messages import MESSAGES, SOURCES, URGENT_RULES


def test_same_facts_yield_identical_findings() -> None:
    facts = F.facts(feeds=(F.feed(9),), nappies=F.nappies_every(6, 2))
    rules = build_rules(F.CONFIG)
    first = evaluate(facts, rules)
    second = evaluate(facts, rules)
    assert [f.fingerprint for f in first] == [f.fingerprint for f in second]
    assert [f.message for f in first] == [f.message for f in second]


def test_multiple_rules_can_fire_together() -> None:
    facts = F.facts(
        feeds=(F.feed(9),), nappies=F.nappies_every(6, 2), temperatures=(F.temperature(1, 38.6),)
    )
    fired = {f.rule_id for f in evaluate(facts, build_rules(F.CONFIG))}
    assert {"FEED_GAP", "WET_NAPPY_LOW", "FEVER_U3M"} <= fired


def test_fingerprints_unique_within_a_sweep() -> None:
    facts = F.facts(feeds=(F.feed(9),), nappies=F.nappies_every(6, 2))
    prints = [f.fingerprint for f in evaluate(facts, build_rules(F.CONFIG))]
    assert len(prints) == len(set(prints))


def test_every_rule_has_copy_and_a_source() -> None:
    ids = {r.rule_id for r in build_rules(F.CONFIG)}
    assert ids == set(MESSAGES) == set(SOURCES)


def test_urgent_rules_carry_urgent_wording() -> None:
    for rule_id in URGENT_RULES:
        assert "seek medical advice now" in MESSAGES[rule_id], rule_id


def test_no_rule_message_diagnoses() -> None:
    """Copy describes the log, never the baby's condition (SPEC 1.1)."""
    banned = ("your baby is", "diagnos", "dehydrat", "failure to thrive", "malnour")
    for rule_id, text in MESSAGES.items():
        lowered = text.lower()
        for phrase in banned:
            assert phrase not in lowered, f"{rule_id} contains {phrase!r}"


def test_severity_matches_declared_rule_severity() -> None:
    facts = F.facts(feeds=(F.feed(9),), temperatures=(F.temperature(1, 39.0),))
    by_id = {r.rule_id: r.severity for r in build_rules(F.CONFIG)}
    for finding in evaluate(facts, build_rules(F.CONFIG)):
        assert finding.severity == by_id[finding.rule_id]
