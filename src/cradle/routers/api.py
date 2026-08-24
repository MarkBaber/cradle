"""Write endpoints + HTMX fragments (tasks U1, U2, U4, U7).

Every quick action is a single POST with no required field beyond the action
itself (the <=2-tap contract, T1). Requests carrying the HX-Request header get
an HTML fragment back; plain form posts get a redirect, so the app works with
JavaScript disabled or htmx unvendored.
"""

import re
import statistics
import tomllib
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from cradle.models import (
    BottleColour,
    FeedMethod,
    GrowthMeasure,
    MilkStore,
    NappyKind,
    StoolColour,
    StoolConsistency,
    UneditableFieldError,
    UnknownTableError,
    to_local,
    to_utc,
)
from cradle.models.enums import ActivityCategory
from cradle.routers.deps import Services, device_name
from cradle.services import InvalidBatchTransitionError, UnknownBatchError
from cradle.services.projection_service import (
    FETCH_LIMIT,
    MIN_SAMPLES,
    _bottle_rate_samples,
    _gap_median_hours,
    _median_bottle_volume,
)

UNDO_SECONDS = 10
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 60 * 60  # browsers cap persistent cookies at 400 days

# Config path for the quick-entry smart defaults (task U22). Not threaded via
# create_app/Services (both outside this task's touches) - a module-level
# path, read fresh on every call, mirroring models/timefmt.py's CONFIG_PATH
# convention. Tests monkeypatch this attribute directly, same as
# tests/unit/test_timefmt.py does for timefmt.CONFIG_PATH.
CONFIG_PATH = Path("rules_config.toml")

DEFAULT_BOTTLE_VOLUME_ML = 60
DEFAULT_BREAST_DURATION_MIN = 20

DEFAULT_WHEEL_STEPS: dict[str, int | float] = {
    "weight": 25,
    "length": 5,
    "head_circ": 5,
    "temp_c": 0.1,
    "bottle_volume_ml": 5,
    "breast_duration_min": 5,
    "tummy_time_duration_min": 1,
    "reading_talking_duration_min": 1,
    "sensory_play_duration_min": 1,
    "foreign_language_duration_min": 1,
}

_ENTRY_DEFAULTS_SECTION_RE = re.compile(r"(?ms)^\[entry_defaults\]\n(?:(?!^\[).)*")
_BOTTLE_VOLUME_LINE_RE = re.compile(r"(?m)^(bottle_volume_ml\s*=\s*)\d+")
_BREAST_DURATION_LINE_RE = re.compile(r"(?m)^(breast_duration_min\s*=\s*)\d+")

_WHEEL_STEPS_SECTION_RE = re.compile(r"(?ms)^\[wheel_steps\]\n(?:(?!^\[).)*")
_WHEEL_STEPS_LINE_RES = {
    key: re.compile(rf"(?m)^({key}\s*=\s*)[^\n]*") for key in DEFAULT_WHEEL_STEPS
}


def wheel_steps(config_path: Path) -> dict[str, int | float]:
    """Read [wheel_steps] from *config_path* (task U37)."""
    table: object = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            table = tomllib.load(fh).get("wheel_steps", {})
    if not isinstance(table, dict):
        table = {}
    res: dict[str, int | float] = {}
    for key, default in DEFAULT_WHEEL_STEPS.items():
        val = table.get(key, default)
        is_num = isinstance(val, int | float) and not isinstance(val, bool)
        if is_num and val > 0:
            res[key] = float(val) if key == "temp_c" else (int(val) if val == int(val) else val)
        else:
            res[key] = default
    return res


def _write_wheel_steps(config_path: Path, values: dict[str, int | float]) -> None:
    """Patch only the keys inside [wheel_steps] (task U37).

    Mirrors app.py's N3 _write_ntfy_topic / this file's U22 _write_entry_defaults:
    a targeted regex substitution scoped to this one section, never a full TOML
    round-trip.
    """
    text = config_path.read_text()
    section_match = _WHEEL_STEPS_SECTION_RE.search(text)
    if section_match is None:
        raise ValueError("rules_config.toml has no [wheel_steps] table")
    section = section_match.group(0)
    missing = []
    for key in DEFAULT_WHEEL_STEPS:
        if key not in values:
            missing.append(key)
            continue
        literal = _toml_number(values[key])

        def _replace(m: re.Match[str], lit: str = literal) -> str:
            return m.group(1) + lit

        section, count = _WHEEL_STEPS_LINE_RES[key].subn(_replace, section, count=1)
        if count == 0:
            missing.append(key)
    if missing:
        raise ValueError(f"rules_config.toml [wheel_steps] table is missing: {', '.join(missing)}")
    text = text[: section_match.start()] + section + text[section_match.end() :]
    config_path.write_text(text)


