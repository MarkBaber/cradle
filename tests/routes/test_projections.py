"""U23: nested feed/mess dials on /today, hunger and mess gauge bars, the
dirty-hint secondary line, cold-start and overdue states, and the compact
one-line feed echo on / (the Log tab).

Renders V3's ProjectionService (already implemented, tests/unit/test_
projection_service.py) - nothing here computes a projection itself; every
expected number is derived from the same arithmetic V3 already guarantees,
via the config overrides documented in rules_config.toml's [projections]
table. Skipped by the offline runner when fastapi is unavailable.
"""

import math
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.models import FeedMethod, NappyKind, to_local  # noqa: E402
from cradle.ports.clock import FixedClock  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}

# Ring geometry the dials must share with the SVG (radius 54 outer/feed,
# radius 38 inner/mess) - used only to compute the *expected* dasharray
# numbers, never imported from the app.
OUTER_R = 54.0
INNER_R = 38.0
OUTER_CIRC = round(2 * math.pi * OUTER_R, 1)
INNER_CIRC = round(2 * math.pi * INNER_R, 1)


def _client(config_path: Path | None = None) -> TestClient:
    db = Path(tempfile.mkdtemp()) / "routes.db"
    app = create_app(
        db_path=db,
        clock=FixedClock(NOW),
        config_path=config_path or (ROOT / "rules_config.toml"),
        start_scheduler=False,
    )
    client = TestClient(app, follow_redirects=False)
    assert client.post("/api/settings/profile", data=PROFILE).status_code == 303
    return client


def _projections_config(**overrides: float) -> Path:
    """The real rules_config.toml with just [projections] numbers swapped, so
    every other table (weight thresholds etc.) stays byte-identical and
    build_services still constructs cleanly."""
    text = (ROOT / "rules_config.toml").read_text(encoding="utf-8")
    for key, value in overrides.items():
        text = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", text)
    path = Path(tempfile.mkdtemp()) / "cfg.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- arithmetic
# Mirrors of the plain formulas the task's contract spells out, used only to
# compute the *expected* numbers for a given synthetic timeline - not a
# stand-in for the implementation itself.


def _hm(hours: float) -> str:
    total_min = round(abs(hours) * 60)
    h, m = divmod(total_min, 60)
    return f"{h}h{m:02d}"


def _ago(hours: float) -> str:
    total_min = round(abs(hours) * 60)
    h, m = divmod(total_min, 60)
    return f"{h}h{m}m ago" if h else f"{m}m ago"


def _hhmm(due_at: datetime) -> str:
    return to_local(due_at).strftime("%H:%M")


def _window_h(window_max_h: float, now: datetime, *due_ats: datetime | None) -> float:
    window_h = float(window_max_h)
    for due in due_ats:
        if due is not None:
            remaining = (due - now).total_seconds() / 3600
            if remaining > window_h:
                window_h = math.ceil(remaining)
    return window_h


def _fraction_left(due_at: datetime | None, now: datetime, window_h: float) -> float:
    if due_at is None:
        return 1.0
    remaining = (due_at - now).total_seconds() / 3600
    if remaining <= 0:
        return 0.0
    return min(1.0, remaining / window_h)


def _dasharray_x(circumference: float, fraction_left: float) -> float:
    return round(fraction_left * circumference, 1)


# ---------------------------------------------------------------- extraction


def _projections_section(html: str) -> str:
    match = re.search(r'<section[^>]*id="projections"[^>]*>', html)
    assert match is not None, "no #projections section in /today"
    return match.group(0)


def _ring(html: str, which: str) -> str:
    """The full <circle class="dial-arc {which} ..."> tag, whatever else it
    carries (overdue/cold class tokens) and however it wraps onto lines."""
    match = re.search(rf'<circle class="dial-arc {which}[^>]*>', html)
    assert match is not None, f"no dial-arc {which} circle in /today"
    return match.group(0)


