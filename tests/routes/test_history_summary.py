"""Route tests for task U43: History and Summary merged into one page.

U40's day-grouped layout is now /history's only view; /summary and
/day-summary are thin back-compat redirects to it. Covers the redirects, the
single tabbar entry, day-grouping order, and the day-grouped rows rendering
across every domain. Edit/Clone/Delete/Add panel behaviour lives in
test_quick_entry.py alongside the rest of the #panel system it reuses.
"""

import re
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


def test_history_most_recent_first_within_day() -> None:
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
    assert p3 < p1 < p2, "Events within a day must show most recent first"


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
    assert "panel=delete&amp;table=feed&amp;event_id=1" in page

    confirm = client.get("/?panel=delete&table=feed&event_id=1").text
    assert 'action="/api/delete"' in confirm
    assert "Delete this" in confirm


# --------------------------------------------------------------- task U46


def test_history_covers_expression_domain() -> None:
    client = _client()
    client.post("/api/express")
    page = client.get("/history").text
    assert "Expression" in page
    assert "Both" in page


def test_history_covers_milk_batch_domain() -> None:
    client = _client()
    client.post("/api/milk/store", data={"store": "fridge", "colour": "blue", "volume_ml": "100"})
    page = client.get("/history").text
    assert "Milk Batch" in page
    assert "blue bottle" in page and "100 ml" in page
    assert "Stored" in page


def test_history_covers_activity_domain() -> None:
    client = _client()
    client.post("/api/activity", data={"category": "tummy_time", "duration_min": "10"})
    page = client.get("/history").text
    assert "Activity" in page
    assert "Tummy Time" in page and "10 min" in page


def test_history_covers_journal_domain() -> None:
    """Journal is deliberately read-only on the history row (U44's own notes,
    task U46's follow-up for visibility only) - a link back to /journal, not
    the Edit/Clone/Delete panel plumbing other domains get."""
    client = _client()
    client.post(
        "/api/journal", data={"title": "First giggle", "story": "So cute", "temperament": "giggly"}
    )
    page = client.get("/history").text
    assert "Journal" in page
    assert "First giggle" in page
    assert 'href="/journal"' in page
    assert "table=journal" not in page


def test_history_defaults_to_current_calendar_week() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "bottle_expressed", "volume_ml": "60"})

    client.post("/api/nappy", data={"kind": "wet"})
    last_week = to_local(NOW - timedelta(days=5))
    client.post(
        "/api/adjust-time",
        data={"table": "nappy", "event_id": 1, "ts": last_week.strftime("%Y-%m-%dT%H:%M")},
    )

    page = client.get("/history").text
    assert "Bottle Expressed" in page
    assert "Wet" not in page, "an event from last week must not show on the default week"


def test_history_prev_next_pagination_moves_window_by_a_week() -> None:
    client = _client()
    client.post("/api/nappy", data={"kind": "wet"})
    last_week = to_local(NOW - timedelta(days=5))
    client.post(
        "/api/adjust-time",
        data={"table": "nappy", "event_id": 1, "ts": last_week.strftime("%Y-%m-%dT%H:%M")},
    )

    default_page = client.get("/history").text
    assert "Wet" not in default_page
    assert "week=2026-07-06" in default_page  # Prev link
    assert "week=2026-07-20" in default_page  # Next link

    prev_page = client.get("/history?week=2026-07-06").text
    assert "Wet" in prev_page

    fragment = client.get("/history/fragment?week=2026-07-06").text
    assert "Wet" in fragment
    assert 'id="history"' in fragment


def test_history_week_of_life_label() -> None:
    """dob 2026-07-01, NOW 2026-07-15 (a Wednesday): the default Mon-Sun week
    (13-19 Jul) starts 12 days after dob -> week 2 of life."""
    client = _client()
    page = client.get("/history").text
    assert "Week 2 of life" in page

    prev = client.get("/history?week=2026-07-06").text
    assert "Week 1 of life" in prev

    before_dob = client.get("/history?week=2026-06-01").text
    assert "Week 1 of life" in before_dob, "a week before dob must clamp, not go to 0 or negative"
    assert "Week 0" not in before_dob and "Week -" not in before_dob


