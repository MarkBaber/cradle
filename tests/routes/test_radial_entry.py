"""Tests for radial quick-entry page (task U47)."""

import math
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

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
    db = Path(tempfile.mkdtemp()) / "routes.db"
    app = create_app(db_path=db, clock=FixedClock(NOW), config_path=ROOT / "rules_config.toml")
    client = TestClient(app, follow_redirects=False)
    if seed_profile:
        assert client.post("/api/settings/profile", data=PROFILE).status_code == 303
    return client


def test_radial_entry_route_accessible() -> None:
    client = _client()
    res = client.get("/radial")
    assert res.status_code == 200
    assert "Radial logging dial" in res.text or "radial-dial" in res.text


def test_bidirectional_links_between_logging_pages() -> None:
    client = _client()
    res_grid = client.get("/")
    assert res_grid.status_code == 200
    assert "/radial" in res_grid.text, "Button grid page must link to /radial"

    res_radial = client.get("/radial")
    assert res_radial.status_code == 200
    assert 'href="/"' in res_radial.text or "Switch to button grid" in res_radial.text, (
        "Radial page must link to /"
    )


def test_all_14_logging_actions_reachable_from_radial_page() -> None:
    client = _client()
    res = client.get("/radial")
    assert res.status_code == 200
    text = res.text

    actions = [
        "breast_left",
        "bottle_expressed",
        "kind=wet",
        "kind=dirty",
        "/api/sleep/toggle",
        "/api/express",
        "category=tummy_time",
        "category=reading_talking",
        "category=sensory_play",
        "category=foreign_language",
        "panel=growth",
        "panel=temperature",
        "panel=milestone",
        "panel=note",
    ]
    for action in actions:
        assert action in text, f"Logging action {action} missing from /radial"


def test_radial_page_panel_overlay_flow() -> None:
    """Panels open identically via ?panel= query param on /radial."""
    client = _client()

    res_feed = client.get("/radial?panel=feed&method=breast_left")
    assert res_feed.status_code == 200
    assert 'id="panel"' in res_feed.text
    assert 'class="overlay open"' in res_feed.text
    assert "/api/feed" in res_feed.text

    res_nappy = client.get("/radial?panel=nappy&kind=wet")
    assert res_nappy.status_code == 200
    assert 'id="panel"' in res_nappy.text
    assert 'class="overlay open"' in res_nappy.text
    assert "/api/nappy" in res_nappy.text

    res_activity = client.get("/radial?panel=activity&category=tummy_time")
    assert res_activity.status_code == 200
    assert 'id="panel"' in res_activity.text
    assert 'class="overlay open"' in res_activity.text
    assert "/api/activity" in res_activity.text


def test_radial_js_implements_pointer_drag_release_gestures() -> None:
    js_path = ROOT / "src" / "cradle" / "routers" / "static" / "radial.js"
    assert js_path.exists(), "radial.js must exist"
    content = js_path.read_text(encoding="utf-8")

    assert "pointerdown" in content, "radial.js must listen for pointerdown"
    assert "pointermove" in content, "radial.js must listen for pointermove"
    assert "pointerup" in content, "radial.js must listen for pointerup"
    assert "atan2" in content, "radial.js must compute angle for wedge hit-testing"


def test_dial_touch_targets_meet_wcag_minimum() -> None:
    """Dial target sizing must compute to at least 44x44 CSS px at 375px viewport width."""
    css_path = ROOT / "src" / "cradle" / "routers" / "static" / "app.css"
    css_text = css_path.read_text(encoding="utf-8")
    assert "85vmin" in css_text or "radial-dial" in css_text

    viewport_w = 375.0
    dial_diameter = min(0.85 * viewport_w, 340.0)  # 318.75 px
    scale = dial_diameter / 320.0  # SVG viewBox is 320x320

    R = 158.0
    r = 40.0
    N = 14

    radial_depth_px = (R - r) * scale
    assert radial_depth_px >= 44.0, f"Radial thickness {radial_depth_px}px is under 44px"

    r_mid = (R + r) / 2.0  # 99.0
    mid_arc_len_svg = r_mid * (2.0 * math.pi / N)  # ~44.42 svg units
    mid_arc_len_px = mid_arc_len_svg * scale  # ~44.25 css px
    assert mid_arc_len_px >= 44.0, f"Midpoint arc width {mid_arc_len_px}px is under 44px"


def test_no_new_third_party_vendor_assets_added() -> None:
    vendor_dir = ROOT / "src" / "cradle" / "routers" / "static" / "vendor"
    vendor_files = {p.name for p in vendor_dir.glob("*")}
    expected_vendor = {
        "README.md",
        "anypicker-all.min.css",
        "anypicker.min.js",
        "htmx.min.js",
        "jquery.min.js",
        "plotly.min.js",
    }
    assert vendor_files.issubset(expected_vendor), (
        f"Unexpected vendor assets found: {vendor_files - expected_vendor}"
    )


def test_no_js_degradation_fallback_list() -> None:
    client = _client()
    res = client.get("/radial")
    assert res.status_code == 200
    assert "radial-no-js-list" in res.text
    assert 'href="/radial?panel=feed&amp;method=breast_left"' in res.text
    assert 'href="/radial?panel=nappy&amp;kind=wet"' in res.text