def _dasharray(tag: str) -> tuple[float, float]:
    match = re.search(r'stroke-dasharray="([\d.]+)\s+([\d.]+)"', tag)
    assert match is not None, f"no stroke-dasharray on {tag!r}"
    return float(match.group(1)), float(match.group(2))


def _window_after(html: str, marker: str, size: int = 500) -> str:
    idx = html.find(marker)
    assert idx != -1, f"{marker!r} not found in response body"
    return html[idx : idx + size]


def _pct_near(html: str, marker: str) -> int:
    chunk = _window_after(html, marker)
    match = re.search(r"(\d+)%", chunk)
    assert match is not None, f"no NN% found near {marker!r}: {chunk!r}"
    return int(match.group(1))


def _without_svg(html: str) -> str:
    return re.sub(r"<svg\b.*?</svg>", "", html, flags=re.DOTALL)


# --------------------------------------------------------- 1. nested rings


def test_today_renders_nested_feed_and_mess_rings_on_a_shared_scale() -> None:
    config = _projections_config(
        ml_per_hour=60, typical_feed_ml=120, mess_interval_min=120, window_max_h=2
    )
    client = _client(config)
    client.app.state.services.logging.log_feed(
        FeedMethod.BOTTLE_FORMULA, ts=NOW - timedelta(minutes=30), volume_ml=60
    )
    client.app.state.services.logging.log_nappy(NappyKind.WET, ts=NOW - timedelta(minutes=30))

    r = client.get("/today")
    assert r.status_code == 200
    html = r.text

    section = _projections_section(html)
    assert "dials" in section
    assert not re.search(r"\bcold\b", section)

    feed_due_at = NOW + timedelta(minutes=30)  # (ts=NOW-30m) + 60ml/60mlph = ts+1h
    mess_due_at = NOW + timedelta(hours=1, minutes=30)  # (ts=NOW-30m) + 120min = ts+2h
    window_h = _window_h(2, NOW, feed_due_at, mess_due_at)

    outer = _ring(html, "outer")
    assert "overdue" not in outer
    x_outer, y_outer = _dasharray(outer)
    assert math.isclose(y_outer, OUTER_CIRC, abs_tol=0.2)
    expected_x_outer = _dasharray_x(OUTER_CIRC, _fraction_left(feed_due_at, NOW, window_h))
    assert math.isclose(x_outer, expected_x_outer, abs_tol=0.2)
    assert 0 < x_outer < y_outer, "feed ring should be depleting, not empty or untouched"

    inner = _ring(html, "inner")
    assert "overdue" not in inner
    x_inner, y_inner = _dasharray(inner)
    assert math.isclose(y_inner, INNER_CIRC, abs_tol=0.2)
    expected_x_inner = _dasharray_x(INNER_CIRC, _fraction_left(mess_due_at, NOW, window_h))
    assert math.isclose(x_inner, expected_x_inner, abs_tol=0.2)
    assert 0 < x_inner < y_inner, "mess ring should be depleting, not empty or untouched"


# --------------------------------------------------------- 2. auto-scale


def test_dial_window_autoscales_past_window_max_h_for_both_rings() -> None:
    """window_max_h=2 but mess is due in 2.5h: the window must grow to 3 for
    BOTH rings, or the mess ring would wrongly render as a maxed-out full
    circle (min(1, 2.5/2) clamps to 1.0) and the feed ring would be scaled
    against the wrong (2h) window."""
    config = _projections_config(
        ml_per_hour=60, typical_feed_ml=60, mess_interval_min=150, window_max_h=2
    )
    client = _client(config)
    client.app.state.services.logging.log_feed(FeedMethod.BOTTLE_FORMULA, ts=NOW, volume_ml=60)
    client.app.state.services.logging.log_nappy(NappyKind.WET, ts=NOW)

    html = client.get("/today").text

    feed_due_at = NOW + timedelta(hours=1)
    mess_due_at = NOW + timedelta(hours=2, minutes=30)
    window_h = _window_h(2, NOW, feed_due_at, mess_due_at)
    assert window_h == 3, "test setup should force a window of 3, not 2"

    x_outer, y_outer = _dasharray(_ring(html, "outer"))
    expected_x_outer_at_3 = _dasharray_x(OUTER_CIRC, _fraction_left(feed_due_at, NOW, 3))
    expected_x_outer_at_2 = _dasharray_x(OUTER_CIRC, _fraction_left(feed_due_at, NOW, 2))
    assert math.isclose(x_outer, expected_x_outer_at_3, abs_tol=0.2), (
        "feed ring must scale against the auto-scaled window of 3, not the "
        "configured window_max_h of 2"
    )
    assert not math.isclose(x_outer, expected_x_outer_at_2, abs_tol=0.2)

    x_inner, y_inner = _dasharray(_ring(html, "inner"))
    expected_x_inner = _dasharray_x(INNER_CIRC, _fraction_left(mess_due_at, NOW, 3))
    assert math.isclose(x_inner, expected_x_inner, abs_tol=0.2)
    assert not math.isclose(x_inner, y_inner, abs_tol=0.2), (
        "mess ring must not be clipped/maxed out full at a 2h scale"
    )