def entry_defaults(config_path: Path) -> dict[str, Any]:
    """Read [entry_defaults] from *config_path* (task U22/U27).

    Falls back to the out-of-the-box values (60ml bottle, 20min breast) if
    the table or a key is missing, same "falls back if unset" convention as
    models/timefmt.py's display zone.
    """
    table: object = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            table = tomllib.load(fh).get("entry_defaults", {})
    if not isinstance(table, dict):
        table = {}
    res: dict[str, Any] = {
        "bottle_volume_ml": int(table.get("bottle_volume_ml", DEFAULT_BOTTLE_VOLUME_ML)),
        "breast_duration_min": int(table.get("breast_duration_min", DEFAULT_BREAST_DURATION_MIN)),
    }
    for cat in ("tummy_time", "reading_talking", "sensory_play", "foreign_language"):
        for key in (f"{cat}_duration_min", cat):
            if key in table and table[key] is not None:
                try:
                    res[f"{cat}_duration_min"] = int(table[key])
                    break
                except (ValueError, TypeError):
                    pass
    res["wheel_steps"] = wheel_steps(config_path)
    return res


def activity_targets(config_path: Path) -> dict[str, str]:
    """Read [activity_targets] from *config_path* (task U27/V4)."""
    table: object = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            table = tomllib.load(fh).get("activity_targets", {})
    if not isinstance(table, dict):
        return {}
    return {str(k): str(v) for k, v in table.items() if isinstance(v, str)}


def _write_entry_defaults(
    config_path: Path, bottle_volume_ml: int, breast_duration_min: int
) -> None:
    """Patch only the two keys inside [entry_defaults] (task U22).

    Mirrors app.py's N3 _write_ntfy_topic: a targeted regex substitution
    scoped to this one section, never a full TOML round-trip, so a
    settings-page POST can never reach an architect-owned clinical threshold
    elsewhere in rules_config.toml.
    """
    text = config_path.read_text()
    section_match = _ENTRY_DEFAULTS_SECTION_RE.search(text)
    if section_match is None:
        raise ValueError("rules_config.toml has no [entry_defaults] table")
    section = section_match.group(0)
    section, n1 = _BOTTLE_VOLUME_LINE_RE.subn(
        lambda m: m.group(1) + str(bottle_volume_ml), section, count=1
    )
    section, n2 = _BREAST_DURATION_LINE_RE.subn(
        lambda m: m.group(1) + str(breast_duration_min), section, count=1
    )
    if n1 == 0 or n2 == 0:
        raise ValueError("rules_config.toml [entry_defaults] table is missing a key")
    text = text[: section_match.start()] + section + text[section_match.end() :]
    config_path.write_text(text)


_PROJECTIONS_OVERRIDE_KEYS = ("ml_per_hour", "typical_feed_ml", "mess_interval_min")
_PROJECTIONS_SECTION_RE = re.compile(r"(?ms)^\[projections\]\n(?:(?!^\[).)*")
_PROJECTIONS_LINE_RES = {
    key: re.compile(rf"(?m)^({key}\s*=\s*)[^\n]*") for key in _PROJECTIONS_OVERRIDE_KEYS
}


def projections_overrides(config_path: Path) -> dict[str, float]:
    """Read the three manual overrides from [projections] (task U24).

    Mirrors entry_defaults()'s "falls back if unset" shape, but the fallback
    here is 0 - meaning "compute it" (V3's own _override convention), not an
    out-of-the-box constant.
    """
    table: object = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            table = tomllib.load(fh).get("projections", {})
    if not isinstance(table, dict):
        table = {}
    out: dict[str, float] = {}
    for key in _PROJECTIONS_OVERRIDE_KEYS:
        value = table.get(key, 0)
        is_number = isinstance(value, int | float) and not isinstance(value, bool)
        out[key] = float(value) if is_number and value > 0 else 0.0
    return out


