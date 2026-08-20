"""Composition root: DB, repos, services, routers, static assets."""

import json
import logging
import re
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from cradle import __version__
from cradle.models import Finding, ReferenceDataMissingError
from cradle.ports.clock import Clock, SystemClock
from cradle.ports.notifier import ConsoleNotifier, Notifier, NtfyNotifier
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
    MilkStockService,
    SeriesService,
    SettingsService,
    TodayService,
)
from cradle.services.alerts_service import load_config

DB_PATH = Path("data/cradle.db")
CONFIG_PATH = Path("rules_config.toml")
STATIC_DIR = Path(__file__).parent / "routers" / "static"

_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{0,64}$")
_NTFY_SECTION_RE = re.compile(r"(?ms)^\[ntfy\]\n(?:(?!^\[).)*")
_NTFY_TOPIC_LINE_RE = re.compile(r'(?m)^(topic\s*=\s*)"[^"]*"')


def _ntfy_table(config_path: Path) -> Mapping[str, object]:
    ntfy = load_config(config_path).get("ntfy", {})
    return ntfy if isinstance(ntfy, Mapping) else {}


def notifier_from_config(config_path: Path) -> Notifier:
    """Build the notifier the [ntfy] table in *config_path* selects.

    Both server and topic must be set or delivery falls back to the console
    -- an unset topic must never raise (N3).
    """
    ntfy = _ntfy_table(config_path)
    server = str(ntfy.get("server", "")).strip()
    topic = str(ntfy.get("topic", "")).strip()
    if server and topic:
        return NtfyNotifier(server, topic)
    return ConsoleNotifier()


def _write_ntfy_topic(config_path: Path, topic: str) -> None:
    """Rewrite only the topic value inside [ntfy], never any other table.

    rules_config.toml's other tables hold architect-owned clinical
    thresholds (CLAUDE.md); a runtime settings edit must not be able to
    reach them, so this patches the existing [ntfy] block textually instead
    of round-tripping the whole file through a TOML writer. The new value is
    JSON-string-escaped, so it can never break out of its own quotes into a
    new key.
    """
    text = config_path.read_text()
    section_match = _NTFY_SECTION_RE.search(text)
    if section_match is None:
        raise ValueError("rules_config.toml has no [ntfy] table")
    section = section_match.group(0)
    new_section, count = _NTFY_TOPIC_LINE_RE.subn(
        lambda m: m.group(1) + json.dumps(topic), section, count=1
    )
    if count == 0:
        raise ValueError("rules_config.toml [ntfy] table has no topic key")
    text = text[: section_match.start()] + new_section + text[section_match.end() :]
    config_path.write_text(text)


class _LiveNotifier:
    """Delegates to whatever notifier the [ntfy] config currently selects.

    Editing the topic in /settings rewrites rules_config.toml on disk; this
    lets the already-constructed settings/alerts services pick up the new
    topic without a process restart.
    """

    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def send(self, finding: Finding) -> None:
        self._notifier.send(finding)

    def refresh(self, notifier: Notifier) -> None:
        self._notifier = notifier


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
        milk=MilkStockService(events, clock),
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
    live_notifier = _LiveNotifier(notifier or notifier_from_config(config_path))
    svc = build_services(db, clock or SystemClock(), live_notifier, config_path, reference)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            from cradle.ports.scheduler import stop_scheduler  # noqa: PLC0415

            stop_scheduler(scheduler)

    app = FastAPI(title="CRADLE", lifespan=lifespan)
    app.state.services = svc  # exposed for operational checks and tests
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(build_pages_router(svc))
    app.include_router(build_api_router(svc))

    @app.get("/api/settings/ntfy")
    def get_ntfy_settings() -> JSONResponse:
        return JSONResponse({"topic": str(_ntfy_table(config_path).get("topic", ""))})

    @app.post("/api/settings/ntfy")
    def post_ntfy_settings(request: Request, topic: Annotated[str, Form()] = "") -> Response:
        topic = topic.strip()
        if not _TOPIC_RE.fullmatch(topic):
            msg = '<p class="err">Topic must be letters, numbers, - or _ (max 64 chars).</p>'
            return HTMLResponse(msg, status_code=400)
        _write_ntfy_topic(config_path, topic)
        live_notifier.refresh(notifier_from_config(config_path))
        if request.headers.get("HX-Request"):
            ok = "Topic saved." if topic else "Topic cleared - notifications go to the console log."
            return HTMLResponse(f'<span class="ok">{ok}</span>')
        return RedirectResponse("/settings", status_code=303)

    if start_scheduler:
        try:
            from cradle.ports.scheduler import build_scheduler  # noqa: PLC0415

            app.state.scheduler = build_scheduler(svc.alerts.sweep)
        except Exception:  # pragma: no cover - scheduling is not worth an outage
            logging.getLogger(__name__).exception(
                "scheduler failed to start; alerts will not fire automatically"
            )
    return app
