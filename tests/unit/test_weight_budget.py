"""Q1: the quick-entry path must stay small enough to load on bad Wi-Fi."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

STATIC = ROOT / "src" / "cradle" / "routers" / "static"
BUDGET_BYTES = 150 * 1024

# Assets the quick-entry page actually pulls. Plotly is deliberately excluded:
# it loads only on /charts (see charts.html), which is why it is not a budget
# item here.
QUICK_ENTRY_ASSETS = ("app.css", "pwa.js", "manifest.json", "icon.svg")

# htmx is fetched by scripts/vendor_assets.sh and is not committed (no network
# in the build sandbox, task U8). Charge its pinned size against the budget so
# this gate is meaningful before vendoring and stays meaningful after.
HTMX_ALLOWANCE_BYTES = 52 * 1024


def _size(name: str) -> int:
    path = STATIC / name
    assert path.exists(), f"quick-entry asset missing: {name}"
    return path.stat().st_size


def test_quick_entry_within_budget() -> None:
    total = sum(_size(n) for n in QUICK_ENTRY_ASSETS)
    vendored_htmx = STATIC / "vendor" / "htmx.min.js"
    total += (vendored_htmx.stat().st_size if vendored_htmx.exists()
              else HTMX_ALLOWANCE_BYTES)
    assert total <= BUDGET_BYTES, (
        f"quick-entry payload {total} bytes exceeds {BUDGET_BYTES}"
    )


def test_plotly_is_not_on_the_quick_entry_path() -> None:
    quick = (ROOT / "src" / "cradle" / "routers" / "templates"
             / "quick_entry.html").read_text(encoding="utf-8")
    base = (ROOT / "src" / "cradle" / "routers" / "templates"
            / "base.html").read_text(encoding="utf-8")
    assert "plotly" not in quick.lower()
    assert "plotly" not in base.lower(), "plotly must stay scoped to /charts"


def test_service_worker_caches_static_only() -> None:
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert 'e.request.method !== "GET"' in sw, "writes must never be served from cache"
    assert '"/static/"' in sw