def _write_projections_overrides(config_path: Path, values: dict[str, float]) -> None:
    """Patch only the three override keys inside [projections] (task U24).

    Mirrors app.py's N3 _write_ntfy_topic / this file's U22
    _write_entry_defaults: a targeted regex substitution scoped to this one
    section, never a full TOML round-trip, so a settings-page POST can never
    reach an architect-owned clinical threshold elsewhere in
    rules_config.toml.
    """
    text = config_path.read_text()
    section_match = _PROJECTIONS_SECTION_RE.search(text)
    if section_match is None:
        raise ValueError("rules_config.toml has no [projections] table")
    section = section_match.group(0)
    missing = []
    for key in _PROJECTIONS_OVERRIDE_KEYS:
        literal = _toml_number(values[key])

        def _replace(m: re.Match[str], lit: str = literal) -> str:
            return m.group(1) + lit

        section, count = _PROJECTIONS_LINE_RES[key].subn(_replace, section, count=1)
        if count == 0:
            missing.append(key)
    if missing:
        raise ValueError(f"rules_config.toml [projections] table is missing: {', '.join(missing)}")
    text = text[: section_match.start()] + section + text[section_match.end() :]
    config_path.write_text(text)


def _toml_number(value: float) -> str:
    return str(int(value)) if value == int(value) else repr(float(value))


def _computed_projections(svc: Services) -> dict[str, float | None]:
    """What V3 would compute right now with no override in force (task U24).

    Reuses V3's own sample functions directly rather than re-deriving the
    medians here, so the settings panel can never drift from what the
    projection actually does. api.py cannot import cradle.repos (routers may
    only import models/services, SPEC 3), so this reads through the
    already-built ProjectionService the app wired at startup rather than
    constructing a second repo - the only two attributes it reaches are the
    repo and nothing else touches config or clock.
    """
    repo = svc.projections._repo  # see docstring above for why
    feeds_chrono = list(reversed(repo.list_feeds(limit=FETCH_LIMIT)))
    nappies_chrono = list(reversed(repo.list_nappies(limit=FETCH_LIMIT)))

    rate_samples = _bottle_rate_samples(feeds_chrono)
    rate = statistics.median(rate_samples) if len(rate_samples) >= MIN_SAMPLES else None

    typical_ml = _median_bottle_volume(feeds_chrono)

    mess_gap_h = _gap_median_hours(nappies_chrono)
    mess_interval_min = mess_gap_h * 60 if mess_gap_h is not None else None

    return {
        "ml_per_hour": round(rate, 1) if rate is not None else None,
        "typical_feed_ml": round(typical_ml, 1) if typical_ml is not None else None,
        "mess_interval_min": round(mess_interval_min, 1) if mess_interval_min is not None else None,
    }


def _clamp_projection_override(raw: str | None) -> float | None:
    """Blank clears the override (returns None); a number must be positive.

    A clamp, not a health claim (task U24 notes): rejects non-numeric and
    non-positive input, but never tells a parent a number is too high or too
    low for their baby.
    """
    if raw is None or raw.strip() == "":
        return None
    value = float(raw)
    if value <= 0:
        raise ValueError("projection overrides must be positive")
    return value


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


def _panel_ts(raw: str | None, ref_date: date | None = None) -> datetime | None:
    """Parse the quick-entry panel's <input type=time> value (U18, U43).

    It carries no date of its own, so a provided "HH:MM" is combined with a
    reference date in the *configured* display zone (models/timefmt, task
    U9) - not the server's OS zone, which would risk the wrong calendar day
    near local midnight. *ref_date* defaults to today so ordinary quick-entry
    keeps working unchanged; the history page's day-group "+" (U43) passes
    that day's own date instead, so a bottle logged from a past day's group
    lands under that day, not today's. Blank or unparseable input means "use
    the clock's now", same as ts=None.
    """
    if not raw:
        return None
    try:
        hour, minute = (int(p) for p in raw.split(":"))
    except ValueError:
        return None
    day = ref_date or to_local(datetime.now(UTC)).date()
    return to_utc(datetime(day.year, day.month, day.day, hour, minute))


