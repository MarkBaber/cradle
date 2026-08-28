"""O5: the Anthropic model/effort mapping the dispatch pipeline actually emits.

Three surfaces decide which model and effort a task runs at, and none of them
had a test:

  - backlog.build_command  -- the argv that reaches the CLI
  - cockpit.recommend_dispatch -- the size-derived Backend/Model/Effort seed
  - usage.MODEL_PRICES / CONTEXT_WINDOWS -- the cost and context-pressure
    signal an operator judges those choices by

Tests cover:
  - --effort is emitted for models that have an effort control, refused for
    the one that does not
  - the largest tasks are recommended at xhigh, not below the CLI's own
    default; a task pinning an effort-less model is recommended no effort
  - a dated-snapshot model id prices and windows as its undated self
  - Sonnet 5's advisory rate
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import backlog as bl  # noqa: E402  (scripts/ is on path)
import cockpit  # noqa: E402
import usage  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal test fixtures
# ---------------------------------------------------------------------------

_CFG = {"verify": "./scripts/test", "branch_prefix": "task/"}
_LAYOUT: dict[str, object] = {"agents": (), "preamble": False}


def _task(**overrides: object) -> dict[str, object]:
    """The smallest task build_command and recommend_dispatch both accept."""
    defaults: dict[str, object] = dict(
        id="O5",
        title="a task",
        routing="impl",
        touches=["scripts/backlog.py"],
        exit_criteria=["it works"],
        description="",
    )
    defaults.update(overrides)
    return defaults


def _big_task(**overrides: object) -> dict[str, object]:
    """A task over recommend_dispatch's L threshold (touches + criteria > 7)."""
    return _task(
        touches=[f"src/f{n}.py" for n in range(6)],
        exit_criteria=[f"criterion {n}" for n in range(4)],
        **overrides,
    )


def _effort_of(argv: list[str]) -> str | None:
    return argv[argv.index("--effort") + 1] if "--effort" in argv else None


# ---------------------------------------------------------------------------
# build_command: --effort is model-gated
# ---------------------------------------------------------------------------


def test_effort_is_emitted_for_models_that_have_an_effort_control() -> None:
    for model in bl.EFFORT_MODELS:
        argv = bl.build_command(_task(), _CFG, _LAYOUT, model=model, effort="xhigh")
        assert _effort_of(argv) == "xhigh", f"{model} lost its --effort"


def test_effort_against_an_effortless_model_is_refused_not_silently_dropped() -> None:
    """haiku has no effort control -- the CLI errors on the flag rather than
    ignoring it, so emitting it anyway would trade a clear failure here for a
    failed dispatch later."""
    effortless = [m for m in bl.MODELS if m not in bl.EFFORT_MODELS]
    assert effortless, "MODELS lost the model this test exists for"
    for model in effortless:
        try:
            bl.build_command(_task(), _CFG, _LAYOUT, model=model, effort="high")
        except ValueError as exc:
            assert "--effort" in str(exc)
        else:
            raise AssertionError(f"{model} + --effort high was allowed")


def test_an_effortless_model_still_dispatches_without_an_effort_level() -> None:
    for model in bl.MODELS:
        argv = bl.build_command(_task(), _CFG, _LAYOUT, model=model, effort="default")
        assert _effort_of(argv) is None
        assert argv[argv.index("--model") + 1] == model


# ---------------------------------------------------------------------------
# recommend_dispatch: the size-derived seed
# ---------------------------------------------------------------------------


def test_largest_tasks_are_recommended_at_xhigh_not_below_the_cli_default() -> None:
    """Omitting --effort already runs at Claude Code's own default of xhigh,
    so recommending 'high' for the biggest tasks dialled the hardest work
    *down* from an unflagged dispatch."""
    rec = cockpit.recommend_dispatch(bl, _big_task(), {})
    assert (rec.backend, rec.model, rec.effort) == ("claude", "opus", "xhigh")


def test_a_task_pinning_an_effortless_model_is_recommended_no_effort() -> None:
    """A task's own `model` field wins over the size-derived default, so a
    large task can still land on haiku -- the seed must not then carry a
    level build_command would refuse."""
    effortless = next(m for m in bl.MODELS if m not in bl.EFFORT_MODELS)
    rec = cockpit.recommend_dispatch(bl, _big_task(model=effortless), {})
    assert rec.model == effortless
    assert rec.effort is None
    # The seed must be dispatchable as recommended.
    bl.build_command(
        _big_task(model=effortless), _CFG, _LAYOUT, model=rec.model, effort=rec.effort or "default"
    )


def test_every_recommended_effort_is_one_the_command_builder_accepts() -> None:
    """Whatever size a task is, the seed and the argv builder must agree."""
    for task in (_task(), _task(exit_criteria=[f"c{n}" for n in range(5)]), _big_task()):
        rec = cockpit.recommend_dispatch(bl, task, {})
        if rec.backend != "claude":
            continue
        assert rec.effort is None or rec.effort in bl.EFFORT_LEVELS
        bl.build_command(task, _CFG, _LAYOUT, model=rec.model, effort=rec.effort or "default")


# ---------------------------------------------------------------------------
# usage: the price and context tables those choices are judged by
# ---------------------------------------------------------------------------


def test_a_dated_model_id_prices_and_windows_as_its_undated_self() -> None:
    """Transcripts record the id the CLI invoked, which for Haiku carries a
    release date the tables are not keyed by. Exact-match .get() failed
    silently: the turn priced at $0.00 and was measured against the 1M
    default window instead of Haiku's 200K."""
    assert usage.base_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert usage.base_model("claude-opus-5") == "claude-opus-5"

    dated = usage.Turn(
        model="claude-haiku-4-5-20251001",
        timestamp="",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_1h_tokens=0,
        cache_creation_5m_tokens=0,
        web_search_requests=0,
        web_fetch_requests=0,
        duration_ms=0,
        session_id="s",
        git_branch="",
        cwd="",
        is_sidechain=False,
        tool_names=(),
    )
    assert usage._turn_cost(dated) == 1.0  # 1M input tokens at $1.00/1M

    windowed = cockpit.Usage(
        model="claude-haiku-4-5-20251001", input=0, output=0, cache_read=0, cache_write=0
    )
    assert windowed.window == 200_000


def test_sonnet_5_advisory_rate() -> None:
    assert usage.MODEL_PRICES["claude-sonnet-5"] == (2.0, 10.0)
