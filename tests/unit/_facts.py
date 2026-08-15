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
    Milestone,
    NappyEvent,
    NappyKind,
    Note,
    Sex,
    SleepEvent,
    StoolColour,
    TemperatureEvent,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
DOB = date(2026, 7, 1)  # NOW is day 15 of life
CONFIG = tomllib.loads((ROOT / "rules_config.toml").read_text(encoding="utf-8"))

_next_id = iter(range(1, 100000))


def baby(dob: date = DOB, birth_weight_g: int = 3400) -> Baby:
    return Baby(
        baby_id=1, name="Test", sex=Sex.FEMALE, dob=dob, due_date=dob, birth_weight_g=birth_weight_g
    )


def _base(ts: datetime) -> dict[str, object]:
    return {"event_id": next(_next_id), "baby_id": 1, "ts": ts, "logged_by": "test"}


def feed(hours_ago: float, now: datetime = NOW) -> FeedEvent:
    return FeedEvent(method=FeedMethod.BREAST_LEFT, **_base(now - timedelta(hours=hours_ago)))


def feeds_every(hours: float, count: int, now: datetime = NOW) -> tuple[FeedEvent, ...]:
    return tuple(feed(hours * i, now) for i in range(count))


def nappy(
    hours_ago: float,
    kind: NappyKind = NappyKind.WET,
    colour: StoolColour = StoolColour.UNSET,
    now: datetime = NOW,
) -> NappyEvent:
    return NappyEvent(kind=kind, stool_colour=colour, **_base(now - timedelta(hours=hours_ago)))


def nappies_every(
    hours: float,
    count: int,
    kind: NappyKind = NappyKind.WET,
    colour: StoolColour = StoolColour.UNSET,
    now: datetime = NOW,
) -> tuple[NappyEvent, ...]:
    return tuple(nappy(hours * i, kind, colour, now) for i in range(count))


def growth(
    days_ago: float, value: int, measure: GrowthMeasure = GrowthMeasure.WEIGHT, now: datetime = NOW
) -> GrowthEvent:
    return GrowthEvent(measure=measure, value=value, **_base(now - timedelta(days=days_ago)))


def temperature(hours_ago: float, temp_c: float, now: datetime = NOW) -> TemperatureEvent:
    return TemperatureEvent(temp_c=temp_c, **_base(now - timedelta(hours=hours_ago)))


def sleep(hours_ago: float, duration_hours: float | None, now: datetime = NOW) -> SleepEvent:
    """A sleep started `hours_ago`; still running (no ts_end) when duration is None."""
    start = now - timedelta(hours=hours_ago)
    ts_end = start + timedelta(hours=duration_hours) if duration_hours is not None else None
    return SleepEvent(ts_end=ts_end, **_base(start))


def milestone(
    hours_ago: float, category: str = "motor", title: str = "held head up", now: datetime = NOW
) -> Milestone:
    return Milestone(category=category, title=title, **_base(now - timedelta(hours=hours_ago)))


def note(
    hours_ago: float, text: str = "settled quickly after every feed today", now: datetime = NOW
) -> Note:
    return Note(text=text, **_base(now - timedelta(hours=hours_ago)))


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
    milestones: tuple[Milestone, ...] = (),
    notes: tuple[Note, ...] = (),
    latest_weight_z: float | None = None,
    baseline_weight_z: float | None = None,
) -> FactSet:
    # FactSet has no milestone/note fields - the rule engine only ever reads the
    # five domains below, so these two are accepted but dropped here. That lets
    # a fixture log a genuinely complete day (A9 regression guard) without every
    # caller needing to know milestones/notes are inert to alerting.
    del milestones, notes
    return FactSet(
        baby=baby(dob, birth_weight_g),
        feeds=feeds,
        nappies=nappies,
        sleeps=sleeps,
        growth=growths,
        temperatures=temperatures,
        latest_weight_z=latest_weight_z,
        baseline_weight_z=baseline_weight_z,
        as_of=now,
    )


def fire(rule_id: str, fact_set: FactSet, config: dict[str, object] | None = None):
    """Run one rule by id; return its Finding or None."""
    from cradle.alerts import build_rules  # noqa: PLC0415

    for rule in build_rules(config if config is not None else CONFIG):
        if rule.rule_id == rule_id:
            return rule.predicate(fact_set)
    raise AssertionError(f"no such rule: {rule_id}")


