"""Service bundle handed to routers by the composition root (app.py)."""

from dataclasses import dataclass
from urllib.parse import unquote

from cradle.services import (
    AlertsService,
    ExportService,
    GrowthService,
    HistoryService,
    LoggingService,
    MilestoneService,
    MilkStockService,
    ProjectionService,
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
    milk: MilkStockService
    projections: ProjectionService


def device_name(cookie: str | None) -> str:
    """Attribution only (D7): no auth, just who tapped the button.

    Set-Cookie is latin-1 on the wire, so the value is percent-encoded on write
    (post_device in routers/api.py) - decode it back here so a name in any
    script survives the round trip.
    """
    return unquote(cookie or "").strip()[:40]
