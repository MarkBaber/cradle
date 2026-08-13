"""U12: device-name attribution (D7) - a plain cookie that labels rows.

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


def _client(seed_profile: bool = True) -> TestClient:
    db = Path(tempfile.mkdtemp()) / "device.db"
    app = create_app(db_path=db, clock=FixedClock(NOW), config_path=ROOT / "rules_config.toml")
    client = TestClient(app, follow_redirects=False)
    if seed_profile:
        assert client.post("/api/settings/profile", data=PROFILE).status_code == 303
    return client


def test_first_visit_prompts_for_a_device_name_and_persists_it() -> None:
    client = _client(seed_profile=False)
    landing = client.get("/")
    assert landing.status_code == 303
    page = client.get(landing.headers["location"])
    assert 'name="device"' in page.text
    assert "Name this device" in page.text

    r = client.post("/api/settings/device", data={"device": "Mum's phone"})
    assert r.status_code == 303
    assert "device_name" in r.headers["set-cookie"]
    # Still mid-first-run: the profile is not saved yet, so keep the welcome copy.
    assert r.headers["location"] == "/settings?first_run=1"
    # Persisted: the cookie comes back on later requests without being resent by hand.
    assert "Mum&#39;s phone" in client.get("/settings").text


def test_events_logged_after_naming_carry_the_device_name() -> None:
    client = _client()
    before = client.post("/api/feed", data={"method": "breast_left"})
    assert before.status_code == 303

    client.post("/api/settings/device", data={"device": "Dad's phone"})
    assert client.post("/api/nappy", data={"kind": "wet"}).status_code == 303

    rows = client.get("/history").text
    assert "by Dad&#39;s phone" in rows
    # The feed logged before naming keeps its empty attribution - no backfill.
    assert rows.count("by Dad&#39;s phone") == 1


def test_history_renders_who_logged_each_row() -> None:
    client = _client()
    client.post("/api/settings/device", data={"device": "kitchen tablet"})
    client.post("/api/feed", data={"method": "bottle_formula"})
    client.post("/api/nappy", data={"kind": "dirty"})

    for page in ("/history", "/history/fragment"):
        assert client.get(page).text.count("by kitchen tablet") == 2


def test_device_name_is_editable_in_settings() -> None:
    client = _client()
    client.post("/api/settings/device", data={"device": "old name"})
    client.post("/api/settings/device", data={"device": "new name"})

    settings = client.get("/settings").text
    assert 'value="new name"' in settings
    assert "old name" not in settings

    client.post("/api/feed", data={"method": "breast_right"})
    assert "by new name" in client.get("/history").text


def test_naming_a_device_copes_with_awkward_input() -> None:
    client = _client()
    # A cookie is latin-1 on the wire: an emoji must be dropped, not 500 the request.
    assert client.post("/api/settings/device", data={"device": "phone 📱"}).status_code == 303
    assert 'value="phone"' in client.get("/settings").text

    # Control characters are rejected by Set-Cookie too, and a tab must not run the
    # words together the way a dropped character would.
    assert client.post("/api/settings/device", data={"device": "hall\tphone"}).status_code == 303
    assert 'value="hall phone"' in client.get("/settings").text

    # The HTMX fragment is hand-built HTML, so the name has to be escaped into it.
    markup = client.post(
        "/api/settings/device", data={"device": "<b>tv</b>"}, headers={"HX-Request": "true"}
    )
    assert "<b>" not in markup.text
    assert "&lt;b&gt;tv&lt;/b&gt;" in markup.text

    hx = client.post(
        "/api/settings/device", data={"device": " tablet "}, headers={"HX-Request": "true"}
    )
    assert hx.status_code == 200
    # The fragment echoes what was stored, which is not always what was typed.
    assert "saved" in hx.text
    assert "tablet" in hx.text
    client.post("/api/feed", data={"method": "breast_left"})

    # Clearing the name stops attribution without disturbing rows already written.
    assert client.post("/api/settings/device", data={"device": "   "}).status_code == 303
    client.post("/api/nappy", data={"kind": "wet"})
    assert client.get("/history/fragment").text.count('class="by"') == 1
