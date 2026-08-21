"""T1: the <=2-tap contract, proven at the HTTP boundary.

Skipped by the offline runner when fastapi is unavailable.
"""

import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.models import to_local  # noqa: E402
from cradle.ports.clock import FixedClock  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}

# Every quick action and the single field it needs (the action itself).
QUICK_ACTIONS = [
    ("/api/feed", {"method": "breast_left"}),
    ("/api/feed", {"method": "breast_right"}),
    ("/api/feed", {"method": "bottle_formula"}),
    ("/api/nappy", {"kind": "wet"}),
    ("/api/nappy", {"kind": "dirty"}),
    ("/api/sleep/toggle", {}),
]


def _client(seed_profile: bool = True) -> TestClient:
    db = Path(tempfile.mkdtemp()) / "routes.db"
    app = create_app(db_path=db, clock=FixedClock(NOW), config_path=ROOT / "rules_config.toml")
    client = TestClient(app, follow_redirects=False)
    if seed_profile:
        assert client.post("/api/settings/profile", data=PROFILE).status_code == 303
    return client


def test_fresh_install_redirects_to_settings() -> None:
    client = _client(seed_profile=False)
    r = client.get("/")
    assert r.status_code == 303
    assert "/settings" in r.headers["location"]


def test_second_device_is_prompted_on_an_already_configured_household() -> None:
    """U15: U12 only prompted on first install (the /settings redirect). A
    second phone joining a household that already has a profile must also be
    asked, not silently log rows with an empty logged_by."""
    client = _client()
    page = client.get("/").text
    assert "Name this device" in page
    assert 'href="/settings"' in page


def test_device_prompt_does_not_reappear_once_named() -> None:
    client = _client()
    client.post("/api/settings/device", data={"device": "kitchen tablet"})
    assert "Name this device" not in client.get("/").text


def test_every_quick_action_is_one_request_with_minimal_payload() -> None:
    client = _client()
    for url, payload in QUICK_ACTIONS:
        r = client.post(url, data=payload, headers={"HX-Request": "true"})
        assert r.status_code == 200, f"{url} {payload} -> {r.status_code}"
        assert "Undo" in r.text, f"{url} must offer undo"


def test_quick_actions_work_without_htmx() -> None:
    """No HX-Request header: plain form post must redirect, not 4xx."""
    client = _client()
    for url, payload in QUICK_ACTIONS:
        r = client.post(url, data=payload)
        assert r.status_code == 303, f"{url} broke without JS"


def test_quick_entry_page_renders_after_profile() -> None:
    client = _client()
    r = client.get("/")
    assert r.status_code == 200
    for label in ("Breast", "Bottle", "Wet", "Dirty"):
        assert label in r.text


def test_undo_removes_the_event() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    assert "breast left" in client.get("/history").text
    client.post("/api/undo", data={"table": "feed", "event_id": 1})
    assert "Nothing logged yet" in client.get("/history").text


