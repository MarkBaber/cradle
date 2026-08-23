"""Assemble facts, evaluate rules, de-duplicate, notify (tasks A1, N2)."""

import logging
import tomllib
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from cradle.alerts import FactSet, RuleSet, build_rules, evaluate
from cradle.models import AlertSeverity, Finding
from cradle.ports.clock import Clock
from cradle.ports.notifier import Notifier
from cradle.repos.alert_log_repo import AlertLogRepo
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.events_repo import EventsRepo
from cradle.services.growth_service import GrowthService

# How far back facts are gathered. Wide enough for the longest rule window
# (weigh-in cadence) without loading the whole history every five minutes.
FACT_WINDOW = timedelta(days=30)

log = logging.getLogger(__name__)


def load_config(path: Path) -> Mapping[str, object]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


class AlertsService:
    def __init__(
        self,
        repo: EventsRepo,
        baby_repo: BabyRepo,
        alert_log: AlertLogRepo,
        growth: GrowthService,
        notifier: Notifier,
        clock: Clock,
        config_path: Path,
    ) -> None:
        self._repo = repo
        self._baby_repo = baby_repo
        self._alert_log = alert_log
        self._growth = growth
        self._notifier = notifier
        self._clock = clock
        self._config_path = config_path

    def rules(self) -> RuleSet:
        return build_rules(load_config(self._config_path))

    def facts(self) -> FactSet | None:
        baby = self._baby_repo.get()
        if baby is None:
            return None
        now = self._clock.now()
        since = now - FACT_WINDOW
        assessment = self._growth.assessment()
        return FactSet(
            baby=baby,
            feeds=tuple(self._repo.list_feeds(limit=1000, since=since)),
            nappies=tuple(self._repo.list_nappies(limit=1000, since=since)),
            sleeps=tuple(self._repo.list_sleeps(limit=1000, since=since)),
            growth=tuple(self._repo.list_growth(limit=1000)),
            temperatures=tuple(self._repo.list_temperatures(limit=1000, since=since)),
            latest_weight_z=assessment.latest_weight_z if assessment else None,
            baseline_weight_z=assessment.baseline_weight_z if assessment else None,
            as_of=now,
        )

    @property
    def _auto_dismiss_window(self) -> timedelta:
        cfg = load_config(self._config_path)
        alerts_cfg = cfg.get("alerts", {})
        hours = 24.0
        if isinstance(alerts_cfg, Mapping):
            val = alerts_cfg.get("auto_dismiss_hours")
            if isinstance(val, int | float) and val > 0:
                hours = float(val)
        return timedelta(hours=hours)

    def auto_dismiss(self, older_than: datetime | None = None) -> int:
        now = self._clock.now()
        cutoff = older_than if older_than is not None else (now - self._auto_dismiss_window)
        return self._alert_log.auto_dismiss(cutoff, as_of=now)

    def sweep(self) -> int:
        """One evaluation pass. Returns the count of findings newly raised.

        Each finding is persisted BEFORE delivery is attempted, and delivery
        failures are contained per-finding: a dead network must not lose an
        alert, nor stop the remaining findings in the same sweep from being
        recorded. The count reflects findings raised, not pushes delivered.
        """
        self.auto_dismiss()
        facts = self.facts()
        if facts is None:
            return 0
        notified = 0
        for finding in evaluate(facts, self.rules()):
            if not self._alert_log.record_if_new(finding):
                continue
            notified += 1
            try:
                self._notifier.send(finding)
            except Exception:
                log.exception("notifier failed for %s; finding is still recorded", finding.rule_id)
        return notified

    def pinned(self) -> list[Finding]:
        """Red findings stay on screen until acknowledged (task U6)."""
        self.auto_dismiss()
        return self._alert_log.unacknowledged(AlertSeverity.RED)

    def outstanding(self) -> list[Finding]:
        self.auto_dismiss()
        return self._alert_log.unacknowledged()

    def acknowledge(self, fingerprint: str) -> bool:
        return self._alert_log.acknowledge(fingerprint, acknowledged_at=self._clock.now())

    def all_messages(self, severity: AlertSeverity | None = None) -> list[Finding]:
        self.auto_dismiss()
        return self._alert_log.all(severity=severity)