def test_history_columns_share_one_width_recipe_across_day_groups() -> None:
    """U46: each day-group keeps its own <section><table> (U40/U43's
    structure - test_history_multiple_same_time_rows_keep_independent_row_ids
    in test_quick_entry.py still counts on one <section class="day-group">
    per day), but every table renders the *same* column widths because
    table-layout:fixed plus one shared per-column width recipe in app.css is
    applied identically to all of them - not auto-sized per table's own
    content."""
    client = _client()
    client.post("/api/feed", data={"method": "bottle_expressed", "volume_ml": "60"})
    yesterday = to_local(NOW - timedelta(days=1)).replace(hour=8, minute=0)
    client.post("/api/feed", data={"method": "breast_left"})
    client.post(
        "/api/adjust-time",
        data={"table": "feed", "event_id": 2, "ts": yesterday.strftime("%Y-%m-%dT%H:%M")},
    )

    page = client.get("/history").text
    assert page.count('<table class="summary-table">') == 2
    assert page.count('<section class="day-group">') == 2

    css = client.get("/static/app.css").text
    assert "table.summary-table{width:100%;table-layout:fixed" in css
    for col in ("time", "activity", "kind", "row-actions"):
        assert re.search(r"table\.summary-table td\." + col + r"\{width:\d+px", css), (
            f"td.{col} has no explicit width - would drift between tables under table-layout:fixed"
        )


_DOMAIN_TAG_VARS = {
    "feed": "--feed",
    "nappy": "--wet",
    "sleep": "--sleep",
    "growth": "--growth",
    "temperature": "--temp",
    "milestone": "--milestone",
    "note": "--note",
    "expression": "--expression",
    "milk_batch": "--milk-batch",
    "activity": "--activity",
    "journal": "--journal",
}


def test_history_every_domain_has_a_distinct_non_alert_badge_color() -> None:
    client = _client()
    css = client.get("/static/app.css").text

    root_block = re.search(r":root\{(.*?)\}", css, re.S)
    assert root_block
    root_vars = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{3,6})", root_block.group(1)))

    resolved = {}
    for domain, expected_var in _DOMAIN_TAG_VARS.items():
        tag_rule = re.search(re.escape(f".tag.{domain}") + r"\{color:var\((--[\w-]+)\)\}", css)
        assert tag_rule, f"no .tag.{domain} rule in app.css"
        var_name = tag_rule.group(1)
        assert var_name == expected_var, f".tag.{domain} uses {var_name}, expected {expected_var}"
        assert var_name in root_vars, f"{var_name} not defined in :root"
        resolved[domain] = root_vars[var_name]

    values = list(resolved.values())
    assert len(set(values)) == len(values), f"badge colors are not all distinct: {resolved}"

    warn, err = root_vars["--warn"], root_vars["--err"]
    for domain, hexval in resolved.items():
        assert hexval not in (warn, err), f"{domain} badge matches an alert severity colour"


def test_history_pagination_controls_work_as_plain_links_without_js() -> None:
    """No-JS contract (U19/U22/U29/U31): the week pager's href is a real,
    independently-working URL - hx-get is progressive enhancement only, so
    this must work with the picker script disabled or unloaded."""
    client = _client()
    page = client.get("/history").text

    prev_href = re.search(r'class="week-prev" href="([^"]+)"', page)
    next_href = re.search(r'class="week-next" href="([^"]+)"', page)
    assert prev_href and next_href

    for href in (prev_href.group(1), next_href.group(1)):
        res = client.get(href.replace("&amp;", "&"))
        assert res.status_code == 200
        assert 'id="history"' in res.text

    assert '<input type="date" name="week"' in page
