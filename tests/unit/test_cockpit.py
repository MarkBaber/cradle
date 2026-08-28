"""CP-01: poll tick must not block the input loop.

Tests cover:
  - refresh(force=False) returns immediately without running probe_agents
  - a background-refresh result is adopted on the next draw()
  - refresh(force=True) ('r' keypress) still runs synchronously
  - _rows_cache entries that belong to unchanged panes survive a refresh
"""

import argparse
import sys
import time
import types
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from cockpit import (  # noqa: E402  (scripts/ is on path)
    Agent,
    Snapshot,
    _State,
)

# ---------------------------------------------------------------------------
# Minimal test fixtures
# ---------------------------------------------------------------------------

_DUMMY_REPO = Path("/tmp/fake-repo")


def _make_snapshot(**overrides: Any) -> Snapshot:
    """Cheapest possible Snapshot that satisfies the NamedTuple signature."""
    defaults: dict[str, Any] = dict(
        repo=_DUMMY_REPO,
        cfg={"project": "test"},
        by_id={},
        buckets={
            "ready": [],
            "in_progress": [],
            "blocked": [],
            "done": [],
            "review": [],
            "deferred": [],
            "needs_routing": [],
        },
        findings=[],
        lanes=[],
        deferred=[],
        action=None,
        layout={},
        git=None,
        errors=[],
        taken_at=time.time(),
    )
    defaults.update(overrides)
    return Snapshot(**defaults)


def _make_args(**overrides: Any) -> argparse.Namespace:
    defaults = dict(lanes=None, no_git=True, capture_usage=False, claude_bin=None)
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_bl() -> types.ModuleType:
    """Stub backlog module -- only the attributes _State.__init__ touches."""
    bl = types.ModuleType("house_backlog")
    return bl


def _make_state(
    *,
    snap: Snapshot | None = None,
    agents: list[Agent] | None = None,
    daily: tuple[int, float] | None = None,
    pending: frozenset[str] | None = None,
) -> _State:
    """Create a _State with pre-seeded data, bypassing curses and real I/O."""
    bl = _make_bl()
    args = _make_args()

    with (
        patch("cockpit.load_usage", return_value=None),
        patch("cockpit.active_panes", return_value=("BACKLOG", "DOCTOR", "FLEET", "NEXT")),
        patch(
            "cockpit.get_subprocess_pool",
            return_value=MagicMock(submit=lambda fn, *a, **kw: Future()),
        ),
    ):
        state = _State(bl, _DUMMY_REPO, args, "claude", {})

    # Seed the state so it's 'ready' (as if an initial refresh already ran).
    state.snap = snap if snap is not None else _make_snapshot()
    state.agents = agents if agents is not None else []
    state.daily = daily
    state.pending = pending if pending is not None else frozenset()
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_poll_tick_refresh_does_not_block_on_the_agents_probe() -> None:
    """refresh(force=False) must not call probe_agents on the calling thread.

    The probe pipeline is expensive (~1.4 s measured); every call to it on
    the input thread makes the terminal unresponsive for that duration.
    refresh(force=False) must submit the work to the background pool and
    return *immediately* -- it must never invoke probe_agents inline.
    """
    state = _make_state()

    probe_agents_called = False

    def _slow_probe_agents(*_a: Any, **_kw: Any) -> list[Agent]:
        nonlocal probe_agents_called
        probe_agents_called = True
        return []

    # Give the state a real pool so we can verify submit() is called but
    # the actual work never runs on *this* thread within the call.
    submitted_fns: list[Any] = []

    class _TrackingPool:
        def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Future:  # type: ignore[override]
            submitted_fns.append(fn)
            f: Future[Any] = Future()
            # Mark done with a dummy result so _poll_refresh doesn't hang.
            f.set_result((_make_snapshot(), [], None, frozenset()))
            return f

    state.pool = _TrackingPool()  # type: ignore[assignment]

    with patch("cockpit.probe_agents", side_effect=_slow_probe_agents):
        state.refresh(force=False)

    # probe_agents must NOT have been called on this thread.
    assert not probe_agents_called, (
        "refresh(force=False) called probe_agents synchronously -- that blocks the input loop"
    )
    # The pipeline must have been submitted to the pool exactly once.
    assert len(submitted_fns) == 1, (
        f"expected exactly 1 pool.submit() call; got {len(submitted_fns)}"
    )


