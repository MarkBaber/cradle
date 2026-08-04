"""MS1/X1/W1 at the HTTP boundary."""

import json
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
PROFILE = {"name": "Test", "sex": "female", "dob": "2026-05-01",
           "due_date": "2026-05-01", "birth_weight_g": 3400}


def _client() -> TestClient:
    app = create_app(db_path=Path(tempfile.mkdtemp()) / "p5.db", clock=FixedClock(NOW),
                     config_path=ROOT / "rules_config.toml",
                     reference=(None, "R2 outstanding"), start_scheduler=False)
    client = TestClient(app, follow_redirects=False)
    client.post("/api/settings/profile", data=PROFILE)
    return client


def test_milestones_page_lists_entries_with_context() -> None:
    client = _client()
    client.post("/api/milestone", data={"category": "social", "title": "First smile"})
    page = client.get("/milestones").text
    assert "First smile" in page
    assert "weeks" in page, "typical-window context shown"
    assert "not a concern in itself" in page


def test_milestones_redirect_before_profile() -> None:
    app = create_app(db_path=Path(tempfile.mkdtemp()) / "q.db", clock=FixedClock(NOW),
                     config_path=ROOT / "rules_config.toml",
                     reference=(None, "x"), start_scheduler=False)
    r = TestClient(app, follow_redirects=False).get("/milestones")
    assert r.status_code == 303


def test_json_export_downloads_and_parses() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    r = client.get("/export/cradle.json")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    data = json.loads(r.text)
    assert data["baby"]["name"] == "Test"
    assert len(data["events"]["feed"]) == 1


def test_csv_export_per_domain() -> None:
    client = _client()
    client.post("/api/nappy", data={"kind": "wet"})
    r = client.get("/export/nappy.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("id,")


def test_unknown_export_domain_is_404() -> None:
    assert _client().get("/export/sqlite_master.csv").status_code == 404


def test_manifest_is_served_and_valid() -> None:
    client = _client()
    r = client.get("/static/manifest.json")
    assert r.status_code == 200
    m = json.loads(r.text)
    assert m["start_url"] == "/"
    assert m["display"] == "standalone"
    assert m["icons"]


def test_service_worker_served_from_root_with_scope_header() -> None:
    """Scope is capped by the worker's own path, so /static/sw.js would not do."""
    client = _client()
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers.get("Service-Worker-Allowed") == "/"
    assert "javascript" in r.headers["content-type"]


def test_pages_link_manifest_and_register_worker() -> None:
    page = _client().get("/").text
    assert 'rel="manifest"' in page
    assert "/static/pwa.js" in page
