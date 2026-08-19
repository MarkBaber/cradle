"""Page routes (tasks U1, U3, U4, U7). Templates in routers/templates/."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from cradle.models import FeedMethod, GrowthMeasure, NappyKind, to_local, to_utc
from cradle.routers.deps import Services, device_name
from cradle.services.export_service import DOMAINS as EXPORT_DOMAINS
from cradle.services.history_service import DOMAINS
from cradle.services.milestone_service import CATEGORIES as MILESTONE_CATEGORIES

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["local"] = to_local


def build_pages_router(svc: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def quick_entry(request: Request) -> Response:
        if not svc.settings.has_profile():
            return RedirectResponse("/settings?first_run=1", status_code=303)
        panel, method, kind = _open_panel(request)
        return TEMPLATES.TemplateResponse(
            request,
            "quick_entry.html",
            {
                "summary": svc.today.summary(),
                "pinned": svc.alerts.pinned(),
                "outstanding": svc.alerts.outstanding(),
                "logged": request.query_params.get("logged", ""),
                "open_panel": panel,
                "open_method": method,
                "open_kind": kind,
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

    @router.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "profile": svc.settings.profile(),
                "first_run": request.query_params.get("first_run") == "1",
                "device": device_name(request.cookies.get("device_name")),
            },
        )

    return router


_FEED_METHODS = {m.value for m in FeedMethod}
_NAPPY_KINDS = {k.value for k in NappyKind}


def _open_panel(request: Request) -> tuple[str, str, str]:
    """Which quick-entry panel (if any) a tile tap asked to open (U18).

    A bad/unknown method or kind collapses back to no panel rather than 4xx-ing -
    the panel is a GET, so there is nothing to reject, only nothing to show.
    """
    panel = request.query_params.get("panel", "")
    method = request.query_params.get("method", "")
    kind = request.query_params.get("kind", "")
    if panel == "feed" and method in _FEED_METHODS:
        return "feed", method, ""
    if panel == "nappy" and kind in _NAPPY_KINDS:
        return "nappy", "", kind
    return "", "", ""


def _int_param(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


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