def test_background_refresh_result_is_adopted_on_a_later_draw() -> None:
    """A background probe result is not visible until _poll_refresh() runs.

    The sequence:
      1. refresh(force=False) -- submits pipeline, returns immediately
      2. draw() -- calls _poll_refresh() which sees the completed future
      3. After draw(), state.snap is the *new* snapshot, not the old one.
    """
    old_snap = _make_snapshot(taken_at=1.0)
    new_snap = _make_snapshot(taken_at=2.0)

    state = _make_state(snap=old_snap)
    assert state.snap is old_snap

    # Build a future that is already resolved with the new pipeline result.
    resolved: Future[Any] = Future()
    resolved.set_result((new_snap, [], None, frozenset()))

    # Patch pool.submit to immediately return the resolved future.
    state.pool = MagicMock()  # type: ignore[assignment]
    state.pool.submit.return_value = resolved

    # Step 1: tick -- submits the pipeline, returns immediately.
    state.refresh(force=False)
    # At this point snap is still the old one.
    assert state.snap is old_snap, "snap must not change until _poll_refresh"

    # Step 2: simulate what draw() does (call _poll_refresh directly to
    # avoid needing a real curses window).
    state._poll_refresh()

    # Step 3: new snapshot is now adopted.
    assert state.snap is new_snap, "background refresh result was not adopted after _poll_refresh()"


def test_explicit_r_refresh_stays_synchronous() -> None:
    """refresh(force=True) must run the probe pipeline on the calling thread.

    An operator's explicit 'r' is an intentional pause -- they asked for
    fresh data.  Blocking for 1-2 s is acceptable (and expected).
    """
    state = _make_state()

    probe_called = False
    new_snap = _make_snapshot(taken_at=99.0)

    def _inline_snapshot(*_a: Any, **_kw: Any) -> Snapshot:
        nonlocal probe_called
        probe_called = True
        return new_snap

    with (
        patch("cockpit.snapshot", side_effect=_inline_snapshot),
        patch("cockpit.probe_agents", return_value=[]),
        patch("cockpit.daily_usage", return_value=None),
        patch("cockpit.pending_landed_task_ids", return_value=frozenset()),
    ):
        state.refresh(force=True)

    assert probe_called, "refresh(force=True) did not call snapshot() on the calling thread"
    assert state.snap is new_snap, "refresh(force=True) did not adopt the new snapshot"
    # No background future should be in flight.
    assert state._refresh_future is None or state._refresh_future.done(), (
        "refresh(force=True) left a pending background future"
    )


def test_rows_cache_survives_a_refresh_that_changed_nothing() -> None:
    """When the probe returns identical data, cached row lists are preserved.

    Clearing _rows_cache unconditionally on every tick forces all panes to
    rebuild their rows even when nothing changed, wasting CPU and causing
    USAGE's transcript scan to run every second.  If snap, agents, daily,
    and pending are all unchanged, every per-pane cache entry must survive.
    """
    fixed_snap = _make_snapshot()
    fixed_agents: list[Agent] = []
    fixed_daily: tuple[int, float] | None = None
    fixed_pending: frozenset[str] = frozenset()

    state = _make_state(
        snap=fixed_snap,
        agents=fixed_agents,
        daily=fixed_daily,
        pending=fixed_pending,
    )

    # Manually populate the rows cache for a few panes.
    from cockpit import Row

    sentinel_0 = [Row("backlog sentinel", "text")]
    sentinel_1 = [Row("doctor sentinel", "text")]
    sentinel_2 = [Row("fleet sentinel", "text")]
    state._rows_cache[0] = sentinel_0
    state._rows_cache[1] = sentinel_1
    state._rows_cache[2] = sentinel_2

    # Adopt a new probe result that is *identical* to what we already hold.
    # _adopt_probe_result compares values; same snapshot object and same
    # empty collections must leave all three cache entries intact.
    state._adopt_probe_result((fixed_snap, fixed_agents, fixed_daily, fixed_pending))

    assert state._rows_cache.get(0) is sentinel_0, (
        "pane 0 cache was evicted even though no source data changed"
    )
    assert state._rows_cache.get(1) is sentinel_1, (
        "pane 1 cache was evicted even though snap did not change"
    )
    assert state._rows_cache.get(2) is sentinel_2, (
        "pane 2 cache was evicted even though snap/agents/daily did not change"
    )
