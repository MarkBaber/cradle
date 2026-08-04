"""U2: timezone normalisation at the browser boundary."""

from datetime import UTC, datetime

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.models.timefmt import to_local, to_utc  # noqa: E402


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