# --------------------------------------------------------- 3. hunger bar


def test_hunger_bar_reads_volume_deficit_not_elapsed_fraction() -> None:
    """Two households, identical elapsed time since their last feed (1h) and
    the identical configured rate, but one has consistently logged SMALL
    bottles and the other consistently LARGE ones - so V3's computed typical
    feed volume differs between them even though "time since last feed" does
    not. hunger_fraction (rate*hours_since/typical_ml) must reflect that
    volume deficit: the small-feed household is numerically hungrier. A
    naive elapsed/window fraction would show the two households identically,
    since both are "1h since last feed" under the same window."""
    config = _projections_config(ml_per_hour=20)

    small = _client(config)
    for h in (7, 5, 3, 1):
        small.app.state.services.logging.log_feed(
            FeedMethod.BOTTLE_FORMULA, ts=NOW - timedelta(hours=h), volume_ml=30
        )
    small_html = small.get("/today").text

    large = _client(config)
    for h in (7, 5, 3, 1):
        large.app.state.services.logging.log_feed(
            FeedMethod.BOTTLE_FORMULA, ts=NOW - timedelta(hours=h), volume_ml=180
        )
    large_html = large.get("/today").text

    small_pct = _pct_near(small_html, "Hunger")
    large_pct = _pct_near(large_html, "Hunger")

    # rate=20ml/h, hours_since=1h: small typical_ml=30 -> 20*1/30 = 67%;
    # large typical_ml=180 -> 20*1/180 = 11%.
    assert small_pct == 67
    assert large_pct == 11
    assert small_pct > large_pct

    assert f"height:{small_pct}%" in _window_after(small_html, "gauge hunger")
    assert f"height:{large_pct}%" in _window_after(large_html, "gauge hunger")


# --------------------------------------------------------- 4. overdue


def test_overdue_feed_empties_the_arc_pins_the_bar_and_shows_a_due_ago_readout() -> None:
    config = _projections_config(ml_per_hour=60, typical_feed_ml=60, window_max_h=2)
    client = _client(config)
    client.app.state.services.logging.log_feed(
        FeedMethod.BOTTLE_FORMULA, ts=NOW - timedelta(hours=5), volume_ml=60
    )

    html = client.get("/today").text

    # feed_due_at = ts + 60ml/60mlph = ts+1h = NOW-4h -> 4h overdue.
    outer = _ring(html, "outer")
    assert "overdue" in outer
    x_outer, _ = _dasharray(outer)
    assert math.isclose(x_outer, 0.0, abs_tol=0.2), "an overdue ring must be emptied"

    hunger_chunk = _window_after(html, 'class="gauge hunger')
    assert "overdue" in hunger_chunk.split(">")[0]
    assert "height:100%" in hunger_chunk, "hunger must pin to 100%, not the raw fraction"
    assert "100%" in hunger_chunk

    ago_text = f"feed due {_ago(4.0)}"
    assert ago_text in html, f"expected {ago_text!r} in the response body"


