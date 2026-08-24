"""Route tests for task U43: History and Summary merged into one page.

U40's day-grouped layout is now /history's only view; /summary and
/day-summary are thin back-compat redirects to it. Covers the redirects, the
single tabbar entry, day-grouping order, and the day-grouped rows rendering
across every domain. Edit/Clone/Delete/Add panel behaviour lives in
test_quick_entry.py alongside the rest of the #panel system it reuses.
"""

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
        db_path=Path(tempfile.mkdtemp()) / "u43.db",
        clock=FixedClock(NOW),
        config_path=ROOT / "rules_config.toml",
        reference=(_table(), None),
        start_scheduler=False,
    )
    client = TestClient(app, follow_redirects=False)
    client.post("/api/settings/profile", data=PROFILE)
    return client


def test_history_redirects_when_no_profile() -> None:
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "no_profile.db",
        clock=FixedClock(NOW),
        config_path=ROOT / "rules_config.toml",
        reference=(_table(), None),
        start_scheduler=False,
    )
    client = TestClient(app, follow_redirects=False)
    res = client.get("/history")
    assert res.status_code == 303
    assert res.headers["location"] == "/settings?first_run=1"


def test_day_summary_and_summary_redirect_to_history() -> None:
    client = _client()
    for path in ("/day-summary", "/summary"):
        res = client.get(path)
        assert res.status_code == 303
        assert res.headers["location"] == "/history"


def test_day_summary_redirect_preserves_query_string() -> None:
    client = _client()
    res = client.get("/day-summary?domain=feed")
    assert res.status_code == 303
    assert res.headers["location"] == "/history?domain=feed"


def test_history_tabbar_has_only_one_entry() -> None:
    client = _client()
    page = client.get("/history").text
    assert page.count('href="/history"') == 1
    assert "Summary" not in page
    assert 'href="/day-summary"' not in page


def test_history_grouped_by_day_newest_first() -> None:
    client = _client()

    # Log feed today
    client.post("/api/feed", data={"method": "bottle_expressed", "volume_ml": "60"})

    # Log feed yesterday
    yesterday = to_local(NOW - timedelta(days=1)).replace(hour=8, minute=0)
    res = client.post("/api/feed", data={"method": "breast_left"})
    assert res.status_code in (200, 303)
    client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 2, "ts": yesterday.strftime("%Y-%m-%dT%H:%M")},
    )

    page = client.get("/history").text
    pos_today = page.find("15 Jul")
    pos_yesterday = page.find("14 Jul")
    assert pos_today != -1 and pos_yesterday != -1
    assert pos_today < pos_yesterday, "Most recent day must come first"


def test_history_chronological_within_day() -> None:
    client = _client()

    # Log feed 1 at 14:00 (ID 1)
    t1 = to_local(NOW).replace(hour=14, minute=0)
    client.post("/api/feed", data={"method": "bottle_expressed", "volume_ml": "60"})
    client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 1, "ts": t1.strftime("%Y-%m-%dT%H:%M")},
    )

    # Log feed 2 at 06:20 (ID 2)
    t2 = to_local(NOW).replace(hour=6, minute=20)
    client.post("/api/feed", data={"method": "breast_left"})
    client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 2, "ts": t2.strftime("%Y-%m-%dT%H:%M")},
    )

    # Log feed 3 at 20:30 (ID 3)
    t3 = to_local(NOW).replace(hour=20, minute=30)
    client.post("/api/feed", data={"method": "bottle_formula", "volume_ml": "100"})
    client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 3, "ts": t3.strftime("%Y-%m-%dT%H:%M")},
    )

    page = client.get("/history").text
    p2 = page.find("06:20")
    p1 = page.find("14:00")
    p3 = page.find("20:30")
    assert p2 != -1 and p1 != -1 and p3 != -1
    assert p2 < p1 < p3, "Events within a day must be ordered chronologically"


def test_history_compact_rows_all_domains() -> None:
    client = _client()

    # 1. feed
    client.post("/api/feed", data={"method": "bottle_expressed", "volume_ml": "60"})
    # 2. nappy
    client.post("/api/nappy", data={"kind": "dirty", "stool_colour": "yellow"})
    # 3. sleep
    client.post("/api/sleep/toggle")
    # 4. growth
    client.post("/api/growth", data={"measure": "weight", "value": "3500"})
    # 5. temperature
    client.post("/api/temperature", data={"temp_c": "36.6", "site": "axillary"})
    # 6. milestone
    client.post("/api/milestone", data={"category": "first", "title": "First bath"})
    # 7. note
    client.post("/api/note", data={"text": "Good day overall"})

    page = client.get("/history").text
    assert "Feed" in page and "Bottle Expressed" in page and "60 ml" in page
    assert "Nappy" in page and "Dirty" in page and "yellow" in page
    assert "Sleep" in page and "asleep" in page
    assert "Growth" in page and "Weight" in page and "3500g" in page
    assert "Temperature" in page and "36.6 C" in page
    assert "Milestone" in page and "First" in page and "First bath" in page
    assert "Note" in page and "Good day overall" in page


def test_history_row_actions_are_orange_and_distinct_from_alert_severity() -> None:
    """Exit criterion: every row has three orange action controls, styled
    distinctly from app.css's amber/red alert severity classes (A7/U42)."""
    client = _client()
    client.post("/api/feed", data={"method": "bottle_expressed", "volume_ml": "60"})

    page = client.get("/history").text
    assert 'class="row-action edit"' in page
    assert 'class="row-action clone"' in page
    assert 'class="row-action del"' in page

    css = client.get("/static/app.css").text
    assert ".row-action{" in css
    assert "--action" in css
    # The row-action colour token must be a distinct variable from the
    # existing amber/red severity tokens, not a re-skin of one of them.
    assert "var(--action)" in css
    assert ".row-action{" in css and "var(--warn)" not in css.split(".row-action{")[1][:200]


def test_history_page_has_delete_confirmation_not_immediate_delete() -> None:
    """U43 exit criterion: Delete opens a confirmation, it never posts
    directly from the row - unlike U4/U38's old inline delete form."""
    client = _client()
    client.post("/api/feed", data={"method": "bottle_expressed", "volume_ml": "60"})

    page = client.get("/history").text
    # No row-level <form action="/api/delete"> firing on click any more...
    assert '<form method="post" action="/api/delete"' not in page
    # ...instead a link opens the confirm panel.
    assert 'panel=delete&amp;table=feed&amp;event_id=1' in page

    confirm = client.get("/?panel=delete&table=feed&event_id=1").text
    assert 'action="/api/delete"' in confirm
    assert "Delete this" in confirm
