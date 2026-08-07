"""U2: timezone normalisation at the browser boundary. U9: configured display zone."""

import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.models import timefmt  # noqa: E402
from cradle.models.timefmt import to_local, to_utc  # noqa: E402


@contextmanager
def _config_path(path: Path):
    original = timefmt.CONFIG_PATH
    timefmt.CONFIG_PATH = path
    try:
        yield
    finally:
        timefmt.CONFIG_PATH = original


@contextmanager
def _configured_zone(name: str):
    """Point timefmt at a scratch rules_config.toml with [display].timezone = name."""
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "rules_config.toml"
        config.write_text(f'[display]\ntimezone = "{name}"\n')
        with _config_path(config):
            yield


def test_naive_input_becomes_aware_utc() -> None:
    result = to_utc(datetime(2026, 7, 15, 9, 30))
    assert result.tzinfo is not None
    assert result.utcoffset() == UTC.utcoffset(None)


def test_aware_input_preserved_as_utc() -> None:
    aware = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)
    assert to_utc(aware) == aware


def test_roundtrip_preserves_instant() -> None:
    original = datetime(2026, 7, 15, 9, 30)
    assert to_local(to_utc(original)).replace(tzinfo=None) == original


def test_missing_config_falls_back_to_utc() -> None:
    with _config_path(Path("/nonexistent/rules_config.toml")):
        result = to_utc(datetime(2026, 7, 15, 9, 30))
        assert result == datetime(2026, 7, 15, 9, 30, tzinfo=UTC)


def test_configured_zone_used_for_parse() -> None:
    with _configured_zone("Europe/London"):
        # 2026-06-15 is BST (UTC+1): naive 09:30 local -> 08:30 UTC.
        result = to_utc(datetime(2026, 6, 15, 9, 30))
        assert result == datetime(2026, 6, 15, 8, 30, tzinfo=UTC)


def test_configured_zone_used_for_render() -> None:
    with _configured_zone("Europe/London"):
        result = to_local(datetime(2026, 6, 15, 8, 30, tzinfo=UTC))
        assert (result.hour, result.minute) == (9, 30)
        assert result.utcoffset() == timedelta(hours=1)


def test_dst_boundary_spring_forward() -> None:
    """Europe/London clocks spring forward at 01:00 UTC on 2026-03-29."""
    with _configured_zone("Europe/London"):
        before = to_local(datetime(2026, 3, 29, 0, 30, tzinfo=UTC))
        assert (before.hour, before.minute) == (0, 30)
        assert before.utcoffset() == timedelta(0)

        after = to_local(datetime(2026, 3, 29, 1, 30, tzinfo=UTC))
        assert (after.hour, after.minute) == (2, 30)
        assert after.utcoffset() == timedelta(hours=1)


def test_dst_boundary_fall_back() -> None:
    """Europe/London clocks fall back at 01:00 UTC on 2026-10-25: local 01:30
    occurs twice, once as BST and once as GMT."""
    with _configured_zone("Europe/London"):
        bst = to_local(datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
        assert (bst.hour, bst.minute) == (1, 30)
        assert bst.utcoffset() == timedelta(hours=1)

        gmt = to_local(datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
        assert (gmt.hour, gmt.minute) == (1, 30)
        assert gmt.utcoffset() == timedelta(0)
