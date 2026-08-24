"""Q1: the quick-entry path must stay small enough to load on bad Wi-Fi."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

STATIC = ROOT / "src" / "cradle" / "routers" / "static"
BUDGET_BYTES = 150 * 1024

# Assets the quick-entry page actually pulls. Plotly is deliberately excluded:
# it loads only on /charts (see charts.html), which is why it is not a budget
# item here. entry.js (task U22) upgrades the panels' time inputs to the
# scroll-wheel picker and is on this path, so it is charged directly - it is
# a real committed file, not a vendored one.
QUICK_ENTRY_ASSETS = ("app.css", "pwa.js", "manifest.json", "icon.svg", "entry.js")

# htmx is fetched by scripts/vendor_assets.sh and is not committed (no network
# in the build sandbox, task U8). Charge its pinned size against the budget so
# this gate is meaningful before vendoring and stays meaningful after.
HTMX_ALLOWANCE_BYTES = 52 * 1024

# jQuery + AnyPicker (task U29, replaces U22's wheel-picker): the combined
# iOS-style date+time picker for the entry panel and /history edit controls.
# Mark Baber directed picking the best interaction for the job over trimming
# the choice to fit the historical 150 KB ceiling, explicitly including a
# jQuery-dependent option (2026-08-21) - so unlike htmx above, these assets
# are exempted from the budget total entirely, the same shape of carve-out
# this file already gives Plotly on /charts (see test_plotly_is_not_on_the_
# quick_entry_path below), rather than charged an allowance-when-absent.
# docs/SPEC.md §1/§7.1 (U29) records the amendment. The gate itself still
# covers every other quick-entry asset unchanged: app.css, entry.js, htmx,
# and the core tiles (pwa.js, manifest.json, icon.svg).
PICKER_ASSETS_EXEMPT_FROM_BUDGET = ("jquery.min.js", "anypicker.min.js", "anypicker-all.min.css")


def _size(name: str) -> int:
    path = STATIC / name
    assert path.exists(), f"quick-entry asset missing: {name}"
    return path.stat().st_size


def _vendored_or_allowance(name: str, allowance: int) -> int:
    path = STATIC / "vendor" / name
    return path.stat().st_size if path.exists() else allowance


def test_quick_entry_within_budget() -> None:
    total = sum(_size(n) for n in QUICK_ENTRY_ASSETS)
    total += _vendored_or_allowance("htmx.min.js", HTMX_ALLOWANCE_BYTES)
    assert total <= BUDGET_BYTES, f"quick-entry payload {total} bytes exceeds {BUDGET_BYTES}"


def test_picker_assets_are_exempt_from_the_budget() -> None:
    """The picker's real vendored size must never be folded into the total above."""
    exempt_total = sum(
        (STATIC / "vendor" / n).stat().st_size
        for n in PICKER_ASSETS_EXEMPT_FROM_BUDGET
        if (STATIC / "vendor" / n).exists()
    )
    if exempt_total:
        assert exempt_total > BUDGET_BYTES, (
            "picker assets are smaller than the budget - the carve-out no longer proves anything"
        )


def test_plotly_is_not_on_the_quick_entry_path() -> None:
    quick = (ROOT / "src" / "cradle" / "routers" / "templates" / "quick_entry.html").read_text(
        encoding="utf-8"
    )
    base = (ROOT / "src" / "cradle" / "routers" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    assert "plotly" not in quick.lower()
    assert "plotly" not in base.lower(), "plotly must stay scoped to /charts"


def test_service_worker_caches_static_only() -> None:
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'e.request.method !== "GET"' in sw, "writes must never be served from cache"
    assert '"/static/"' in sw


def test_service_worker_precaches_chart_assets() -> None:
    """U41: repeat /charts visits must get plotly/chart.js/series.js from the SW cache."""
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert '"/static/vendor/plotly.min.js"' in sw
    assert '"/static/chart.js"' in sw
    assert '"/static/series.js"' in sw
    assert "cradle-static-v1" not in sw, "CACHE must be bumped so the new assets get precached"


# task U41: the full Plotly bundle vendored before this task was ~4.5MB on
# disk even minified - this app only ever draws cartesian traces (bar,
# scatter, dual y-axis, fill "tonexty", addFrames/animate - see chart.js and
# series.js), so scripts/vendor_assets.sh now pins a cartesian-only partial
# bundle instead. This guards against silently drifting back to the full one.
PLOTLY_FULL_BUNDLE_BYTES = 4 * 1024 * 1024


def test_journal_page_adds_no_new_static_assets() -> None:
    """U44: /journal reuses base.html's already-budgeted app.css/pwa.js
    (styling is inline in the template's own {% block head %}) and pulls in
    no new committed static file of its own, so BUDGET_BYTES above is
    unaffected by this task - nothing to amend."""
    journal = (
        ROOT / "src" / "cradle" / "routers" / "templates" / "journal.html"
    ).read_text(encoding="utf-8")
    assert "<script src=" not in journal
    assert '<link rel="stylesheet"' not in journal


def test_journal_book_is_self_contained_with_no_external_references() -> None:
    """The exported book (task U44) must open standalone with no server: no
    /static asset, no external stylesheet or script, no http(s) reference."""
    book = (
        ROOT / "src" / "cradle" / "routers" / "templates" / "journal_book.html"
    ).read_text(encoding="utf-8")
    assert "/static/" not in book
    assert "<script" not in book
    assert "http://" not in book
    assert "https://" not in book


def test_vendored_plotly_is_the_smaller_cartesian_bundle() -> None:
    path = STATIC / "vendor" / "plotly.min.js"
    if not path.exists():
        return  # not vendored in this environment (no network) - nothing to check
    assert path.stat().st_size < PLOTLY_FULL_BUNDLE_BYTES, (
        "vendored plotly.min.js is as large as the old full bundle - "
        "check scripts/vendor_assets.sh still points at the cartesian build"
    )