def _parse_panel_date(raw: str | None) -> date | None:
    """Parse the panel's hidden "date" field (U43): blank/invalid means "today"."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


_PANEL_CLOSE_OOB = '<div id="panel" class="overlay" hx-swap-oob="true"></div>'


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
        # quick_entry.html's Save buttons target #toast, which sits under the
        # #panel overlay (z-index 60, position:fixed) while a panel is open -
        # any toast swapped in there is invisible until the overlay closes.
        # Nothing else closes it, so a successful save looked like it silently
        # did nothing. An out-of-band swap collapses #panel back to its closed
        # (no "open" class, empty) markup regardless of what the primary
        # #toast target is, making the toast visible and the panel close on
        # every quick-entry save in the same response.
        return HTMLResponse(_toast(table, event_id, label) + _PANEL_CLOSE_OOB)
    return RedirectResponse(f"/?logged={table}:{event_id}", status_code=303)


def _milk_err() -> Response:
    return HTMLResponse('<p class="err">That bottle cannot do that.</p>', status_code=400)


def _milk_response(request: Request, label: str) -> Response:
    if request.headers.get("HX-Request"):
        return HTMLResponse(f'<div class="toast">{label}</div>')
    return RedirectResponse("/milk", status_code=303)


def build_api_router(svc: Services) -> APIRouter:
    router = APIRouter()

    def who(request: Request) -> str:
        return device_name(request.cookies.get("device_name"))

    # --------------------------------------------------------- quick actions
    @router.post("/api/feed")
    def post_feed(
        request: Request,
        method: Annotated[str, Form()] = FeedMethod.BREAST_LEFT.value,
        side_left: Annotated[str | None, Form()] = None,
        side_right: Annotated[str | None, Form()] = None,
        duration_min: Annotated[int | None, Form()] = None,
        volume_ml: Annotated[int | None, Form()] = None,
        ts: Annotated[str | None, Form()] = None,
        date: Annotated[str | None, Form()] = None,
    ) -> Response:
        if side_left is not None or side_right is not None:
            if side_left is not None and side_right is not None:
                m = FeedMethod.BREAST_BOTH
            elif side_left is not None:
                m = FeedMethod.BREAST_LEFT
            else:
                m = FeedMethod.BREAST_RIGHT
        else:
            m = FeedMethod(method)
        event_id = svc.logging.log_feed(
            m,
            logged_by=who(request),
            ts=_panel_ts(ts, ref_date=_parse_panel_date(date)),
            duration_min=duration_min,
            volume_ml=volume_ml,
        )
        return _respond(request, "feed", event_id, m.value.replace("_", " "))

    @router.post("/api/nappy")
    def post_nappy(
        request: Request,
        kind: Annotated[str, Form()] = NappyKind.WET.value,
        stool_colour: Annotated[str, Form()] = StoolColour.UNSET.value,
        consistency: Annotated[str, Form()] = StoolConsistency.UNSET.value,
        ts: Annotated[str | None, Form()] = None,
        date: Annotated[str | None, Form()] = None,
    ) -> Response:
        k = NappyKind(kind)
        event_id = svc.logging.log_nappy(
            k,
            StoolColour(stool_colour),
            StoolConsistency(consistency),
            logged_by=who(request),
            ts=_panel_ts(ts, ref_date=_parse_panel_date(date)),
        )
        return _respond(request, "nappy", event_id, f"{k.value} nappy")

    @router.post("/api/activity")
    def post_activity(
        request: Request,
        category: Annotated[str, Form()],
        duration_min: Annotated[str | None, Form()] = None,
        note: Annotated[str, Form()] = "",
        ts: Annotated[str | None, Form()] = None,
        date: Annotated[str | None, Form()] = None,
    ) -> Response:
        cat = ActivityCategory(category)
        dur: int | None = None
        if duration_min is not None and str(duration_min).strip() != "":
            try:
                dur = int(duration_min)
            except ValueError:
                dur = None
        event_id = svc.logging.log_activity(
            cat,
            duration_min=dur,
            note=note,
            logged_by=who(request),
            ts=_panel_ts(ts, ref_date=_parse_panel_date(date)),
        )
        return _respond(request, "activity", event_id, cat.value.replace("_", " "))

    @router.post("/api/sleep/toggle")
    def post_sleep_toggle(request: Request) -> Response:
        was_running = svc.logging.running_sleep() is not None
        event_id = svc.logging.toggle_sleep(logged_by=who(request))
        return _respond(request, "sleep", event_id, "wake" if was_running else "sleep start")

    @router.post("/api/express")
    def post_express(request: Request) -> Response:
        event_id = svc.logging.log_expression(logged_by=who(request))
        return _respond(request, "expression", event_id, "expression")

    # ---------------------------------------------------------- milk stock
    @router.post("/api/milk/store")
    def post_milk_store(
        request: Request,
        store: Annotated[str, Form()],
        colour: Annotated[str, Form()],
        volume_ml: Annotated[int, Form()],
    ) -> Response:
        try:
            svc.milk.store_now(
                MilkStore(store),
                BottleColour(colour),
                volume_ml,
                logged_by=who(request),
            )
        except (ValueError, InvalidBatchTransitionError):
            return HTMLResponse('<p class="err">Check the store and colour.</p>', status_code=400)
        if request.headers.get("HX-Request"):
            return HTMLResponse('<div class="toast">Stored</div>')
        return RedirectResponse("/milk", status_code=303)

    @router.post("/api/milk/thaw")
    def post_milk_thaw(request: Request, batch_id: Annotated[int, Form()]) -> Response:
        try:
            svc.milk.thaw(batch_id)
        except (UnknownBatchError, InvalidBatchTransitionError):
            return _milk_err()
        return _milk_response(request, "Thawed")

    @router.post("/api/milk/open")
    def post_milk_open(request: Request, batch_id: Annotated[int, Form()]) -> Response:
        try:
            svc.milk.open_batch(batch_id)
        except (UnknownBatchError, InvalidBatchTransitionError):
            return _milk_err()
        return _milk_response(request, "Opened")

    @router.post("/api/milk/use")
    def post_milk_use(request: Request, batch_id: Annotated[int, Form()]) -> Response:
        try:
            svc.milk.use(batch_id)
        except (UnknownBatchError, InvalidBatchTransitionError):
            return _milk_err()
        return _milk_response(request, "Used")

    @router.post("/api/milk/discard")
    def post_milk_discard(request: Request, batch_id: Annotated[int, Form()]) -> Response:
        try:
            svc.milk.discard(batch_id)
        except (UnknownBatchError, InvalidBatchTransitionError):
            return _milk_err()
        return _milk_response(request, "Discarded")

    # ------------------------------------------------------- detailed entry
    @router.post("/api/growth")
    def post_growth(
        request: Request,
        measure: Annotated[str, Form()],
        value: Annotated[int, Form()],
        source: Annotated[str, Form()] = "home",
        ts: Annotated[str | None, Form()] = None,
        date: Annotated[str | None, Form()] = None,
    ) -> Response:
        event_id = svc.logging.log_growth(
            GrowthMeasure(measure),
            value,
            source,
            logged_by=who(request),
            ts=_panel_ts(ts, ref_date=_parse_panel_date(date)),
        )
        return _respond(request, "growth", event_id, measure)

    @router.post("/api/temperature")
    def post_temperature(
        request: Request,
        temp_c: Annotated[float, Form()],
        site: Annotated[str, Form()] = "axilla",
        ts: Annotated[str | None, Form()] = None,
        date: Annotated[str | None, Form()] = None,
    ) -> Response:
        event_id = svc.logging.log_temperature(
            temp_c, site, logged_by=who(request), ts=_panel_ts(ts, ref_date=_parse_panel_date(date))
        )
        return _respond(request, "temperature", event_id, f"{temp_c:.1f} C")

    @router.post("/api/milestone")
    def post_milestone(
        request: Request,
        title: Annotated[str, Form()],
        category: Annotated[str, Form()] = "first",
        note: Annotated[str, Form()] = "",
        ts: Annotated[str | None, Form()] = None,
        date: Annotated[str | None, Form()] = None,
    ) -> Response:
        event_id = svc.logging.log_milestone(
            category,
            title,
            note,
            logged_by=who(request),
            ts=_panel_ts(ts, ref_date=_parse_panel_date(date)),
        )
        return _respond(request, "milestone", event_id, "milestone")

    @router.post("/api/note")
    def post_note(
        request: Request,
        text: Annotated[str, Form()],
        tags: Annotated[str, Form()] = "",
        ts: Annotated[str | None, Form()] = None,
        date: Annotated[str | None, Form()] = None,
    ) -> Response:
        parsed = tuple(t.strip() for t in tags.split(",") if t.strip())
        event_id = svc.logging.log_note(
            text, parsed, logged_by=who(request), ts=_panel_ts(ts, ref_date=_parse_panel_date(date))
        )
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

    @router.post("/api/edit-event")
    def post_edit_event(
        request: Request,
        table: Annotated[str, Form()],
        event_id: Annotated[int, Form()],
        ts: Annotated[str | None, Form()] = None,
        date: Annotated[str | None, Form()] = None,
        ts_end: Annotated[str | None, Form()] = None,
        method: Annotated[str | None, Form()] = None,
        duration_min: Annotated[str | None, Form()] = None,
        volume_ml: Annotated[str | None, Form()] = None,
        note: Annotated[str | None, Form()] = None,
        kind: Annotated[str | None, Form()] = None,
        stool_colour: Annotated[str | None, Form()] = None,
        consistency: Annotated[str | None, Form()] = None,
        location: Annotated[str | None, Form()] = None,
        measure: Annotated[str | None, Form()] = None,
        value: Annotated[str | None, Form()] = None,
        source: Annotated[str | None, Form()] = None,
        temp_c: Annotated[str | None, Form()] = None,
        site: Annotated[str | None, Form()] = None,
        category: Annotated[str | None, Form()] = None,
        title: Annotated[str | None, Form()] = None,
        text: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
    ) -> Response:
        """History Edit-panel save (task U43): the many-fields-at-once sibling
        of /api/edit-field. EventsRepo.edit_event enforces the per-table
        column allow-list same as that endpoint; a field outside it - or a
        bad value - raises the same errors caught below. Only fields the
        panel actually submitted are included, so a table's irrelevant
        fields (e.g. a nappy edit never sends "measure") never reach
        edit_event and can't collide with another table's columns.
        """
        raw_fields: dict[str, str] = {}
        for name, val in (
            ("method", method),
            ("duration_min", duration_min),
            ("volume_ml", volume_ml),
            ("note", note),
            ("kind", kind),
            ("stool_colour", stool_colour),
            ("consistency", consistency),
            ("location", location),
            ("measure", measure),
            ("value", value),
            ("source", source),
            ("temp_c", temp_c),
            ("site", site),
            ("category", category),
            ("title", title),
            ("text", text),
        ):
            if val is not None and val != "":
                raw_fields[name] = val
        if tags is not None:
            raw_fields["tags"] = ",".join(t.strip() for t in tags.split(",") if t.strip())

        try:
            fields: dict[str, object] = {
                k: _coerce_field_value(k, v) for k, v in raw_fields.items()
            }
            if ts is not None and ts.strip():
                combined = (
                    to_utc(datetime.fromisoformat(ts))
                    if "T" in ts
                    else _panel_ts(ts, ref_date=_parse_panel_date(date))
                )
                if combined is not None:
                    fields["ts"] = combined
            if ts_end is not None and ts_end.strip():
                fields["ts_end"] = to_utc(datetime.fromisoformat(ts_end))
        except ValueError:
            return HTMLResponse('<div class="toast err">Bad value</div>', status_code=400)

        try:
            svc.logging.edit(table, event_id, fields)
        except (UnknownTableError, UneditableFieldError, ValueError):
            return HTMLResponse('<div class="toast err">Bad value</div>', status_code=400)
        if request.headers.get("HX-Request"):
            return HTMLResponse('<div class="toast">Updated</div>' + _PANEL_CLOSE_OOB)
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
            # The confirm-delete step (U43) posts from inside #panel, so the
            # response both removes the row (matched by id, wherever the
            # underlying page put it) and closes the panel, same as any other
            # panel save.
            row_removed = f'<tr id="{table}-{event_id}" hx-swap-oob="delete"></tr>'
            return HTMLResponse('<div class="toast">Deleted</div>' + row_removed + _PANEL_CLOSE_OOB)
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
        referer = request.headers.get("Referer")
        return RedirectResponse(referer or "/", status_code=303)

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

    @router.get("/api/settings/entry-defaults")
    def get_entry_defaults() -> JSONResponse:
        defs = entry_defaults(CONFIG_PATH)
        return JSONResponse({
            "bottle_volume_ml": defs["bottle_volume_ml"],
            "breast_duration_min": defs["breast_duration_min"],
        })

    @router.post("/api/settings/entry-defaults")
    def post_entry_defaults(
        request: Request,
        bottle_volume_ml: Annotated[int, Form()] = DEFAULT_BOTTLE_VOLUME_ML,
        breast_duration_min: Annotated[int, Form()] = DEFAULT_BREAST_DURATION_MIN,
    ) -> Response:
        if bottle_volume_ml <= 0 or breast_duration_min <= 0:
            msg = '<p class="err">Defaults must be positive numbers.</p>'
            return HTMLResponse(msg, status_code=400)
        _write_entry_defaults(CONFIG_PATH, bottle_volume_ml, breast_duration_min)
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="ok">Defaults saved.</span>')
        return RedirectResponse("/settings", status_code=303)

    @router.get("/api/settings/projections")
    def get_projections_overrides() -> JSONResponse:
        overrides = projections_overrides(CONFIG_PATH)
        computed = _computed_projections(svc)
        return JSONResponse(
            {
                key: {"override": overrides[key], "computed": computed[key]}
                for key in _PROJECTIONS_OVERRIDE_KEYS
            }
        )

    @router.post("/api/settings/projections")
    def post_projections_overrides(
        request: Request,
        ml_per_hour: Annotated[str, Form()] = "",
        typical_feed_ml: Annotated[str, Form()] = "",
        mess_interval_min: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            values = {
                "ml_per_hour": _clamp_projection_override(ml_per_hour) or 0.0,
                "typical_feed_ml": _clamp_projection_override(typical_feed_ml) or 0.0,
                "mess_interval_min": _clamp_projection_override(mess_interval_min) or 0.0,
            }
        except ValueError:
            msg = '<p class="err">Overrides must be blank or a positive number.</p>'
            return HTMLResponse(msg, status_code=400)
        _write_projections_overrides(CONFIG_PATH, values)
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="ok">Projection overrides saved.</span>')
        return RedirectResponse("/settings", status_code=303)

    @router.get("/api/settings/wheel-steps")
    def get_wheel_steps_endpoint() -> JSONResponse:
        return JSONResponse(wheel_steps(CONFIG_PATH))

    @router.post("/api/settings/wheel-steps")
    def post_wheel_steps_endpoint(
        request: Request,
        weight: Annotated[str, Form()] = "25",
        length: Annotated[str, Form()] = "5",
        head_circ: Annotated[str, Form()] = "5",
        temp_c: Annotated[str, Form()] = "0.1",
        bottle_volume_ml: Annotated[str, Form()] = "5",
        breast_duration_min: Annotated[str, Form()] = "5",
        tummy_time_duration_min: Annotated[str, Form()] = "1",
        reading_talking_duration_min: Annotated[str, Form()] = "1",
        sensory_play_duration_min: Annotated[str, Form()] = "1",
        foreign_language_duration_min: Annotated[str, Form()] = "1",
    ) -> Response:
        raw = {
            "weight": weight,
            "length": length,
            "head_circ": head_circ,
            "temp_c": temp_c,
            "bottle_volume_ml": bottle_volume_ml,
            "breast_duration_min": breast_duration_min,
            "tummy_time_duration_min": tummy_time_duration_min,
            "reading_talking_duration_min": reading_talking_duration_min,
            "sensory_play_duration_min": sensory_play_duration_min,
            "foreign_language_duration_min": foreign_language_duration_min,
        }
        values: dict[str, int | float] = {}
        try:
            for k, v in raw.items():
                val = float(v)
                if val <= 0:
                    raise ValueError(f"{k} must be positive")
                values[k] = val if k == "temp_c" else (int(val) if val == int(val) else val)
        except ValueError:
            msg = '<p class="err">Wheel steps must be positive numbers.</p>'
            return HTMLResponse(msg, status_code=400)

        _write_wheel_steps(CONFIG_PATH, values)
        if request.headers.get("HX-Request"):
            return HTMLResponse('<span class="ok">Scroll-wheel increments saved.</span>')
        return RedirectResponse("/settings", status_code=303)

    return router
