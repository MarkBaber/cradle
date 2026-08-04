"""Service bundle handed to routers by the composition root (app.py)."""

from dataclasses import dataclass

from cradle.services import (
    AlertsService,
    ExportService,
    GrowthService,
    HistoryService,
    LoggingService,
    MilestoneService,
    SeriesService,
    SettingsService,
    TodayService,
)


@dataclass(frozen=True, slots=True)
class Services:
    logging: LoggingService
    today: TodayService
    history: HistoryService
    settings: SettingsService
    growth: GrowthService
    alerts: AlertsService
    milestones: MilestoneService
    export: ExportService
    series: SeriesService


def device_name(cookie: str | None) -> str:
    """Attribution only (D7): no auth, just who tapped the button."""
    return (cookie or "").strip()[:40]