def healthy_baseline(now: datetime = NOW) -> dict[str, object]:
    """A genuinely well-logged day across every domain: nothing should fire.

    Covers all seven logged domains at once (A9 regression guard): weight,
    length and head circumference together, a completed sleep and one still
    running, a dirty nappy with a normal (non-flag) colour, a milestone and a
    note - so a test can no longer accidentally exercise only one measure at
    a time.
    """
    return {
        "now": now,
        "feeds": feeds_every(2, 12, now),
        "nappies": (
            *nappies_every(3, 8, NappyKind.WET, now=now),
            *nappies_every(8, 3, NappyKind.DIRTY, StoolColour.BROWN, now),
        ),
        "sleeps": (
            sleep(4, 1.5, now),
            sleep(0.5, None, now),
        ),
        "growths": (
            growth(1, 3500, now=now),
            growth(2, 520, GrowthMeasure.LENGTH, now),
            growth(2, 370, GrowthMeasure.HEAD_CIRC, now),
        ),
        "temperatures": (temperature(1, 36.8, now),),
        "milestones": (milestone(6, now=now),),
        "notes": (note(5, now=now),),
    }


def _override(now: datetime = NOW, **changes: object) -> dict[str, object]:
    base = healthy_baseline(now)
    base.update(changes)
    return base


def one_abnormality(rule_id: str) -> dict[str, object]:
    """Healthy in every domain but one: only `rule_id` should fire.

    Pairs with healthy_baseline() to guard against rules interfering with
    each other (A9): each branch changes the single input a rule cares about
    and leaves every other domain in its healthy state.
    """
    if rule_id == "FEED_GAP":
        return _override(feeds=tuple(feed(5 + 2 * i) for i in range(12)))
    if rule_id == "FEED_COUNT_LOW":
        return _override(feeds=tuple(feed(4 * i) for i in range(5)))
    if rule_id == "WET_NAPPY_LOW":
        return _override(
            nappies=(
                *nappies_every(8, 3, NappyKind.WET),
                *nappies_every(8, 3, NappyKind.DIRTY, StoolColour.BROWN),
            )
        )
    if rule_id == "STOOL_ABSENT":
        return _override(
            nappies=(
                *nappies_every(3, 8, NappyKind.WET),
                nappy(25, NappyKind.DIRTY, StoolColour.BROWN),
                nappy(33, NappyKind.DIRTY, StoolColour.BROWN),
                nappy(41, NappyKind.DIRTY, StoolColour.BROWN),
            )
        )
    if rule_id == "STOOL_COLOUR":
        base = healthy_baseline()
        base["nappies"] = (*base["nappies"], nappy(0.5, NappyKind.DIRTY, StoolColour.RED))
        return base
    if rule_id == "WEIGHT_LOSS_10PC":
        return _override(
            dob=NOW.date() - timedelta(days=9),  # day 10: below regain_by_day, isolates the loss
            growths=(
                growth(1, 3000),
                growth(2, 520, GrowthMeasure.LENGTH),
                growth(2, 370, GrowthMeasure.HEAD_CIRC),
            ),
        )
    if rule_id == "WEIGHT_NOT_REGAINED":
        return _override(
            growths=(
                growth(1, 3300),  # below birth weight but not a 10% loss
                growth(2, 520, GrowthMeasure.LENGTH),
                growth(2, 370, GrowthMeasure.HEAD_CIRC),
            )
        )
    if rule_id == "CENTILE_CROSS":
        return _override(latest_weight_z=-0.5, baseline_weight_z=1.0)
    if rule_id == "FEVER_U3M":
        return _override(temperatures=(temperature(1, 38.5),))
    if rule_id == "WEIGH_IN_DUE":
        return _override(
            growths=(
                growth(2, 520, GrowthMeasure.LENGTH),
                growth(2, 370, GrowthMeasure.HEAD_CIRC),
            )
        )
    if rule_id == "MEASUREMENT_GAP":
        gap_now = NOW + timedelta(days=20)  # past every rule's max_age_days gate but one
        return {
            "now": gap_now,
            "feeds": (),
            "nappies": tuple(nappy(13 + 2 * i, NappyKind.WET, now=gap_now) for i in range(6)),
            "sleeps": (),
            "growths": (growth(13 / 24, 3500, now=gap_now),),
            "temperatures": (),
        }
    raise AssertionError(f"no one_abnormality fixture for rule: {rule_id}")
