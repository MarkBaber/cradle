"""Page routes (tasks U1, U3, U4, U7). Templates in routers/templates/."""

import math
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from cradle.models import (
    BottleColour,
    FeedMethod,
    GrowthMeasure,
    MilkStore,
    NappyKind,
    to_local,
    to_utc,
)
from cradle.models.enums import ActivityCategory
from cradle.routers import api as api_module
from cradle.routers.deps import Services, device_name
from cradle.services.export_service import DOMAINS as EXPORT_DOMAINS
from cradle.services.history_service import DOMAINS
from cradle.services.milestone_service import CATEGORIES as MILESTONE_CATEGORIES
from cradle.services.projection_service import ProjectionResult
from cradle.services.series_service import MAX_DAYS


def _format_age(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["local"] = to_local
TEMPLATES.env.filters["age"] = _format_age


# ------------------------------------------------------- next-event dials (U23)
# Presentation-only view model over V3's ProjectionResult: no storage, no write
# path. Lives here rather than in _projections.html because the auto-scaled
# window is arithmetic shared between the two rings, and reads better as
# Python than duplicated in Jinja.


def _hours(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds() / 3600


def _hm(hours: float) -> str:
    """'0h30' style countdown, rounded to the minute."""
    total_min = round(abs(hours) * 60)
    h, m = divmod(total_min, 60)
    return f"{h}h{m:02d}"


def _ago(hours: float) -> str:
    total_min = round(abs(hours) * 60)
    h, m = divmod(total_min, 60)
    return f"{h}h{m}m ago" if h else f"{m}m ago"


def _fraction_left(due_at: datetime | None, now: datetime, window_h: float) -> float:
    """1.0 = full ring (cold, nothing to deplete yet); 0.0 = emptied (overdue)."""
    if due_at is None:
        return 1.0
    remaining = _hours(now, due_at)
    if remaining <= 0:
        return 0.0
    return min(1.0, remaining / window_h)


def _ring_label(name: str, due_at: datetime | None, overdue: bool, now: datetime) -> str:
    if due_at is None:
        cold_noun = "feeds" if name == "feed" else "nappies"
        return f"log a few {cold_noun} and this will fill in"
    delta = _hours(now, due_at)
    if overdue:
        return f"{name} due {_ago(delta)}"
    return f"{name} in {_hm(delta)}, {to_local(due_at).strftime('%H:%M')}"


def _feed_echo(p: ProjectionResult) -> str:
    """The compact one-line feed-only readout for /log (task U23)."""
    if p.feed_due_at is None:
        return "log a few feeds and this will fill in"
    delta = _hours(p.as_of, p.feed_due_at)
    if p.feed_overdue:
        return f"feed due {_ago(delta)}"
    return f"next feed in {_hm(delta)}"


def _dial_context(p: ProjectionResult) -> dict[str, object]:
    now = p.as_of
    window_h = float(p.window_max_h)
    for due in (p.feed_due_at, p.mess_due_at):
        if due is not None:
            remaining = _hours(now, due)
            if remaining > window_h:
                window_h = math.ceil(remaining)

    feed_cold = p.feed_due_at is None
    mess_cold = p.mess_due_at is None
    both_cold = feed_cold and mess_cold

    if both_cold:
        center_label = "log a few feeds and this will fill in"
        center_overdue = False
    else:
        candidates = [
            (due, overdue, which)
            for due, overdue, which in (
                (p.feed_due_at, p.feed_overdue, "feed"),
                (p.mess_due_at, p.mess_overdue, "mess"),
            )
            if due is not None
        ]
        soonest_due, soonest_overdue, which = min(candidates, key=lambda c: c[0])
        if soonest_overdue:
            center_label = f"{which} due {_ago(_hours(now, soonest_due))}"
        else:
            center_label = (
                f"next {which} in {_hm(_hours(now, soonest_due))}, "
                f"{to_local(soonest_due).strftime('%H:%M')}"
            )
        center_overdue = soonest_overdue

    dirty_hint = None
    if p.dirty_due_at is not None:
        dirty_hint = f"dirty due ~{to_local(p.dirty_due_at).strftime('%H:%M')}"

    return {
        "both_cold": both_cold,
        "feed_cold": feed_cold,
        "mess_cold": mess_cold,
        "feed_overdue": p.feed_overdue,
        "mess_overdue": p.mess_overdue,
        "feed_fraction": _fraction_left(p.feed_due_at, now, window_h),
        "mess_fraction": _fraction_left(p.mess_due_at, now, window_h),
        "feed_label": _ring_label("feed", p.feed_due_at, p.feed_overdue, now),
        "mess_label": _ring_label("mess", p.mess_due_at, p.mess_overdue, now),
        "center_label": center_label,
        "center_overdue": center_overdue,
        "hunger_pct": 100 if p.feed_overdue else round(p.hunger_fraction * 100),
        "mess_pct": 100 if p.mess_overdue else round(p.mess_level_fraction * 100),
        "dirty_hint": dirty_hint,
    }


def build_pages_router(svc: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def quick_entry(request: Request) -> Response:
        if not svc.settings.has_profile():
            return RedirectResponse("/settings?first_run=1", status_code=303)
        panel, method, kind, category = _open_panel(request)
        summary = svc.today.summary()
        return TEMPLATES.TemplateResponse(
            request,
            "quick_entry.html",
            {
                "summary": summary,
                "pinned": svc.alerts.pinned(),
                "outstanding": svc.alerts.outstanding(),
                "logged": request.query_params.get("logged", ""),
                "open_panel": panel,
                "open_method": method,
                "open_kind": kind,
                "open_category": category,
                "now_time": to_local(summary.as_of).strftime("%H:%M") if summary else "",
                "entry_defaults": api_module.entry_defaults(api_module.CONFIG_PATH),
                "activity_targets": api_module.activity_targets(api_module.CONFIG_PATH),
                "feed_echo": _feed_echo(svc.projections.projections()),
            },
        )

    @router.get("/today", response_class=HTMLResponse)
    def today(request: Request) -> Response:
        if not svc.settings.has_profile():
            return RedirectResponse("/settings?first_run=1", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "today.html",
            {
                "summary": svc.today.summary(),
                "pinned": svc.alerts.pinned(),
                "outstanding": svc.alerts.outstanding(),
                "dial": _dial_context(svc.projections.projections()),
            },
        )

    @router.get("/today/fragment", response_class=HTMLResponse)
    def today_fragment(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request, "_today_strip.html", {"summary": svc.today.summary()}
        )

    @router.get("/history", response_class=HTMLResponse)
    def history(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            "history.html",
            {
                "rows": _rows(request, svc),
                "domains": DOMAINS,
                "selected": _selected(request),
                "focus": request.query_params.get("focus", ""),
            },
        )

    @router.get("/history/fragment", response_class=HTMLResponse)
    def history_fragment(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            "_history_table.html",
            {"rows": _rows(request, svc), "focus": request.query_params.get("focus", "")},
        )

    @router.get("/charts", response_class=HTMLResponse)
    def charts(request: Request) -> Response:
        if not svc.settings.has_profile():
            return RedirectResponse("/settings?first_run=1", status_code=303)
        raw = request.query_params.get("measure", GrowthMeasure.WEIGHT.value)
        measure = (
            GrowthMeasure(raw) if raw in {m.value for m in GrowthMeasure} else GrowthMeasure.WEIGHT
        )
        return TEMPLATES.TemplateResponse(
            request,
            "charts.html",
            {
                "measure": measure,
                "measures": list(GrowthMeasure),
                "series": svc.growth.centile_chart_series(measure),
                "assessment": svc.growth.assessment(),
                "daily": svc.series.daily(_int_param(request, "days", 14)),
            },
        )

    @router.get("/patterns", response_class=HTMLResponse)
    def patterns(request: Request) -> Response:
        if not svc.settings.has_profile():
            return RedirectResponse("/settings?first_run=1", status_code=303)
        days = _int_param(request, "days", 14)
        return TEMPLATES.TemplateResponse(
            request,
            "patterns.html",
            {
                "ribbon": svc.series.ribbon(days),
                "daily": svc.series.daily(days),
                "days": days,
                "hours": range(0, 25, 3),
            },
        )

    @router.get("/api/series/daily")
    def daily_series(request: Request) -> Response:
        d = svc.series.daily(_int_param(request, "days", 14))
        return JSONResponse(
            {
                "days": [x.isoformat() for x in d.days],
                "feeds": list(d.feeds),
                "bottle_ml": list(d.bottle_ml),
                "wet": list(d.wet),
                "dirty": list(d.dirty),
                "sleep_hours": list(d.sleep_hours),
                "longest_sleep_hours": list(d.longest_sleep_hours),
                "night_wakings": list(d.night_wakings),
                "age_days": list(d.age_days),
                "targets": {
                    "feed_volume_ml": list(d.targets.feed_volume_ml),
                    "wet_min": list(d.targets.wet_min),
                    "wet_max": list(d.targets.wet_max),
                    "dirty_min": list(d.targets.dirty_min),
                    "dirty_max": list(d.targets.dirty_max),
                    "sleep_min_hours": list(d.targets.sleep_min_hours),
                    "sleep_max_hours": list(d.targets.sleep_max_hours),
                },
            }
        )

    @router.get("/api/series/ribbon")
    def ribbon_series(request: Request) -> Response:
        r = svc.series.ribbon(_int_param(request, "days", 14))
        return JSONResponse(
            {
                "night_start": r.night_start,
                "night_end": r.night_end,
                "days": [
                    {
                        "day": day.day.isoformat(),
                        "sleep": [[s.start_hour, s.end_hour] for s in day.sleep],
                        "feeds": list(day.feeds),
                        "nappies": list(day.nappies),
                    }
                    for day in r.days
                ],
            }
        )

    @router.get("/api/charts/{measure}")
    def charts_series(measure: str) -> Response:
        if measure not in {m.value for m in GrowthMeasure}:
            return JSONResponse({"error": "unknown measure"}, status_code=404)
        series = svc.growth.centile_chart_series(GrowthMeasure(measure))
        if series is None:
            return JSONResponse({"error": "no baby profile"}, status_code=404)
        return JSONResponse(
            {
                "measure": series.measure.value,
                "unit": series.unit,
                "ages": list(series.ages),
                "curves": {k: list(v) for k, v in series.curves.items()},
                "trajectory": [list(p) for p in series.trajectory],
                "frames": list(series.frames),
                "unavailable_reason": series.unavailable_reason,
            }
        )

    @router.get("/sw.js")
    def service_worker() -> Response:
        """Served from root: a worker's scope is capped by its own path, so
        /static/sw.js could only ever control /static/* (task W1)."""
        path = Path(__file__).parent / "static" / "sw.js"
        return Response(
            path.read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"},
        )

    @router.get("/milestones", response_class=HTMLResponse)
    def milestones(request: Request) -> Response:
        if not svc.settings.has_profile():
            return RedirectResponse("/settings?first_run=1", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "milestones.html",
            {
                "cards": svc.milestones.timeline() or (),
                "corrected": svc.milestones.uses_corrected_age(),
                "categories": MILESTONE_CATEGORIES,
            },
        )

    @router.get("/milk", response_class=HTMLResponse)
    def milk(request: Request) -> Response:
        if not svc.settings.has_profile():
            return RedirectResponse("/settings?first_run=1", status_code=303)
        stock = svc.milk.stock_on_hand()
        live_colours = {ba.batch.colour for s in stock.values() for ba in s.batches}
        available_colours = [c for c in BottleColour if c not in live_colours]
        return TEMPLATES.TemplateResponse(
            request,
            "milk.html",
            {
                "stock": stock,
                "stores": list(MilkStore),
                "available_colours": available_colours,
            },
        )

    @router.get("/export", response_class=HTMLResponse)
    def export_page(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(request, "export.html", {"domains": EXPORT_DOMAINS})

    @router.get("/export/cradle.json")
    def export_json() -> Response:
        return Response(
            svc.export.export_json(),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="cradle.json"'},
        )

    @router.get("/export/{domain}.csv")
    def export_csv(domain: str) -> Response:
        if domain not in EXPORT_DOMAINS:
            return JSONResponse({"error": "unknown domain"}, status_code=404)
        return Response(
            svc.export.export_csv(domain),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{domain}.csv"'},
        )

    @router.get("/messages", response_class=HTMLResponse)
    def messages(request: Request) -> Response:
        if not svc.settings.has_profile():
            return RedirectResponse("/settings?first_run=1", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "messages.html",
            {
                "messages": svc.alerts.all_messages(),
            },
        )

    @router.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "profile": svc.settings.profile(),
                "first_run": request.query_params.get("first_run") == "1",
                "device": device_name(request.cookies.get("device_name")),
                "entry_defaults": api_module.entry_defaults(api_module.CONFIG_PATH),
            },
        )

    return router


_FEED_METHODS = {m.value for m in FeedMethod}
_NAPPY_KINDS = {k.value for k in NappyKind}
_ACTIVITY_CATEGORIES = {c.value for c in ActivityCategory}
_MORE_PANELS = {"growth", "temperature", "milestone", "note"}


def _open_panel(request: Request) -> tuple[str, str, str, str]:
    """Which quick-entry panel (if any) a tile tap asked to open (U18/U27).

    A bad/unknown method, kind or category collapses back to no panel rather than 4xx-ing -
    the panel is a GET, so there is nothing to reject, only nothing to show.
    """
    panel = request.query_params.get("panel", "")
    method = request.query_params.get("method", "")
    kind = request.query_params.get("kind", "")
    category = request.query_params.get("category", "")
    if panel == "feed" and method in _FEED_METHODS:
        return "feed", method, "", ""
    if panel == "nappy" and kind in _NAPPY_KINDS:
        return "nappy", "", kind, ""
    if panel == "activity" and category in _ACTIVITY_CATEGORIES:
        return "activity", "", "", category
    if panel in _ACTIVITY_CATEGORIES:
        return "activity", "", "", panel
    if panel in _MORE_PANELS:
        return panel, "", "", ""
    return "", "", "", ""


def _int_param(
    request: Request, name: str, default: int, *, minimum: int = 1, maximum: int = MAX_DAYS
) -> int:
    """Clamped so a mistyped/malicious `days` can't force an unbounded row
    build in series_service (task U13) - there is no auth (D7) gating this."""
    raw = request.query_params.get(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _selected(request: Request) -> tuple[str, ...]:
    raw = request.query_params.getlist("domain")
    return tuple(d for d in raw if d in DOMAINS) or DOMAINS


def _rows(request: Request, svc: Services) -> list[object]:
    def parse(name: str) -> datetime | None:
        raw = request.query_params.get(name)
        if not raw:
            return None
        try:
            return to_utc(datetime.fromisoformat(raw))
        except ValueError:
            return None

    return list(
        svc.history.rows(domains=_selected(request), since=parse("since"), until=parse("until"))
    )