def test_undo_rejects_unknown_table() -> None:
    client = _client()
    r = client.post(
        "/api/undo", data={"table": "baby", "event_id": 1}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 400


def test_sleep_toggle_flips_label() -> None:
    client = _client()
    assert "Sleep" in client.get("/").text
    client.post("/api/sleep/toggle")
    assert "Wake" in client.get("/").text
    client.post("/api/sleep/toggle")
    assert "Sleep" in client.get("/").text


def test_adjust_time_updates_history() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    r = client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 1, "ts": "2026-07-15T09:30"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "09:30" in client.get("/history").text


def test_adjust_time_rejects_bad_timestamp() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    r = client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 1, "ts": "not-a-time"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 400


def test_history_domain_filter() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    client.post("/api/nappy", data={"kind": "wet"})
    only_nappy = client.get("/history?domain=nappy").text
    assert "breast left" not in only_nappy, "filter leaked a feed row"
    assert "wet" in only_nappy


def test_naive_adjust_time_does_not_corrupt_history_ordering() -> None:
    """datetime-local input is naive; mixing it with aware rows must not break sorting."""
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    client.post("/api/nappy", data={"kind": "wet"})
    client.post("/api/adjust-time", data={"table": "feed", "event_id": 1, "ts": "2026-07-15T09:30"})
    assert client.get("/history").status_code == 200


def test_edit_field_sets_bottle_volume_after_the_fact() -> None:
    """U10: volume_ml is a post-hoc edit, so /today and the bottle_ml series stay
    zero until it's set, then pick it up once it is."""
    client = _client()
    client.post("/api/feed", data={"method": "bottle_formula"})
    assert "Bottle intake" not in client.get("/today").text
    r = client.post(
        "/api/edit-field",
        data={"table": "feed", "event_id": 1, "field": "volume_ml", "value": "90"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "90 ml" in client.get("/history").text
    assert "Bottle intake in the last 24h: 90 ml" in client.get("/today").text
    series = client.get("/api/series/daily").json()
    assert 90 in series["bottle_ml"]


def test_edit_field_sets_breast_feed_duration() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    r = client.post(
        "/api/edit-field",
        data={"table": "feed", "event_id": 1, "field": "duration_min", "value": "12"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "12 min" in client.get("/history").text


def test_edit_field_sets_sleep_wake_time_and_updates_totals() -> None:
    client = _client()
    client.post("/api/sleep/toggle")
    client.post(
        "/api/adjust-time", data={"table": "sleep", "event_id": 1, "ts": "2026-07-15T10:00"}
    )
    r = client.post(
        "/api/edit-field",
        data={"table": "sleep", "event_id": 1, "field": "ts_end", "value": "2026-07-15T11:00"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "slept 60 min" in client.get("/history").text
    strip = client.get("/today/fragment").text
    assert "1h0m" in strip and "slept / 24h" in strip


def test_edit_field_rejects_column_outside_allow_list() -> None:
    """logged_by is a real column on `feed` but not in EDITABLE - the allow-list
    is what stands between a form post and an arbitrary column write (U10 notes)."""
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    r = client.post(
        "/api/edit-field",
        data={"table": "feed", "event_id": 1, "field": "logged_by", "value": "hacker"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 400


def test_edit_field_rejects_unknown_table() -> None:
    client = _client()
    r = client.post(
        "/api/edit-field",
        data={"table": "baby", "event_id": 1, "field": "ts", "value": "2026-07-15T09:00"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 400


def test_edit_field_rejects_bad_value() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    r = client.post(
        "/api/edit-field",
        data={"table": "feed", "event_id": 1, "field": "volume_ml", "value": "not-a-number"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 400


def test_profile_rejects_bad_date() -> None:
    client = _client()
    bad = dict(PROFILE, dob="not-a-date")
    assert client.post("/api/settings/profile", data=bad).status_code == 400


# --------------------------------------------------------------------- U18


PANEL_TILES = [
    ("feed", "method", "breast_left"),
    ("feed", "method", "breast_right"),
    ("feed", "method", "bottle_expressed"),
    ("nappy", "kind", "wet"),
    ("nappy", "kind", "dirty"),
]


def test_each_tile_opens_a_panel_with_its_choice_preselected() -> None:
    client = _client()
    for panel, param, value in PANEL_TILES:
        r = client.get(f"/?panel={panel}&{param}={value}")
        assert r.status_code == 200, f"{panel}/{value} -> {r.status_code}"
        if value in ("breast_left", "breast_right"):
            # U22: side is a pair of mutually-exclusive toggle buttons (radio
            # inputs styled as buttons), not a <select> - the checked radio
            # is the preselected side.
            radio_id = "side_left" if value == "breast_left" else "side_right"
            match = re.search(rf'id="{radio_id}"[^>]*>', r.text)
            assert match is not None, f"radio {radio_id} not found"
            assert "checked" in match.group(0), f"{value} not preselected"
        elif panel == "feed":
            assert f'value="{value}" selected' in r.text, f"{value} not preselected"
        else:  # nappy kind is a hidden field, not a select - the panel shape itself is the choice
            assert f'name="kind" value="{value}"' in r.text
    dirty = client.get("/?panel=nappy&kind=dirty").text
    assert 'name="stool_colour"' in dirty
    wet = client.get("/?panel=nappy&kind=wet").text
    assert "stool_colour" not in wet


def test_bottle_tile_now_defaults_to_expressed_not_formula() -> None:
    """The tile used to post bottle_formula - wrong for a household that expresses."""
    client = _client()
    page = client.get("/").text
    assert "method=bottle_expressed" in page
    assert "bottle_formula" not in page


def test_breast_panel_save_logs_side_and_duration_end_to_end() -> None:
    client = _client()
    client.get("/?panel=feed&method=breast_right")  # tap 1: open
    r = client.post(
        "/api/feed",
        data={"method": "breast_right", "duration_min": "14"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    hist = client.get("/history").text
    assert "breast right" in hist
    assert "14 min" in hist


def test_bottle_panel_save_logs_volume_ml_end_to_end() -> None:
    client = _client()
    client.get("/?panel=feed&method=bottle_expressed")  # tap 1: open
    r = client.post(
        "/api/feed",
        data={"method": "bottle_expressed", "volume_ml": "80"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    hist = client.get("/history").text
    assert "bottle expressed" in hist
    assert "80 ml" in hist


def test_bottle_volume_from_panel_appears_in_today_and_series() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "bottle_expressed", "volume_ml": "90"})
    assert "Bottle intake in the last 24h: 90 ml" in client.get("/today").text
    series = client.get("/api/series/daily").json()
    assert 90 in series["bottle_ml"]


def test_dirty_panel_save_logs_stool_colour_end_to_end() -> None:
    client = _client()
    client.get("/?panel=nappy&kind=dirty")  # tap 1: open
    r = client.post(
        "/api/nappy",
        data={"kind": "dirty", "stool_colour": "green"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "dirty (green)" in client.get("/history").text


def test_stool_colour_alert_fires_from_a_colour_entered_through_the_ui() -> None:
    """Posted via /api/nappy exactly as the Dirty panel's Save does - not a
    synthetic FactSet fixture."""
    client = _client()
    client.post("/api/nappy", data={"kind": "dirty", "stool_colour": "red"})
    raised = client.app.state.services.alerts.sweep()
    assert raised >= 1
    pinned = client.app.state.services.alerts.pinned()
    assert any(f.rule_id == "STOOL_COLOUR" for f in pinned)


def test_accepting_every_default_is_two_taps_and_writes_a_row_at_now() -> None:
    client = _client()
    r1 = client.get("/?panel=feed&method=breast_left")  # tap 1
    assert r1.status_code == 200
    r2 = client.post("/api/feed", data={"method": "breast_left"}, headers={"HX-Request": "true"})
    assert r2.status_code == 200  # tap 2
    feeds = client.app.state.services.logging.recent_feeds(1)
    assert feeds[0].ts == NOW


PANEL_SAVE_WITH_BLANK_OPTIONAL_FIELDS = [
    ("/api/feed", {"method": "breast_left", "duration_min": "", "ts": ""}),
    ("/api/feed", {"method": "breast_right", "duration_min": "", "ts": ""}),
    ("/api/feed", {"method": "bottle_expressed", "volume_ml": "", "ts": ""}),
    ("/api/nappy", {"kind": "wet", "ts": ""}),
    ("/api/nappy", {"kind": "dirty", "stool_colour": "unset", "ts": ""}),
]


def test_panel_time_field_combines_with_todays_display_zone_date() -> None:
    """ts carries no date (native <input type=time> only, per this task's notes);
    the server must attach today's date in the *configured* display zone
    (models/timefmt, task U9), not the process's OS zone."""
    client = _client()
    client.post("/api/feed", data={"method": "breast_left", "ts": "09:30"})
    feeds = client.app.state.services.logging.recent_feeds(1)
    local = to_local(feeds[0].ts)
    assert (local.hour, local.minute) == (9, 30)
    assert local.date() == to_local(datetime.now(UTC)).date()


def test_panel_save_with_every_optional_field_blank_never_4xxs() -> None:
    """A browser submits blank <input> fields as empty strings, not as missing
    fields - Save with every default accepted must still never 4xx (with or
    without htmx)."""
    client = _client()
    for url, payload in PANEL_SAVE_WITH_BLANK_OPTIONAL_FIELDS:
        r = client.post(url, data=payload, headers={"HX-Request": "true"})
        assert r.status_code < 400, f"{url} {payload} -> {r.status_code}"
        r2 = client.post(url, data=payload)
        assert r2.status_code < 400, f"{url} {payload} (no JS) -> {r2.status_code}"


# --------------------------------------------------------------------- U22


def test_bottle_panel_prefills_the_configured_volume_default() -> None:
    """Out of the box, rules_config.toml's [entry_defaults] ships 60ml - never
    an entry gate (T1), just a starting value the parent can still change."""
    client = _client()
    page = client.get("/?panel=feed&method=bottle_expressed").text
    match = re.search(r'name="volume_ml"[^>]*>', page)
    assert match is not None
    assert 'value="60"' in match.group(0)


def test_breast_panel_prefills_the_configured_duration_default() -> None:
    client = _client()
    page = client.get("/?panel=feed&method=breast_left").text
    match = re.search(r'name="duration_min"[^>]*>', page)
    assert match is not None
    assert 'value="20"' in match.group(0)


def test_every_panel_time_field_prefills_with_now_in_the_display_zone() -> None:
    """Accepting every default really does write 'now' (U18's 2-tap criterion)
    only if the time field itself already shows now - under FixedClock, in
    the configured display zone (U9), not UTC or the server's OS zone."""
    client = _client()
    expected = to_local(NOW).strftime("%H:%M")
    for panel, param, value in PANEL_TILES:
        page = client.get(f"/?panel={panel}&{param}={value}").text
        match = re.search(r'name="ts"[^>]*>', page)
        assert match is not None, f"{panel}/{value} has no time field"
        assert f'value="{expected}"' in match.group(0), f"{panel}/{value} time not prefilled"


def test_panels_render_as_a_modal_overlay_with_a_dimmed_backdrop() -> None:
    """U22 (4): panels used to swap into #panel in the normal page flow below
    the grid; they must now be a centred overlay with a dimmed backdrop,
    closable without saving (U16 abandoned-panel contract), and this must all
    be true of the server-rendered HTML alone - no JS runs in this client."""
    client = _client()
    closed = client.get("/").text
    assert '<div id="panel" class="overlay">' in closed
    assert "overlay-backdrop" not in closed
    open_page = client.get("/?panel=nappy&kind=wet").text
    assert '<div id="panel" class="overlay open">' in open_page
    assert 'class="overlay-backdrop"' in open_page
    assert 'class="modal"' in open_page
    # closable without saving: the close affordances are plain links back to
    # "/", not a JS handler, and a plain GET there writes no row.
    assert 'class="modal-close" href="/"' in open_page
    assert 'class="overlay-backdrop" href="/"' in open_page


def test_time_field_is_upgraded_by_the_vendored_combined_picker() -> None:
    """The picker is a vendored library (task U29: AnyPicker/jQuery, replacing
    U22's wheel-picker) over the real <input type=time name=ts> (U19's
    constraints, carried through U22 and now U29): with the script unloaded -
    true of this client, which never executes JS - the native control alone
    must still post a value /api/feed already accepts."""
    client = _client()
    page = client.get("/?panel=feed&method=breast_left").text
    assert '<script src="/static/vendor/jquery.min.js" defer></script>' in page
    assert '<script src="/static/vendor/anypicker.min.js" defer></script>' in page
    assert '<script src="/static/entry.js" defer></script>' in page
    assert '<link rel="stylesheet" href="/static/vendor/anypicker-all.min.css">' in page
    assert '<input type="time" name="ts"' in page
    r = client.post("/api/feed", data={"method": "breast_left", "ts": "08:15"})
    assert r.status_code == 303
    feeds = client.app.state.services.logging.recent_feeds(1)
    local = to_local(feeds[0].ts)
    assert (local.hour, local.minute) == (8, 15)


# --------------------------------------------------------------------- U29


def test_panel_save_can_persist_a_picked_date_other_than_today() -> None:
    """The panel's ts field still posts only "HH:MM" (api.py's _panel_ts is
    unchanged, per this task's own contract), so a picked date reaches the
    server via a follow-up /api/adjust-time call once the new event's id is
    known from the create toast's data-table/data-event-id (api.py's
    _toast()) - exactly what entry.js's onSetOutput -> submit -> htmx:afterSwap
    chain drives client-side. This test performs that same two-step flow
    directly, proving Save can end up on a date other than today, and that it
    round-trips through /api/adjust-time's existing, unchanged
    datetime.fromisoformat parsing."""
    client = _client()
    r = client.post(
        "/api/feed", data={"method": "breast_left", "ts": "23:15"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    match = re.search(r'data-table="([^"]+)" data-event-id="(\d+)"', r.text)
    assert match is not None, "create toast is missing data-table/data-event-id"
    table, event_id = match.group(1), match.group(2)
    assert table == "feed"

    r2 = client.post(
        "/api/adjust-time",
        data={"table": table, "event_id": event_id, "ts": "2026-07-10 23:15"},
    )
    assert r2.status_code == 303

    feeds = client.app.state.services.logging.recent_feeds(1)
    local = to_local(feeds[0].ts)
    assert local.date().isoformat() == "2026-07-10", "Save must land on a date other than today"
    assert (local.hour, local.minute) == (23, 15)


def test_history_edit_field_uses_the_vendored_combined_picker() -> None:
    """/history's inline-edit timestamp field uses the same picker the panel
    does. history.html/base.html have no touchable head-block for it, so
    _history_table.html loads the vendor scripts itself (still functions
    without a <head>: defer only cares about DOM-parse completion, not
    document position)."""
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    page = client.get("/history").text
    assert '<script src="/static/vendor/jquery.min.js" defer></script>' in page
    assert '<script src="/static/vendor/anypicker.min.js" defer></script>' in page
    assert '<script src="/static/entry.js" defer></script>' in page
    assert '<link rel="stylesheet" href="/static/vendor/anypicker-all.min.css">' in page
    assert '<input type="datetime-local" name="ts"' in page


def test_history_edit_corrects_both_date_and_time() -> None:
    """A corrected date+time (not just a corrected time-of-day, already
    covered by test_adjust_time_updates_history) round-trips through
    /history's combined picker's underlying route, unchanged."""
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    r = client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 1, "ts": "2026-07-10T06:45"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "10 Jul 06:45" in client.get("/history").text


def test_panel_and_history_submit_correct_timestamps_with_picker_script_disabled() -> None:
    """U19's and U22's no-JS contract, carried forward by U29 for both
    surfaces the combined picker now touches: TestClient never executes JS,
    so this - like every other test in this file - proves the native
    fallback controls alone still post a timestamp the server accepts."""
    client = _client()
    r = client.post("/api/feed", data={"method": "breast_left", "ts": "08:15"})
    assert r.status_code == 303
    feeds = client.app.state.services.logging.recent_feeds(1)
    local = to_local(feeds[0].ts)
    assert (local.hour, local.minute) == (8, 15)

    r2 = client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 1, "ts": "2026-07-14T21:00"},
    )
    assert r2.status_code == 303
    assert "14 Jul 21:00" in client.get("/history").text


def test_breast_panel_side_toggle_buttons_are_mutually_exclusive() -> None:
    """L and R are toggle buttons (radio inputs styled as buttons), not a
    <select>: exactly one carries the active class matching open_method, the
    other carries the dimmed/inactive class."""
    client = _client()

    left_open = client.get("/?panel=feed&method=breast_left").text
    left_label = re.search(r'<label for="side_left"[^>]*>', left_open)
    right_label = re.search(r'<label for="side_right"[^>]*>', left_open)
    assert left_label is not None and right_label is not None
    assert "active" in left_label.group(0) and "inactive" not in left_label.group(0)
    assert "inactive" in right_label.group(0)

    right_open = client.get("/?panel=feed&method=breast_right").text
    left_label2 = re.search(r'<label for="side_left"[^>]*>', right_open)
    right_label2 = re.search(r'<label for="side_right"[^>]*>', right_open)
    assert left_label2 is not None and right_label2 is not None
    assert "inactive" in left_label2.group(0)
    assert "active" in right_label2.group(0) and "inactive" not in right_label2.group(0)

    r = client.post(
        "/api/feed",
        data={"method": "breast_right", "duration_min": "10"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "breast right" in client.get("/history").text


def test_breast_is_a_single_tile() -> None:
    """U22 (6): the Feed grid had two tiles (Breast left, Breast right); they
    are now one tile offering L/R inside the panel instead."""
    page = _client().get("/").text
    assert page.count("<span>Breast</span>") == 1
    assert "Breast left" not in page
    assert "Breast right" not in page


# --------------------------------------------------------------------- U28


MORE_PANEL_TILES = ["growth", "temperature", "milestone", "note"]

# Each of the four "More" panels' endpoint plus the minimal valid payload
# that its Save button submits today (verbatim field set, unchanged by U28).
MORE_PANEL_SAVE = {
    "growth": ("/api/growth", {"measure": "weight", "value": "3600", "source": "home"}),
    "temperature": ("/api/temperature", {"temp_c": "36.9"}),
    "milestone": ("/api/milestone", {"category": "first", "title": "First smile"}),
    "note": ("/api/note", {"text": "Slept well tonight"}),
}


def test_more_details_element_is_gone_and_each_more_panel_opens_as_a_modal_overlay() -> None:
    """U28 (1): the old <details class="more"> inline-forms block is deleted
    entirely, and growth/temperature/milestone/note now open as the same
    centred modal overlay the feed/nappy panels already use (U22 shape)."""
    client = _client()
    closed = client.get("/").text
    assert "<details class=\"more\">" not in closed
    assert "More: weight, temperature, milestone, note" not in closed
    for panel in MORE_PANEL_TILES:
        page = client.get(f"/?panel={panel}").text
        assert "<details class=\"more\">" not in page
        assert "More: weight, temperature, milestone, note" not in page
        assert '<div id="panel" class="overlay open">' in page, f"{panel} did not open a panel"
        assert 'class="overlay-backdrop"' in page, f"{panel} missing backdrop"
        assert 'class="modal"' in page, f"{panel} missing modal"
        assert 'class="modal-close" href="/"' in page, f"{panel} missing close link"
        assert 'class="overlay-backdrop" href="/"' in page, f"{panel} backdrop not closable"


def test_each_more_panel_save_persists_via_its_existing_endpoint_end_to_end() -> None:
    """U28 (2): Save on each of the four new tiles still posts to the same
    /api/... endpoint with the same field set the old inline forms used."""
    client = _client()
    for panel in MORE_PANEL_TILES:
        client.get(f"/?panel={panel}")  # tap 1: open
        url, payload = MORE_PANEL_SAVE[panel]
        r = client.post(url, data=payload, headers={"HX-Request": "true"})  # tap 2: save
        assert r.status_code == 200, f"{panel} save -> {r.status_code}"
    hist = client.get("/history").text
    assert "weight 3600g (home)" in hist
    assert "36.9 C (axilla)" in hist
    assert "first: First smile" in hist
    assert "Slept well tonight" in hist


def test_closing_a_more_panel_without_saving_writes_no_row() -> None:
    """U16 abandoned-panel contract, applied to the four new tiles: opening a
    panel via GET and then simply navigating back (a plain GET to "/",
    exactly what the close affordances do) must not write anything."""
    for panel in MORE_PANEL_TILES:
        client = _client()
        client.get(f"/?panel={panel}")  # open
        client.get("/")  # close, no save
        hist = client.get(f"/history?domain={panel}").text
        assert "Nothing logged yet" in hist, f"{panel} wrote a row despite no save"


def test_every_more_panel_stays_reachable_and_submittable_with_js_disabled() -> None:
    """U18 no-JS fallback, applied to the four new tiles: the tile's plain
    href must open the panel via an ordinary GET, and the panel's plain
    <form method="post"> must still redirect on submit (no HX-Request)."""
    client = _client()
    for panel in MORE_PANEL_TILES:
        r = client.get(f"/?panel={panel}")
        assert r.status_code == 200, f"{panel} tile fallback -> {r.status_code}"
        url, payload = MORE_PANEL_SAVE[panel]
        r2 = client.post(url, data=payload)
        assert r2.status_code == 303, f"{panel} save without JS -> {r2.status_code}"
