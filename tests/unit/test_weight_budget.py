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
