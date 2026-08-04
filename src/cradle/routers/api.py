"""Write endpoints + HTMX fragments (tasks U1, U2, U4, U7).

Every quick action is a single POST with no required field beyond the action
itself (the <=2-tap contract, T1). Requests carrying the HX-Request header get
an HTML fragment back; plain form posts get a redirect, so the app works with
JavaScript disabled or htmx unvendored.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from cradle.models import (
    FeedMethod,
    GrowthMeasure,
    NappyKind,
    StoolColour,
    UnknownTableError,
    to_utc,
)
from cradle.routers.deps import Services, device_name

UNDO_SECONDS = 10


def _toast(table: str, event_id: int, label: str) -> str:
    return (
        f'<div class="toast" role="status" data-table="{table}" '
        f'data-event-id="{event_id}" data-undo-seconds="{UNDO_SECONDS}">'
        f"<span>{label} logged</span>"
        f'<form method="post" action="/api/undo" hx-post="/api/undo" '
        f'hx-target="#toast" hx-swap="innerHTML">'
        f'<input type="hidden" name="table" value="{table}">'
        f'<input type="hidden" name="event_id" value="{event_id}">'
        f'<button class="undo" type="submit">Undo</button></form>'
        f'<a class="adjust" href="/history?focus={table}:{event_id}">Adjust time</a>'
        "</div>"
    )


def _respond(request: Request, table: str, event_id: int, label: str) -> Response:
    if request.headers.get("HX-Request"):
        return HTMLResponse(_toast(table, event_id, label))
    return RedirectResponse(f"/?logged={table}:{event_id}", status_code=303)


def build_api_router(svc: Services) -> APIRouter:
    router = APIRouter()

    def who(request: Request) -> str:
        return device_name(request.cookies.get("device_name"))

    # --------------------------------------------------------- quick actions
    @router.post("/api/feed")
    def post_feed(
        request: Request,
        method: Annotated[str, Form()] = FeedMethod.BREAST_LEFT.value,
        duration_min: Annotated[int | None, Form()] = None,
        volume_ml: Annotated[int | None, Form()] = None,
    ) -> Response:
        m = FeedMethod(method)
        event_id = svc.logging.log_feed(
            m, logged_by=who(request), duration_min=duration_min, volume_ml=volume_ml
        )
        return _respond(request, "feed", event_id, m.value.replace("_", " "))

    @router.post("/api/nappy")
    def post_nappy(
        request: Request,
        kind: Annotated[str, Form()] = NappyKind.WET.value,
        stool_colour: Annotated[str, Form()] = StoolColour.UNSET.value,
    ) -> Response:
        k = NappyKind(kind)
        event_id = svc.logging.log_nappy(k, StoolColour(stool_colour), logged_by=who(request))
        return _respond(request, "nappy", event_id, f"{k.value} nappy")

    @router.post("/api/sleep/toggle")
    def post_sleep_toggle(request: Request) -> Response:
        was_running = svc.logging.running_sleep() is not None
        event_id = svc.logging.toggle_sleep(logged_by=who(request))
        return _respond(request, "sleep", event_id, "wake" if was_running else "sleep start")

    # ------------------------------------------------------- detailed entry
    @router.post("/api/growth")
    def post_growth(
        request: Request,
        measure: Annotated[str, Form()],
        value: Annotated[int, Form()],
        source: Annotated[str, Form()] = "home",
    ) -> Response:
        event_id = svc.logging.log_growth(
            GrowthMeasure(measure), value, source, logged_by=who(request)
        )
        return _respond(request, "growth", event_id, measure)

    @router.post("/api/temperature")
    def post_temperature(
        request: Request,
        temp_c: Annotated[float, Form()],
        site: Annotated[str, Form()] = "axilla",
    ) -> Response:
        event_id = svc.logging.log_temperature(temp_c, site, logged_by=who(request))
        return _respond(request, "temperature", event_id, f"{temp_c:.1f} C")

    @router.post("/api/milestone")
    def post_milestone(
        request: Request,
        title: Annotated[str, Form()],
        category: Annotated[str, Form()] = "first",
        note: Annotated[str, Form()] = "",
    ) -> Response:
        event_id = svc.logging.log_milestone(category, title, note, logged_by=who(request))
        return _respond(request, "milestone", event_id, "milestone")

    @router.post("/api/note")
    def post_note(
        request: Request,
        text: Annotated[str, Form()],
        tags: Annotated[str, Form()] = "",
    ) -> Response:
        parsed = tuple(t.strip() for t in tags.split(",") if t.strip())
        event_id = svc.logging.log_note(text, parsed, logged_by=who(request))
        return _respond(request, "note", event_id, "note")

    # ------------------------------------------------------- undo / adjust
    @router.post("/api/undo")
    def post_undo(
        request: Request,
        table: Annotated[str, Form()],
        event_id: Annotated[int, Form()],
    ) -> Response:
        try:
            svc.logging.undo(table, event_id)
        except UnknownTableError:
            return HTMLResponse('<div class="toast err">Unknown record</div>', status_code=400)
        if request.headers.get("HX-Request"):
            return HTMLResponse('<div class="toast">Undone</div>')
        return RedirectResponse("/", status_code=303)

    @router.post("/api/adjust-time")
    def post_adjust_time(
        request: Request,
        table: Annotated[str, Form()],
        event_id: Annotated[int, Form()],
        ts: Annotated[str, Form()],
    ) -> Response:
        try:
            svc.logging.adjust_time(table, event_id, to_utc(datetime.fromisoformat(ts)))
        except (UnknownTableError, ValueError):
            return HTMLResponse('<div class="toast err">Bad time</div>', status_code=400)
        if request.headers.get("HX-Request"):
            return HTMLResponse('<div class="toast">Time updated</div>')
        return RedirectResponse("/history", status_code=303)

    @router.post("/api/delete")
    def post_delete(
        request: Request,
        table: Annotated[str, Form()],
        event_id: Annotated[int, Form()],
    ) -> Response:
        try:
            svc.logging.undo(table, event_id)
        except UnknownTableError:
            return HTMLResponse("Unknown record", status_code=400)
        if request.headers.get("HX-Request"):
            return HTMLResponse("")
        return RedirectResponse("/history", status_code=303)

    # -------------------------------------------------------------- alerts
    @router.post("/api/alerts/acknowledge")
    def post_acknowledge(
        request: Request,
        fingerprint: Annotated[str, Form()],
    ) -> Response:
        svc.alerts.acknowledge(fingerprint)
        if request.headers.get("HX-Request"):
            return HTMLResponse("")
        return RedirectResponse("/", status_code=303)

    # ----------------------------------------------------------- settings
    @router.post("/api/settings/profile")
    def post_profile(
        request: Request,
        name: Annotated[str, Form()],
        sex: Annotated[str, Form()],
        dob: Annotated[str, Form()],
        due_date: Annotated[str, Form()],
        birth_weight_g: Annotated[int, Form()],
    ) -> Response:
        try:
            svc.settings.save_profile(name, sex, dob, due_date, birth_weight_g)
        except ValueError:
            return HTMLResponse(
                '<p class="err">Check the dates and sex value.</p>', status_code=400
            )
        return RedirectResponse("/", status_code=303)

    @router.post("/api/settings/test-notification")
    def post_test_notification(request: Request) -> Response:
        svc.settings.test_notification()
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="ok">Sent - check your phone.</span>')
        return RedirectResponse("/settings", status_code=303)

    return router
