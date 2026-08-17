"""T1: the <=2-tap contract, proven at the HTTP boundary.

Skipped by the offline runner when fastapi is unavailable.
"""

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
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
    for label in ("Breast left", "Bottle", "Wet", "Dirty"):
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
