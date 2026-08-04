"""Deterministic evaluation: facts in, findings out (task A2)."""

from cradle.alerts.facts import FactSet
from cradle.alerts.rules import RuleSet
from cradle.models import Finding


def evaluate(facts: FactSet, rules: RuleSet) -> list[Finding]:
    """Run every rule over one snapshot.

    Pure and total: a failing predicate must not silence the rest of the rule
    set, so each is isolated. Ordering follows the rule set, so the same
    FactSet always yields the same list.
    """
    findings: list[Finding] = []
    for rule in rules:
        finding = rule.predicate(facts)
        if finding is not None:
            findings.append(finding)
    return findings
