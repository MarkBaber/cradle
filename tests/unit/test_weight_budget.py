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

# wheel-picker (task U22, MIT, npm "wheel-picker" 1.1.0): the one vendored JS
# library Mark Baber's request explicitly authorised, for the iPhone-style
# scroll-wheel time picker only. Same "not committed, charge the pinned size"
# treatment as htmx above. Allowances are rounded up from the real pinned
# sizes (scripts/vendor_assets.sh): wheelpicker.min.js is 13342 bytes,
# wheelpicker.min.css is 2429 bytes.
WHEELPICKER_JS_ALLOWANCE_BYTES = 14 * 1024
WHEELPICKER_CSS_ALLOWANCE_BYTES = 3 * 1024


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
    total += _vendored_or_allowance("wheelpicker.min.js", WHEELPICKER_JS_ALLOWANCE_BYTES)
    total += _vendored_or_allowance("wheelpicker.min.css", WHEELPICKER_CSS_ALLOWANCE_BYTES)
    assert total <= BUDGET_BYTES, f"quick-entry payload {total} bytes exceeds {BUDGET_BYTES}"


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
