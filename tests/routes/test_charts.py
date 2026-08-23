"""U5: charts page and series endpoint, with and without reference data."""

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
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


def _client(with_reference: bool = True) -> TestClient:
    reference = (_table(), None) if with_reference else (None, "task R2 outstanding")
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "c.db",
        clock=FixedClock(NOW),
        config_path=ROOT / "rules_config.toml",
        reference=reference,
    )
    client = TestClient(app, follow_redirects=False)
    client.post("/api/settings/profile", data=PROFILE)
    return client


def test_charts_page_renders() -> None:
    client = _client()
    client.post("/api/growth", data={"measure": "weight", "value": 3500})
    r = client.get("/charts")
    assert r.status_code == 200
    assert "Growth" in r.text
    assert "3500g" in r.text, "table fallback must show the raw measurement"
    assert "day 14" in r.text and "15 Jul 2026" in r.text, (
        "row must show the real calendar date next to the age-in-days count"
    )


def test_charts_page_empty_history_renders_without_error() -> None:
    client = _client()
    r = client.get("/charts")
    assert r.status_code == 200
    assert "No weight logged yet." in r.text


def test_series_endpoint_returns_curves_and_trajectory() -> None:
    client = _client()
    client.post("/api/growth", data={"measure": "weight", "value": 3500})
    d = client.get("/api/charts/weight").json()
    assert d["unavailable_reason"] is None
    assert set(d["curves"]) == {"0.4", "2", "9", "25", "50", "75", "91", "98", "99.6"}
    assert d["trajectory"] == [[14, 3500.0, "15 Jul 2026"]]
    assert d["frames"] == [1]
    assert all(len(v) == len(d["ages"]) for v in d["curves"].values())


def test_unknown_measure_is_404() -> None:
    assert _client().get("/api/charts/wingspan").status_code == 404


def test_missing_reference_data_is_surfaced_not_faked() -> None:
    client = _client(with_reference=False)
    client.post("/api/growth", data={"measure": "weight", "value": 3500})
    d = client.get("/api/charts/weight").json()
    assert d["curves"] == {}, "no reference data must mean no curves, not fake ones"
    assert "R2" in d["unavailable_reason"]
    page = client.get("/charts")
    assert "unavailable" in page.text
    assert "3500g" in page.text, "logged data still shown"


def test_charts_redirect_before_profile_exists() -> None:
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "d.db",
        clock=FixedClock(NOW),
        config_path=ROOT / "rules_config.toml",
        reference=(_table(), None),
    )
    client = TestClient(app, follow_redirects=False)
    r = client.get("/charts")
    assert r.status_code == 303 and "/settings" in r.headers["location"]


def test_weight_loss_shown_on_page() -> None:
    client = _client()
    client.post("/api/growth", data={"measure": "weight", "value": 3060})
    assert "10.0% below birth weight" in client.get("/charts").text


def test_charts_renders_four_separate_chart_containers() -> None:
    """C6: /charts renders feeds, wet, dirty, and sleep chart containers."""
    client = _client()
    r = client.get("/charts")
    assert r.status_code == 200
    for container_id in ("feedschart", "wetchart", "dirtychart", "sleeptargetchart"):
        assert f'id="{container_id}"' in r.text
    assert 'id="dailychart"' not in r.text


def test_daily_series_api_includes_age_days_and_targets() -> None:
    """C6: GET /api/series/daily includes age_days and targets dict."""
    client = _client()
    r = client.get("/api/series/daily?days=7")
    assert r.status_code == 200
    d = r.json()
    assert "age_days" in d
    assert len(d["age_days"]) == 7
    assert "targets" in d
    targets = d["targets"]
    for key in (
        "feed_volume_ml",
        "wet_min",
        "wet_max",
        "dirty_min",
        "dirty_max",
        "sleep_min_hours",
        "sleep_max_hours",
    ):
        assert key in targets
        assert len(targets[key]) == 7


def test_none_target_entries_are_none_not_zero() -> None:
    """C6: None target entries render as None/null in JSON contract, not misleading zero."""
    client = _client()
    r = client.get("/api/series/daily?days=7")
    d = r.json()
    # Before any weight is logged, feed_volume_ml target must be None
    assert all(val is None for val in d["targets"]["feed_volume_ml"])
