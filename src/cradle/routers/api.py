"""Write endpoints + HTMX fragments (tasks U1, U2, U4, U7).

Every quick action is a single POST with no required field beyond the action
itself (the <=2-tap contract, T1). Requests carrying the HX-Request header get
an HTML fragment back; plain form posts get a redirect, so the app works with
JavaScript disabled or htmx unvendored.
"""

from datetime import UTC, datetime
from html import escape
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from cradle.models import (
    FeedMethod,
    GrowthMeasure,
    NappyKind,
    StoolColour,
    UneditableFieldError,
    UnknownTableError,
    to_local,
    to_utc,
)
from cradle.routers.deps import Services, device_name

UNDO_SECONDS = 10
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 60 * 60  # browsers cap persistent cookies at 400 days

# Value coercion for the generic post-hoc field editor (U10). The allow-list
# itself lives in EventsRepo.EDITABLE; this only maps a raw form string to the
# right Python type for the fields that list permits.
_INT_FIELDS = {"duration_min", "volume_ml", "value"}
_DATETIME_FIELDS = {"ts", "ts_end"}
_FLOAT_FIELDS = {"temp_c"}


def _coerce_field_value(field: str, raw: str) -> object:
    if field in _DATETIME_FIELDS:
        return to_utc(datetime.fromisoformat(raw))
    if field in _INT_FIELDS:
        return int(raw)
    if field in _FLOAT_FIELDS:
        return float(raw)
    return raw


def _panel_ts(raw: str | None) -> datetime | None:
    """Parse the quick-entry panel's <input type=time> value (U18).

    It carries no date, so a provided "HH:MM" is combined with today's date in
    the *configured* display zone (models/timefmt, task U9) - not the server's
    OS zone, which would risk the wrong calendar day near local midnight. Blank
    or unparseable input means "use the clock's now", same as ts=None.
    """
    if not raw:
        return None
    try:
        hour, minute = (int(p) for p in raw.split(":"))
    except ValueError:
        return None
    today = to_local(datetime.now(UTC)).date()
    return to_utc(datetime(today.year, today.month, today.day, hour, minute))


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


def _normalize_device_name(value: str) -> str:
    """Collapse whitespace/control-character runs to a single space and cap the
    length at 40. Everything else - any script, emoji included - is kept:
    Set-Cookie is latin-1 on the wire and rejects control characters, but
    post_device percent-encodes the result before writing it, so a non-latin-1
    name no longer has to be dropped to avoid a 500."""
    kept = (c if c.isprintable() else " " for c in value)
    return " ".join("".join(kept).split())[:40]


def _device_saved(name: str) -> str:
    """Echo what was stored: the cookie-safe name can differ from what was typed."""
    if not name:
        return '<span class="ok">Device name cleared.</span>'
    return f'<span class="ok">Device name saved. Entries here are labelled "{escape(name)}".</span>'


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
        ts: Annotated[str | None, Form()] = None,
    ) -> Response:
        m = FeedMethod(method)
        event_id = svc.logging.log_feed(
            m,
            logged_by=who(request),
            ts=_panel_ts(ts),
            duration_min=duration_min,
            volume_ml=volume_ml,
        )
        return _respond(request, "feed", event_id, m.value.replace("_", " "))

    @router.post("/api/nappy")
    def post_nappy(
        request: Request,
        kind: Annotated[str, Form()] = NappyKind.WET.value,
        stool_colour: Annotated[str, Form()] = StoolColour.UNSET.value,
        ts: Annotated[str | None, Form()] = None,
    ) -> Response:
        k = NappyKind(kind)
        event_id = svc.logging.log_nappy(
            k, StoolColour(stool_colour), logged_by=who(request), ts=_panel_ts(ts)
        )
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

    @router.post("/api/edit-field")
    def post_edit_field(
        request: Request,
        table: Annotated[str, Form()],
        event_id: Annotated[int, Form()],
        field: Annotated[str, Form()],
        value: Annotated[str, Form()],
    ) -> Response:
        """Generic post-hoc field edit (SPEC 5.4): volume_ml, duration_min, ts_end etc.

        EventsRepo.edit_event enforces the per-table column allow-list; a field
        outside it raises UneditableFieldError here, same as an unknown table.
        """
        try:
            svc.logging.edit(table, event_id, {field: _coerce_field_value(field, value)})
        except (UnknownTableError, UneditableFieldError, ValueError):
            return HTMLResponse('<div class="toast err">Bad value</div>', status_code=400)
        if request.headers.get("HX-Request"):
            return HTMLResponse('<div class="toast">Updated</div>')
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

    @router.post("/api/settings/device")
    def post_device(
        request: Request,
        device: Annotated[str, Form()] = "",
    ) -> Response:
        """Name this device (D7). A plain cookie: it labels rows, it protects nothing.
        Rows already written keep whatever attribution they were saved with."""
        name = _normalize_device_name(device)
        back = "/settings" if svc.settings.has_profile() else "/settings?first_run=1"
        resp: Response = (
            HTMLResponse(_device_saved(name))
            if request.headers.get("HX-Request")
            else RedirectResponse(back, status_code=303)
        )
        resp.set_cookie(
            "device_name", quote(name, safe=""), max_age=DEVICE_COOKIE_MAX_AGE, samesite="lax"
        )
        return resp

    @router.post("/api/settings/test-notification")
    def post_test_notification(request: Request) -> Response:
        svc.settings.test_notification()
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="ok">Sent - check your phone.</span>')
        return RedirectResponse("/settings", status_code=303)

    return router
