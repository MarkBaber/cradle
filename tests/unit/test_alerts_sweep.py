"""A1/N2: fact assembly, de-duplication, notification, acknowledgement."""

from datetime import timedelta
from pathlib import Path

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import AlertSeverity, FeedMethod, GrowthMeasure
from cradle.ports.notifier import ConsoleNotifier
from cradle.repos.alert_log_repo import AlertLogRepo
from cradle.repos.baby_repo import BabyRepo
from cradle.services.alerts_service import AlertsService
from cradle.services.growth_service import GrowthService
from cradle.services.logging_service import LoggingService

CONFIG = Path(__file__).resolve().parents[2] / "rules_config.toml"


def _build(seed_baby: bool = True):
    db = make_db(seed_baby=seed_baby, dob=NOW.date() - timedelta(days=14))
    repo = make_repo(db)
    baby_repo = BabyRepo(db)
    notifier = ConsoleNotifier()
    svc = AlertsService(
        repo,
        baby_repo,
        AlertLogRepo(db),
        GrowthService(repo, baby_repo, None, "no reference"),
        notifier,
        clock(),
        CONFIG,
    )
    return LoggingService(repo, clock()), svc, notifier


def test_sweep_without_profile_is_a_noop() -> None:
    _, svc, notifier = _build(seed_baby=False)
    assert svc.facts() is None
    assert svc.sweep() == 0
    assert notifier.sent == []


def test_sweep_notifies_once_per_fingerprint() -> None:
    log, svc, notifier = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(hours=9))
    first = svc.sweep()
    assert first > 0
    sent_after_first = len(notifier.sent)
    assert svc.sweep() == 0, "second sweep must not re-notify"
    assert len(notifier.sent) == sent_after_first


def test_new_condition_notifies_again() -> None:
    log, svc, notifier = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(hours=9))
    svc.sweep()
    before = len(notifier.sent)
    log.log_temperature(38.7, ts=NOW - timedelta(minutes=5))
    assert svc.sweep() >= 1
    assert len(notifier.sent) > before


def test_facts_carry_growth_z_when_available() -> None:
    log, svc, _ = _build()
    log.log_growth(GrowthMeasure.WEIGHT, 3300, ts=NOW)
    facts = svc.facts()
    assert facts is not None
    assert len(facts.growth) == 1
    assert facts.latest_weight_z is None, "no reference table: no z, no guess"


def test_findings_persist_even_if_notifier_fails() -> None:
    log, svc, _ = _build()

    class Broken:
        def send(self, finding):
            raise RuntimeError("no network")

    svc._notifier = Broken()
    log.log_temperature(39.0, ts=NOW - timedelta(minutes=1))
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(hours=9))

    raised = svc.sweep()  # must not propagate the delivery failure

    assert raised >= 2, "a dead notifier must not stop later findings being recorded"
    outstanding = {f.rule_id for f in svc.outstanding()}
    assert "FEVER_U3M" in outstanding
    assert "FEED_GAP" in outstanding


def test_red_findings_are_pinned_until_acknowledged() -> None:
    log, svc, _ = _build()
    log.log_temperature(39.0, ts=NOW - timedelta(minutes=1))
    svc.sweep()
    pinned = svc.pinned()
    assert pinned and all(f.severity is AlertSeverity.RED for f in pinned)

    assert svc.acknowledge(pinned[0].fingerprint) is True
    assert svc.acknowledge(pinned[0].fingerprint) is False, "acknowledging is idempotent"
    assert all(f.fingerprint != pinned[0].fingerprint for f in svc.pinned())


def test_acknowledging_does_not_resurface_on_next_sweep() -> None:
    log, svc, _ = _build()
    log.log_temperature(39.0, ts=NOW - timedelta(minutes=1))
    svc.sweep()
    svc.acknowledge(svc.pinned()[0].fingerprint)
    svc.sweep()
    assert svc.pinned() == []
