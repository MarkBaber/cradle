"""Composition root: DB, repos, services, routers, static assets."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cradle import __version__
from cradle.models import ReferenceDataMissingError
from cradle.ports.clock import Clock, SystemClock
from cradle.ports.notifier import ConsoleNotifier, Notifier
from cradle.reference.lms import LmsTable, load_table
from cradle.repos.alert_log_repo import AlertLogRepo
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.db import Db
from cradle.repos.events_repo import EventsRepo
from cradle.routers.api import build_api_router
from cradle.routers.deps import Services
from cradle.routers.pages import build_pages_router
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

DB_PATH = Path("data/cradle.db")
CONFIG_PATH = Path("rules_config.toml")
STATIC_DIR = Path(__file__).parent / "routers" / "static"


def load_reference() -> tuple[LmsTable | None, str | None]:
    """Load the growth reference, or report why it is unavailable.

    Missing reference data disables centiles but must never take the app down:
    logging a feed at 3am matters more than a chart.
    """
    try:
        return load_table(), None
    except ReferenceDataMissingError as exc:
        return None, str(exc)


def build_services(
    db: Db,
    clock: Clock,
    notifier: Notifier,
    config_path: Path = CONFIG_PATH,
    reference: tuple[LmsTable | None, str | None] | None = None,
) -> Services:
    events = EventsRepo(db)
    baby = BabyRepo(db)
    table, table_error = reference if reference is not None else load_reference()
    growth = GrowthService(events, baby, table, table_error)
    alert_log = AlertLogRepo(db)
    return Services(
        logging=LoggingService(events, clock),
        today=TodayService(events, baby, clock, config_path),
        history=HistoryService(events),
        settings=SettingsService(baby, notifier),
        growth=growth,
        alerts=AlertsService(events, baby, alert_log, growth, notifier, clock, config_path),
        milestones=MilestoneService(events, baby),
        export=ExportService(events, baby, alert_log, __version__),
        series=SeriesService(events, baby, clock),
    )


def create_app(
    db_path: Path | str = DB_PATH,
    clock: Clock | None = None,
    notifier: Notifier | None = None,
    config_path: Path = CONFIG_PATH,
    reference: tuple[LmsTable | None, str | None] | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Db(db_path)
    db.migrate()
    svc = build_services(
        db, clock or SystemClock(), notifier or ConsoleNotifier(), config_path, reference
    )

    app = FastAPI(title="CRADLE")
    app.state.services = svc  # exposed for operational checks and tests
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(build_pages_router(svc))
    app.include_router(build_api_router(svc))

    if start_scheduler:
        try:
            from cradle.ports.scheduler import build_scheduler  # noqa: PLC0415

            app.state.scheduler = build_scheduler(svc.alerts.sweep)
        except Exception:  # pragma: no cover - scheduling is not worth an outage
            logging.getLogger(__name__).exception(
                "scheduler failed to start; alerts will not fire automatically"
            )
    return app
