"""Synthetic-timeline builder for rule tests (A3-A6)."""

import sys
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cradle.alerts.facts import FactSet  # noqa: E402
from cradle.models import (  # noqa: E402
    Baby,
    FeedEvent,
    FeedMethod,
    GrowthEvent,
    GrowthMeasure,
    NappyEvent,
    NappyKind,
    Sex,
    SleepEvent,
    StoolColour,
    TemperatureEvent,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
DOB = date(2026, 7, 1)          # NOW is day 15 of life
CONFIG = tomllib.loads((ROOT / "rules_config.toml").read_text(encoding="utf-8"))

_next_id = iter(range(1, 100000))


def baby(dob: date = DOB, birth_weight_g: int = 3400) -> Baby:
    return Baby(baby_id=1, name="Test", sex=Sex.FEMALE, dob=dob,
                due_date=dob, birth_weight_g=birth_weight_g)


def _base(ts: datetime) -> dict[str, object]:
    return {"event_id": next(_next_id), "baby_id": 1, "ts": ts, "logged_by": "test"}


def feed(hours_ago: float, now: datetime = NOW) -> FeedEvent:
    return FeedEvent(method=FeedMethod.BREAST_LEFT,
                     **_base(now - timedelta(hours=hours_ago)))


def feeds_every(hours: float, count: int, now: datetime = NOW) -> tuple[FeedEvent, ...]:
    return tuple(feed(hours * i, now) for i in range(count))


def nappy(hours_ago: float, kind: NappyKind = NappyKind.WET,
          colour: StoolColour = StoolColour.UNSET,
          now: datetime = NOW) -> NappyEvent:
    return NappyEvent(kind=kind, stool_colour=colour,
                      **_base(now - timedelta(hours=hours_ago)))


def nappies_every(hours: float, count: int, kind: NappyKind = NappyKind.WET,
                  now: datetime = NOW) -> tuple[NappyEvent, ...]:
    return tuple(nappy(hours * i, kind, now=now) for i in range(count))


def growth(days_ago: float, value: int,
           measure: GrowthMeasure = GrowthMeasure.WEIGHT,
           now: datetime = NOW) -> GrowthEvent:
    return GrowthEvent(measure=measure, value=value,
                       **_base(now - timedelta(days=days_ago)))


def temperature(hours_ago: float, temp_c: float, now: datetime = NOW) -> TemperatureEvent:
    return TemperatureEvent(temp_c=temp_c, **_base(now - timedelta(hours=hours_ago)))


def facts(
    *,
    now: datetime = NOW,
    dob: date = DOB,
    birth_weight_g: int = 3400,
    feeds: tuple[FeedEvent, ...] = (),
    nappies: tuple[NappyEvent, ...] = (),
    sleeps: tuple[SleepEvent, ...] = (),
    growths: tuple[GrowthEvent, ...] = (),
    temperatures: tuple[TemperatureEvent, ...] = (),
    latest_weight_z: float | None = None,
    baseline_weight_z: float | None = None,
) -> FactSet:
    return FactSet(
        baby=baby(dob, birth_weight_g), feeds=feeds, nappies=nappies, sleeps=sleeps,
        growth=growths, temperatures=temperatures, latest_weight_z=latest_weight_z,
        baseline_weight_z=baseline_weight_z, as_of=now,
    )


def fire(rule_id: str, fact_set: FactSet, config: dict[str, object] | None = None):
    """Run one rule by id; return its Finding or None."""
    from cradle.alerts import build_rules  # noqa: PLC0415

    for rule in build_rules(config if config is not None else CONFIG):
        if rule.rule_id == rule_id:
            return rule.predicate(fact_set)
    raise AssertionError(f"no such rule: {rule_id}")


def healthy_baseline(now: datetime = NOW) -> dict[str, object]:
    """A timeline where nothing should fire: frequent feeds and nappies."""
    return {
        "now": now,
        "feeds": feeds_every(2, 12, now),
        "nappies": (*nappies_every(3, 8, NappyKind.WET, now),
                    *nappies_every(8, 3, NappyKind.DIRTY, now)),
        "growths": (growth(1, 3500, now=now),),
        "temperatures": (temperature(1, 36.8, now),),
    }
