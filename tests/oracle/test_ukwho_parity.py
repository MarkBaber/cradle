"""Oracle parity: LMS engine vs published UK-WHO values, +/-0.01 z (task R4).

Ground truth in ukwho_vectors.csv was sourced independently of
src/cradle/reference/lms.py (see that file's header) - never regenerate it by
calling this module's own zscore().

Permanent regression collateral - never delete once populated."""

import csv
import io
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.models import GrowthMeasure, Sex  # noqa: E402
from cradle.reference.lms import load_table  # noqa: E402

VECTORS = Path(__file__).parent / "ukwho_vectors.csv"
TOLERANCE_Z = 0.01
MIN_VECTORS = 40

UNPOPULATED_REASON = (
    "ukwho_vectors.csv has no rows yet - task R2 has not vendored the UK-WHO "
    "LMS tables, so oracle parity is unverified, not passing"
)


def _rows_from_text(text: str) -> list[dict[str, str]]:
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    return list(csv.DictReader(io.StringIO(body)))


def _read_vectors(path: Path) -> list[dict[str, str]]:
    return _rows_from_text(path.read_text(encoding="utf-8"))


def _skip(reason: str) -> NoReturn:
    """Report `reason` as a genuine pytest skip when running inside a live
    pytest session (shows as <skipped> in junit.xml); otherwise raise
    ModuleNotFoundError naming R2, so scripts/offline_runner.py's existing
    missing-dep SKIP path reports it rather than a hard failure - it has no
    fixture support and only recognises that one exception as a skip."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        import pytest

        pytest.skip(reason)
    raise ModuleNotFoundError(reason, name="R2")


def _read_vectors_or_skip() -> list[dict[str, str]]:
    rows = _read_vectors(VECTORS)
    if not rows:
        _skip(UNPOPULATED_REASON)
    return rows


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
    _assert_parity(_read_vectors_or_skip())


def test_ukwho_parity_has_at_least_40_vectors() -> None:
    assert len(_read_vectors_or_skip()) >= MIN_VECTORS


def test_empty_vector_file_fails() -> None:
    """The empty-file bypass is gone: no rows must fail the gate, not pass it.

    This calls the comparison harness directly, bypassing the file-level skip
    in test_ukwho_parity() above - task R4's own guarantee that the harness
    itself never silently passes an empty vector set, independent of Q3's
    skip-while-unpopulated behaviour for the real file."""
    empty_rows = _rows_from_text("measure,sex,age_days,value,expected_z,note\n")
    try:
        _assert_parity(empty_rows)
    except AssertionError:
        return
    raise AssertionError("empty vector file must fail the parity gate, not pass silently")


@contextmanager
def _empty_vectors_file():  # type: ignore[no-untyped-def]
    """Point the module-level VECTORS at a temp header-only CSV for the
    duration of the block, restoring it after. Plain stdlib (tempfile,
    contextlib) rather than pytest's monkeypatch/tmp_path fixtures, so these
    tests still run with zero arguments under offline_runner.py."""
    global VECTORS
    original = VECTORS
    fd, path_str = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("measure,sex,age_days,value,expected_z,note\n")
        VECTORS = Path(path_str)
        yield
    finally:
        VECTORS = original
        os.unlink(path_str)


@contextmanager
def _no_live_pytest_session():  # type: ignore[no-untyped-def]
    saved = os.environ.pop("PYTEST_CURRENT_TEST", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["PYTEST_CURRENT_TEST"] = saved


@contextmanager
def _live_pytest_session():  # type: ignore[no-untyped-def]
    saved = os.environ.get("PYTEST_CURRENT_TEST")
    os.environ["PYTEST_CURRENT_TEST"] = "tests/oracle/test_ukwho_parity.py::fake (call)"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = saved


def _assert_skips_naming_r2(run) -> None:  # type: ignore[no-untyped-def]
    try:
        run()
    except ModuleNotFoundError as exc:
        assert exc.name == "R2", f"expected ModuleNotFoundError naming R2, got name={exc.name!r}"
        return
    except AssertionError:
        raise AssertionError(
            "empty vector file with no live pytest session must SKIP naming R2, "
            "not raise AssertionError"
        ) from None
    raise AssertionError(
        "empty vector file with no live pytest session must SKIP "
        "(ModuleNotFoundError named R2), not pass silently"
    )


def test_ukwho_parity_skips_naming_r2_outside_live_pytest() -> None:
    """Q3: zero data rows with no live pytest session (offline_runner.py's
    exec_module/call path) must SKIP naming R2 - not pass, not AssertionError."""
    with _empty_vectors_file(), _no_live_pytest_session():
        _assert_skips_naming_r2(test_ukwho_parity)


def test_ukwho_parity_min_vectors_skips_naming_r2_outside_live_pytest() -> None:
    """Q3: the >=40-vectors gate must also SKIP naming R2 while unpopulated,
    not fail its count assertion."""
    with _empty_vectors_file(), _no_live_pytest_session():
        _assert_skips_naming_r2(test_ukwho_parity_has_at_least_40_vectors)


def test_ukwho_parity_skips_via_pytest_skip_inside_live_pytest() -> None:
    """Q3: with a live pytest session (PYTEST_CURRENT_TEST set), the skip must
    go through pytest.skip() so it shows as <skipped> in junit.xml, with a
    reason naming R2. Guarded: offline_runner.py reports this test itself as
    a missing-dep skip where pytest isn't installed."""
    import pytest

    with _empty_vectors_file(), _live_pytest_session():
        with pytest.raises(pytest.skip.Exception) as excinfo:
            test_ukwho_parity()
        assert "R2" in str(excinfo.value)
