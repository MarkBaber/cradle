"""C1-C4 at the HTTP boundary."""

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.models import to_local  # noqa: E402
from cradle.ports.clock import FixedClock  # noqa: E402
from cradle.reference.lms import LmsRow, LmsTable  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}


def _table() -> LmsTable:
    rows = [LmsRow(a, 1.0, 3400.0 + 1000.0 * a / 60.0, 0.12) for a in (0, 30, 60)]
    return LmsTable({("weight", "female"): rows}, "synthetic-v1")


def _client() -> TestClient:
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "c4.db",
        clock=FixedClock(NOW),
        config_path=ROOT / "rules_config.toml",
        reference=(_table(), None),
        start_scheduler=False,
    )
    client = TestClient(app, follow_redirects=False)
    client.post("/api/settings/profile", data=PROFILE)
    return client


def test_daily_series_endpoint_shape() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "bottle_formula", "volume_ml": "80"})
    client.post("/api/nappy", data={"kind": "wet"})
    d = client.get("/api/series/daily?days=7").json()
    assert len(d["days"]) == 7
    for key in (
        "feeds",
        "bottle_ml",
        "wet",
        "dirty",
        "sleep_hours",
        "longest_sleep_hours",
        "night_wakings",
    ):
        assert len(d[key]) == 7, key
    assert d["feeds"][-1] == 1
    assert d["bottle_ml"][-1] == 80
    assert d["wet"][-1] == 1


def test_daily_series_clamps_absurd_windows() -> None:
    client = _client()
    assert len(client.get("/api/series/daily?days=99999").json()["days"]) == 120
    assert len(client.get("/api/series/daily?days=nonsense").json()["days"]) == 14


def test_ribbon_endpoint_shape() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    r = client.get("/api/series/ribbon?days=3").json()
    assert len(r["days"]) == 3
    assert r["night_start"] == 19 and r["night_end"] == 7
    today = r["days"][-1]
    assert len(today["feeds"]) == 1
    assert 0 <= today["feeds"][0] <= 24


def test_patterns_page_renders_rows_and_table_fallback() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    page = client.get("/patterns?days=7").text
    assert "Rhythm" in page
    assert "rsleep" in page, "ribbon markup present"
    assert "feeds" in page
    assert "longest" in page, "numbers also available without JavaScript"


def test_patterns_redirects_before_profile() -> None:
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "e.db",
        clock=FixedClock(NOW),
        config_path=ROOT / "rules_config.toml",
        reference=(_table(), None),
        start_scheduler=False,
    )
    assert TestClient(app, follow_redirects=False).get("/patterns").status_code == 303


def test_sleep_crossing_midnight_appears_on_both_rows() -> None:
    client = _client()
    start = to_local(NOW).replace(hour=22, minute=0, second=0, microsecond=0) - timedelta(days=1)
    client.post("/api/sleep/toggle")
    client.post(
        "/api/adjust-time",
        data={"table": "sleep", "event_id": 1, "ts": start.strftime("%Y-%m-%dT%H:%M")},
    )
    client.post("/api/sleep/toggle")  # ends the running sleep at NOW

    r = client.get("/api/series/ribbon?days=3").json()
    spans = [d["sleep"] for d in r["days"]]
    assert any(s and s[0][1] == 24.0 for s in spans), "clipped at midnight"
    assert any(s and s[0][0] == 0.0 for s in spans), "resumed after midnight"


def test_growth_playback_frames_served() -> None:
    """C3: one animation frame per measurement."""
    client = _client()
    for value in (3400, 3500, 3650):
        client.post("/api/growth", data={"measure": "weight", "value": str(value)})
    d = client.get("/api/charts/weight").json()
    assert d["frames"] == [1, 2, 3]
    assert len(d["trajectory"]) == 3


def test_charts_page_includes_daily_totals() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    page = client.get("/charts").text
    assert "Daily totals" in page
    assert "series.js" in page
