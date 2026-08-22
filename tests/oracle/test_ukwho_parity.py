"""Oracle parity: LMS engine vs published UK-WHO values, +/-0.01 z (task R4).

Ground truth in ukwho_vectors.csv was sourced independently of
src/cradle/reference/lms.py (see that file's header) - never regenerate it by
calling this module's own zscore().

Permanent regression collateral - never delete once populated."""

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.models import GrowthMeasure, Sex  # noqa: E402
from cradle.reference.lms import load_table  # noqa: E402

VECTORS = Path(__file__).parent / "ukwho_vectors.csv"
TOLERANCE_Z = 0.01
MIN_VECTORS = 40


def _rows_from_text(text: str) -> list[dict[str, str]]:
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    return list(csv.DictReader(io.StringIO(body)))


def _read_vectors(path: Path) -> list[dict[str, str]]:
    return _rows_from_text(path.read_text(encoding="utf-8"))


def _assert_parity(rows: list[dict[str, str]]) -> None:
    assert rows, "vector file has no rows - task R4 is not done until it is populated"
    table = load_table()
    for row in rows:
        measure = GrowthMeasure(row["measure"])
        sex = Sex(row["sex"])
        age_days = int(row["age_days"])
        value = float(row["value"])
        expected_z = float(row["expected_z"])
        actual_z = table.zscore(measure, sex, age_days, value).z
        diff = abs(actual_z - expected_z)
        assert diff <= TOLERANCE_Z, (
            f"{row['measure']}/{row['sex']} age={age_days}d value={value}: "
            f"z={actual_z:.4f} expected {expected_z:.4f} (diff {diff:.4f} > {TOLERANCE_Z})"
        )


def test_ukwho_parity() -> None:
    _assert_parity(_read_vectors(VECTORS))


def test_ukwho_parity_has_at_least_40_vectors() -> None:
    assert len(_read_vectors(VECTORS)) >= MIN_VECTORS


def test_empty_vector_file_fails() -> None:
    """The empty-file bypass is gone: no rows must fail the gate, not pass it."""
    empty_rows = _rows_from_text("measure,sex,age_days,value,expected_z,note\n")
    try:
        _assert_parity(empty_rows)
    except AssertionError:
        return
    raise AssertionError("empty vector file must fail the parity gate, not pass silently")
