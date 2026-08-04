"""Pure rules engine (SPEC 5.2). Imports models + stdlib only. No I/O, no clock."""

from cradle.alerts.engine import evaluate
from cradle.alerts.facts import FactSet
from cradle.alerts.rules import Rule, RuleSet, build_rules

__all__ = ["FactSet", "Rule", "RuleSet", "build_rules", "evaluate"]
