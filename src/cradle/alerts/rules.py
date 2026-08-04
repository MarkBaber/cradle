"""Rule definitions (tasks A3-A6).

Every rule is a pure predicate over a FactSet. Thresholds arrive as a plain
mapping parsed from rules_config.toml by the service layer - this module does
no file I/O and reads no clock, so a rule's behaviour is fully determined by
its inputs (D6).

Ages used here are CHRONOLOGICAL, not corrected: the NHS day-of-life feeding
and nappy expectations are counted from birth. Corrected age applies to growth
centiles only (D5).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from cradle.alerts import messages
from cradle.alerts.facts import FactSet
from cradle.models import (
    AlertSeverity,
    Finding,
    GrowthEvent,
    GrowthMeasure,
    NappyKind,
    StoolColour,
)

RED_STOOL_COLOURS = frozenset(
    {
        StoolColour.PALE_CHALKY,
        StoolColour.RED,
        StoolColour.BLACK,
    }
)


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    severity: AlertSeverity
    predicate: Callable[[FactSet], Finding | None]


RuleSet = tuple[Rule, ...]


def _cfg(config: Mapping[str, object], section: str) -> Mapping[str, object]:
    value = config.get(section, {})
    return value if isinstance(value, Mapping) else {}


def _num(section: Mapping[str, object], key: str, default: float) -> float:
    value = section.get(key, default)
    return float(value) if isinstance(value, int | float) else default


def _age_days(facts: FactSet) -> int:
    return (facts.as_of.date() - facts.baby.dob).days


def _day_of_life(facts: FactSet) -> int:
    return _age_days(facts) + 1


def _weights(facts: FactSet) -> list[GrowthEvent]:
    """Weight measurements only, oldest first.

    `facts.growth` carries every measure. Length and head circumference are
    stored in millimetres, so a 550mm length read as a weight looks like a
    catastrophic loss against a 3400g birth weight — a false red escalation.
    Weight rules must never see anything but weights (task A9).
    """
    return sorted(
        (g for g in facts.growth if g.measure is GrowthMeasure.WEIGHT),
        key=lambda g: g.ts,
    )


def _finding(
    rule: str, severity: AlertSeverity, bucket: str, ts: datetime, **fields: object
) -> Finding:
    return Finding(
        rule_id=rule,
        severity=severity,
        message=messages.render(rule, **fields),
        fingerprint=f"{rule}:{bucket}",
        ts=ts,
    )


# --------------------------------------------------------------------- feeds


def _feed_gap(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    cfg = _cfg(config, "feed_gap")
    max_gap = timedelta(hours=_num(cfg, "max_gap_hours", 4.0))
    max_age = int(_num(cfg, "max_age_days", 28))

    def predicate(facts: FactSet) -> Finding | None:
        if _age_days(facts) >= max_age or not facts.feeds:
            return None
        last = max(f.ts for f in facts.feeds)
        gap = facts.as_of - last
        if gap <= max_gap:
            return None
        # Bucketed on the gap's start, so one episode raises one alert.
        return _finding(
            "FEED_GAP",
            AlertSeverity.REMINDER,
            last.isoformat(timespec="hours"),
            facts.as_of,
            hours=gap.total_seconds() / 3600,
            last=last.strftime("%H:%M"),
        )

    return predicate


def _feed_count_low(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    cfg = _cfg(config, "feed_count_low")
    minimum = int(_num(cfg, "min_feeds_24h", 8))
    max_age = int(_num(cfg, "max_age_days", 28))

    def predicate(facts: FactSet) -> Finding | None:
        if _age_days(facts) >= max_age:
            return None
        window = facts.as_of - timedelta(hours=24)
        count = sum(1 for f in facts.feeds if f.ts >= window)
        if count >= minimum:
            return None
        return _finding(
            "FEED_COUNT_LOW",
            AlertSeverity.AMBER,
            facts.as_of.date().isoformat(),
            facts.as_of,
            count=count,
            expected=minimum,
        )

    return predicate


# -------------------------------------------------------------------- nappies


def _wet_expected(config: Mapping[str, object], day_of_life: int) -> int | None:
    table = _cfg(config, "wet_nappy_low").get("by_day_of_life", {})
    if not isinstance(table, Mapping):
        return None
    key = str(day_of_life) if day_of_life <= 4 else "5plus"
    value = table.get(key)
    return int(value) if isinstance(value, int) else None


def _wet_nappy_low(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    def predicate(facts: FactSet) -> Finding | None:
        day = _day_of_life(facts)
        expected = _wet_expected(config, day)
        if expected is None:
            return None
        window = facts.as_of - timedelta(hours=24)
        count = sum(
            1
            for n in facts.nappies
            if n.ts >= window and n.kind in (NappyKind.WET, NappyKind.MIXED)
        )
        if count >= expected:
            return None
        return _finding(
            "WET_NAPPY_LOW",
            AlertSeverity.AMBER,
            facts.as_of.date().isoformat(),
            facts.as_of,
            count=count,
            expected=expected,
            day=day,
        )

    return predicate


def _stool_absent(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    cfg = _cfg(config, "stool_absent")
    max_gap = timedelta(hours=_num(cfg, "max_gap_hours", 24))
    from_day = int(_num(cfg, "from_day", 3))
    to_day = int(_num(cfg, "to_day", 28))

    def predicate(facts: FactSet) -> Finding | None:
        day = _day_of_life(facts)
        if not from_day <= day <= to_day:
            return None
        dirty = [n.ts for n in facts.nappies if n.kind in (NappyKind.DIRTY, NappyKind.MIXED)]
        if not dirty:
            return None
        last = max(dirty)
        gap = facts.as_of - last
        if gap <= max_gap:
            return None
        return _finding(
            "STOOL_ABSENT",
            AlertSeverity.AMBER,
            last.isoformat(timespec="hours"),
            facts.as_of,
            hours=gap.total_seconds() / 3600,
            day=day,
        )

    return predicate


def _stool_colour(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    black_until = int(_num(_cfg(config, "stool_colour"), "black_normal_until_day", 5))

    def predicate(facts: FactSet) -> Finding | None:
        for nappy in sorted(facts.nappies, key=lambda n: n.ts, reverse=True):
            if nappy.stool_colour not in RED_STOOL_COLOURS:
                continue
            day = (nappy.ts.date() - facts.baby.dob).days + 1
            if nappy.stool_colour is StoolColour.BLACK and day <= black_until:
                continue  # meconium is normal in the first days
            return _finding(
                "STOOL_COLOUR",
                AlertSeverity.RED,
                str(nappy.event_id),
                nappy.ts,
                colour=nappy.stool_colour.value.replace("_", " "),
            )
        return None

    return predicate


# -------------------------------------------------------------------- weight


def _weight_loss(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    fraction = _num(_cfg(config, "weight"), "loss_red_fraction", 0.10)

    def predicate(facts: FactSet) -> Finding | None:
        weights = _weights(facts)
        birth = facts.baby.birth_weight_g
        if not weights or birth <= 0:
            return None
        latest = weights[-1]
        if latest.value >= birth * (1 - fraction):
            return None
        return _finding(
            "WEIGHT_LOSS_10PC",
            AlertSeverity.RED,
            str(latest.event_id),
            latest.ts,
            weight=latest.value,
            birth=birth,
            pct=(birth - latest.value) / birth * 100.0,
        )

    return predicate


def _weight_not_regained(
    config: Mapping[str, object],
) -> Callable[[FactSet], Finding | None]:
    by_day = int(_num(_cfg(config, "weight"), "regain_by_day", 14))

    def predicate(facts: FactSet) -> Finding | None:
        day = _day_of_life(facts)
        if day < by_day:
            return None
        weights = _weights(facts)
        if not weights or weights[-1].value >= facts.baby.birth_weight_g:
            return None
        return _finding(
            "WEIGHT_NOT_REGAINED",
            AlertSeverity.AMBER,
            facts.as_of.date().isoformat(),
            facts.as_of,
            birth=facts.baby.birth_weight_g,
            day=day,
            weight=weights[-1].value,
        )

    return predicate


def _centile_cross(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    threshold = _num(_cfg(config, "weight"), "centile_cross_z", 1.33)

    def predicate(facts: FactSet) -> Finding | None:
        # Silent without a reference table: no z pair, no claim (R2 blocked).
        if facts.latest_weight_z is None or facts.baseline_weight_z is None:
            return None
        drop = facts.baseline_weight_z - facts.latest_weight_z
        if drop < threshold:
            return None
        weights = _weights(facts)
        bucket = str(weights[-1].event_id) if weights else facts.as_of.date().isoformat()
        return _finding(
            "CENTILE_CROSS",
            AlertSeverity.AMBER,
            bucket,
            facts.as_of,
            drop=drop,
        )

    return predicate


# ------------------------------------------------------- fever and reminders


def _fever(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    cfg = _cfg(config, "fever_u3m")
    threshold = _num(cfg, "temp_c", 38.0)
    max_age = int(_num(cfg, "max_age_days", 90))

    def predicate(facts: FactSet) -> Finding | None:
        if _age_days(facts) >= max_age or not facts.temperatures:
            return None
        latest = max(facts.temperatures, key=lambda t: t.ts)
        if latest.temp_c < threshold:
            return None
        return _finding(
            "FEVER_U3M",
            AlertSeverity.RED,
            str(latest.event_id),
            latest.ts,
            temp=latest.temp_c,
            age=_age_days(facts),
        )

    return predicate


def _weigh_in_due(config: Mapping[str, object]) -> Callable[[FactSet], Finding | None]:
    cfg = _cfg(config, "weigh_in_due")
    max_gap = int(_num(cfg, "max_gap_days", 14))
    max_age = int(_num(cfg, "max_age_days", 180))

    def predicate(facts: FactSet) -> Finding | None:
        if _age_days(facts) >= max_age:
            return None
        weights = _weights(facts)
        last = weights[-1].ts if weights else None
        reference = last if last is not None else _birth_instant(facts)
        days = (facts.as_of - reference).days
        if days < max_gap:
            return None
        return _finding(
            "WEIGH_IN_DUE",
            AlertSeverity.REMINDER,
            facts.as_of.date().isoformat(),
            facts.as_of,
            days=days,
        )

    return predicate


def _birth_instant(facts: FactSet) -> datetime:
    return datetime.combine(facts.baby.dob, facts.as_of.timetz())


def _measurement_gap(
    config: Mapping[str, object],
) -> Callable[[FactSet], Finding | None]:
    max_gap = timedelta(hours=_num(_cfg(config, "measurement_gap"), "max_gap_hours", 12))

    def predicate(facts: FactSet) -> Finding | None:
        stamps = [
            *(f.ts for f in facts.feeds),
            *(n.ts for n in facts.nappies),
            *(s.ts for s in facts.sleeps),
            *(g.ts for g in facts.growth),
            *(t.ts for t in facts.temperatures),
        ]
        if not stamps:
            return None
        last = max(stamps)
        gap = facts.as_of - last
        if gap <= max_gap:
            return None
        return _finding(
            "MEASUREMENT_GAP",
            AlertSeverity.INFO,
            last.isoformat(timespec="hours"),
            facts.as_of,
            hours=gap.total_seconds() / 3600,
        )

    return predicate


_BUILDERS: tuple[tuple[str, AlertSeverity, object], ...] = (
    ("FEED_GAP", AlertSeverity.REMINDER, _feed_gap),
    ("FEED_COUNT_LOW", AlertSeverity.AMBER, _feed_count_low),
    ("WET_NAPPY_LOW", AlertSeverity.AMBER, _wet_nappy_low),
    ("STOOL_ABSENT", AlertSeverity.AMBER, _stool_absent),
    ("STOOL_COLOUR", AlertSeverity.RED, _stool_colour),
    ("WEIGHT_LOSS_10PC", AlertSeverity.RED, _weight_loss),
    ("WEIGHT_NOT_REGAINED", AlertSeverity.AMBER, _weight_not_regained),
    ("CENTILE_CROSS", AlertSeverity.AMBER, _centile_cross),
    ("FEVER_U3M", AlertSeverity.RED, _fever),
    ("WEIGH_IN_DUE", AlertSeverity.REMINDER, _weigh_in_due),
    ("MEASUREMENT_GAP", AlertSeverity.INFO, _measurement_gap),
)


def build_rules(config: Mapping[str, object]) -> RuleSet:
    """Build the v1 rule set from an already-parsed rules_config mapping.

    Architect note (A3): SPEC 5.2 named this `load_rules(config_path)`. Changed
    to take a mapping so the alerts layer performs no file I/O and its purity
    claim is real rather than nominal. The service layer owns the file read.
    """
    return tuple(
        Rule(rule_id=rule_id, severity=severity, predicate=builder(config))  # type: ignore[operator]
        for rule_id, severity, builder in _BUILDERS
    )
