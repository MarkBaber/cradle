"""Composition root: DB, repos, services, routers, static assets."""

import json
import logging
import os
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
from cradle.ports.whatsapp import WhatsAppCloudNotifier, WhatsAppNotifier
from cradle.reference.lms import LmsTable, load_table
from cradle.repos.alert_log_repo import AlertLogRepo
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.badges_repo import BadgesRepo
from cradle.repos.chat_log_repo import ChatLogRepo
from cradle.repos.db import Db
from cradle.repos.events_repo import EventsRepo
from cradle.routers.api import build_api_router
from cradle.routers.deps import Services
from cradle.routers.pages import build_pages_router
from cradle.services import (
    AchievementsService,
    AlertsService,
    ExportService,
    GrowthService,
    HistoryService,
    JournalService,
    LoggingService,
    MilestoneService,
    MilkStockService,
    ProjectionService,
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

_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_+-]{0,64}$")


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


def whatsapp_from_config(chat_log: ChatLogRepo) -> WhatsAppCloudNotifier:
    """Build the WhatsApp echo adapter from its DB-backed setting plus the environment.

    chat_id is a plain destination id, editable at runtime like ntfy's topic
    (N3) -- but unlike the topic it is not TOML-backed: this task's touches
    list has no room for rules_config.toml, so chat_id lives in the one-row
    whatsapp_settings table (migration 0008) via ChatLogRepo instead.
    access_token and phone_number_id are read from the environment only and
    are never written by this app: task N5's notes judged a WhatsApp access
    token materially more sensitive than a shared ntfy topic, so unlike N3 it
    is kept off disk entirely rather than reusing that precedent. There is no
    git-ignored local-secrets-file convention anywhere else in this repo to
    extend instead, so environment variables are the storage location
    recorded for this secret.
    """
    chat_id = chat_log.get_chat_id()
    access_token = os.environ.get("CRADLE_WHATSAPP_TOKEN", "").strip()
    phone_number_id = os.environ.get("CRADLE_WHATSAPP_PHONE_ID", "").strip()
    return WhatsAppCloudNotifier(access_token, phone_number_id, chat_id)


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


class _LiveWhatsApp:
    """Delegates to whatever WhatsApp adapter the current chat_id setting selects.

    Same rationale as _LiveNotifier: editing chat_id in /settings updates the
    whatsapp_settings table, and LoggingService already holds a reference to
    this wrapper, so refresh() lets that edit take effect without a restart.
    """

    def __init__(self, whatsapp: WhatsAppNotifier) -> None:
        self._whatsapp = whatsapp

    @property
    def configured(self) -> bool:
        return self._whatsapp.configured

    def send(self, text: str) -> bool:
        return self._whatsapp.send(text)

    def refresh(self, whatsapp: WhatsAppNotifier) -> None:
        self._whatsapp = whatsapp


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
    whatsapp: WhatsAppNotifier,
    config_path: Path = CONFIG_PATH,
    reference: tuple[LmsTable | None, str | None] | None = None,
) -> Services:
    events = EventsRepo(db)
    baby = BabyRepo(db)
    badges = BadgesRepo(db)
    chat_log = ChatLogRepo(db)
    table, table_error = reference if reference is not None else load_reference()
    growth = GrowthService(events, baby, table, table_error)
    alert_log = AlertLogRepo(db)
    history = HistoryService(events)
    return Services(
        logging=LoggingService(events, clock, history, whatsapp, chat_log),
        today=TodayService(events, baby, clock, config_path),
        history=history,
        settings=SettingsService(baby, notifier),
        growth=growth,
        alerts=AlertsService(events, baby, alert_log, growth, notifier, clock, config_path),
        milestones=MilestoneService(events, baby),
        export=ExportService(events, baby, alert_log, __version__),
        series=SeriesService(events, baby, clock, config_path),
        milk=MilkStockService(events, clock),
        projections=ProjectionService(events, clock, config_path),
        journal=JournalService(events, clock),
        achievements=AchievementsService(events, badges, notifier, clock),
    )


def create_app(
    db_path: Path | str = DB_PATH,
    clock: Clock | None = None,
    notifier: Notifier | None = None,
    whatsapp: WhatsAppNotifier | None = None,
    config_path: Path = CONFIG_PATH,
    reference: tuple[LmsTable | None, str | None] | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = Db(db_path)
    db.migrate()
    chat_log = ChatLogRepo(db)
    live_notifier = _LiveNotifier(notifier or notifier_from_config(config_path))
    live_whatsapp = _LiveWhatsApp(whatsapp or whatsapp_from_config(chat_log))
    svc = build_services(
        db, clock or SystemClock(), live_notifier, live_whatsapp, config_path, reference
    )

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

    @app.get("/api/settings/whatsapp")
    def get_whatsapp_settings() -> JSONResponse:
        return JSONResponse({"chat_id": chat_log.get_chat_id()})

    @app.post("/api/settings/whatsapp")
    def post_whatsapp_settings(request: Request, chat_id: Annotated[str, Form()] = "") -> Response:
        chat_id = chat_id.strip()
        if not _CHAT_ID_RE.fullmatch(chat_id):
            msg = '<p class="err">Chat id must be letters, numbers, - , _ or + (max 64 chars).</p>'
            return HTMLResponse(msg, status_code=400)
        chat_log.set_chat_id(chat_id)
        live_whatsapp.refresh(whatsapp_from_config(chat_log))
        if request.headers.get("HX-Request"):
            ok = "Chat id saved." if chat_id else "Chat id cleared - WhatsApp echo is disabled."
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