# --------------------------------------------------------- 5. cold start


def test_cold_start_renders_greyed_dials_with_the_not_enough_history_prompt() -> None:
    client = _client()  # zero feed and nappy events

    r = client.get("/today")
    assert r.status_code == 200

    section = _projections_section(r.text)
    assert re.search(r"\bcold\b", section), "the card must carry a cold class token, not hide"
    assert "log a few feeds and this will fill in" in r.text


# --------------------------------------------------------- 6. dirty hint


def test_dirty_hint_appears_as_a_secondary_readout_once_enough_history_exists() -> None:
    config = _projections_config()
    client = _client(config)
    for h in (10, 7, 4, 1):
        client.app.state.services.logging.log_nappy(NappyKind.DIRTY, ts=NOW - timedelta(hours=h))

    html = client.get("/today").text

    dirty_due_at = NOW - timedelta(hours=1) + timedelta(hours=3)  # median gap 3h
    expected = f"dirty due ~{_hhmm(dirty_due_at)}"
    assert expected in html


def test_dirty_hint_absent_without_enough_dirty_or_mixed_history() -> None:
    client = _client()  # no nappies logged at all -> dirty_due_at is None
    html = client.get("/today").text
    assert "dirty due ~" not in html


# --------------------------------------------------------- 7. /log echo


def test_log_page_shows_the_compact_next_feed_echo_and_still_submits_without_js() -> None:
    config = _projections_config(ml_per_hour=60, typical_feed_ml=60)
    client = _client(config)
    client.app.state.services.logging.log_feed(
        FeedMethod.BOTTLE_FORMULA, ts=NOW - timedelta(minutes=30), volume_ml=60
    )

    page = client.get("/")
    assert page.status_code == 200
    assert 'class="note feed-echo"' in page.text
    assert "next feed in 0h30" in page.text
    assert "13:30" not in _window_after(page.text, "feed-echo", 60), (
        "the /log echo is feed-only and countdown-only, no clock time"
    )

    r = client.post("/api/feed", data={"method": "breast_left"})
    assert r.status_code < 400, "the no-JS quick-entry POST must keep working"


def test_log_page_echo_shows_overdue_and_cold_variants() -> None:
    overdue_config = _projections_config(ml_per_hour=60, typical_feed_ml=60)
    overdue_client = _client(overdue_config)
    overdue_client.app.state.services.logging.log_feed(
        FeedMethod.BOTTLE_FORMULA, ts=NOW - timedelta(hours=5), volume_ml=60
    )
    overdue_page = overdue_client.get("/").text
    assert f"feed due {_ago(4.0)}" in overdue_page

    cold_client = _client()
    cold_page = cold_client.get("/").text
    assert "log a few feeds and this will fill in" in cold_page


# --------------------------------------------------------- 8. accessibility


def test_dials_are_accessible_svg_is_hidden_and_numbers_exist_as_plain_text() -> None:
    config = _projections_config(
        ml_per_hour=60, typical_feed_ml=120, mess_interval_min=120, window_max_h=2
    )
    client = _client(config)
    client.app.state.services.logging.log_feed(
        FeedMethod.BOTTLE_FORMULA, ts=NOW - timedelta(minutes=30), volume_ml=60
    )
    client.app.state.services.logging.log_nappy(NappyKind.WET, ts=NOW - timedelta(minutes=30))

    html = client.get("/today").text

    svg_tag = re.search(r"<svg\b[^>]*>", html)
    assert svg_tag is not None
    assert 'aria-hidden="true"' in svg_tag.group(0)

    feed_due_at = NOW + timedelta(minutes=30)  # (ts=NOW-30m) + 60ml/60mlph = ts+1h
    center_text = f"next feed in {_hm(0.5)}, {_hhmm(feed_due_at)}"
    assert center_text in html

    outside_svg = _without_svg(html)
    assert center_text in outside_svg, (
        "the numeric countdown must exist as real DOM text outside the "
        "decorative <svg>, not only inside SVG attributes"
    )
