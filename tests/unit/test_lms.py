"""R1: LMS maths, interpolation, inversion, and strict data loading.

Synthetic tables only — these tests pin the *arithmetic*. Agreement with the
real UK-WHO reference is a separate gate (task R4, tests/oracle/).
"""

import sys
import tempfile
from math import exp, isclose, log
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.models import GrowthMeasure, ReferenceDataMissingError, Sex  # noqa: E402
from cradle.reference.lms import (  # noqa: E402
    LmsRow,
    LmsTable,
    load_table,
    z_for_centile,
)

W, F = GrowthMeasure.WEIGHT, Sex.FEMALE


def table(*rows: LmsRow, version: str = "synthetic") -> LmsTable:
    return LmsTable({(W.value, F.value): list(rows)}, version)


def test_lms_formula_l_nonzero() -> None:
    t = table(LmsRow(0, 1.0, 3400.0, 0.1), LmsRow(10, 1.0, 3400.0, 0.1))
    # L=1 => z = ((x/M) - 1) / S
    assert isclose(t.zscore(W, F, 0, 3400.0).z, 0.0, abs_tol=1e-12)
    assert isclose(t.zscore(W, F, 0, 3740.0).z, 1.0, rel_tol=1e-12)


def test_lms_formula_l_zero_is_lognormal() -> None:
    t = table(LmsRow(0, 0.0, 3400.0, 0.12), LmsRow(10, 0.0, 3400.0, 0.12))
    expected = log(3800.0 / 3400.0) / 0.12
    assert isclose(t.zscore(W, F, 0, 3800.0).z, expected, rel_tol=1e-12)
    assert isclose(t.zscore(W, F, 0, 3400.0 * exp(0.12)).z, 1.0, rel_tol=1e-12)


def test_centile_matches_normal_distribution() -> None:
    t = table(LmsRow(0, 1.0, 100.0, 0.1), LmsRow(10, 1.0, 100.0, 0.1))
    assert isclose(t.zscore(W, F, 0, 100.0).centile, 50.0, abs_tol=1e-9)
    assert isclose(t.zscore(W, F, 0, 100.0 * 1.196).centile, 97.5, abs_tol=0.05)


def test_linear_interpolation_of_lms() -> None:
    t = table(LmsRow(0, 1.0, 3000.0, 0.10), LmsRow(10, 1.0, 4000.0, 0.20))
    row = t.lms(W, F, 5)
    assert isclose(row.M, 3500.0)
    assert isclose(row.S, 0.15)
    row_q = t.lms(W, F, 2)
    assert isclose(row_q.M, 3200.0)


def test_exact_age_hit_is_not_interpolated() -> None:
    t = table(LmsRow(0, 1.0, 3000.0, 0.1), LmsRow(10, 1.0, 4000.0, 0.2))
    assert t.lms(W, F, 10).M == 4000.0
    assert t.lms(W, F, 0).M == 3000.0


def test_out_of_range_raises_lookup_error() -> None:
    t = table(LmsRow(0, 1.0, 3400.0, 0.1), LmsRow(10, 1.0, 3600.0, 0.1))
    for age in (-1, 11):
        try:
            t.lms(W, F, age)
        except LookupError:
            continue
        raise AssertionError(f"age {age} should be outside the table")


def test_unknown_measure_or_sex_raises() -> None:
    t = table(LmsRow(0, 1.0, 3400.0, 0.1), LmsRow(10, 1.0, 3600.0, 0.1))
    try:
        t.zscore(W, Sex.MALE, 5, 3400.0)
    except ReferenceDataMissingError:
        return
    raise AssertionError("missing series must not silently fall back")


def test_non_positive_measurement_rejected() -> None:
    t = table(LmsRow(0, 1.0, 3400.0, 0.1), LmsRow(10, 1.0, 3600.0, 0.1))
    try:
        t.zscore(W, F, 0, 0.0)
    except ValueError:
        return
    raise AssertionError("zero weight must be rejected")


def test_value_at_z_inverts_zscore() -> None:
    for L in (1.0, 0.0, -0.35):
        t = table(LmsRow(0, L, 3400.0, 0.12), LmsRow(10, L, 3600.0, 0.12))
        for z in (-2.0, -0.5, 0.0, 1.33, 2.67):
            x = t.value_at_z(W, F, 4, z)
            assert isclose(t.zscore(W, F, 4, x).z, z, abs_tol=1e-9), (L, z)


def test_value_at_centile_endpoints() -> None:
    t = table(LmsRow(0, -0.3, 3400.0, 0.12), LmsRow(10, -0.3, 3600.0, 0.12))
    low = t.value_at_centile(W, F, 5, 0.4)
    high = t.value_at_centile(W, F, 5, 99.6)
    mid = t.value_at_centile(W, F, 5, 50.0)
    assert low < mid < high


def test_z_for_centile_known_values() -> None:
    assert isclose(z_for_centile(50.0), 0.0, abs_tol=1e-12)
    assert isclose(z_for_centile(97.5), 1.959964, abs_tol=1e-5)
    assert isclose(z_for_centile(0.4), -2.652070, abs_tol=1e-5)
    for bad in (0.0, 100.0, -1.0):
        try:
            z_for_centile(bad)
        except ValueError:
            continue
        raise AssertionError(f"centile {bad} should be rejected")


# ------------------------------------------------------------- loader strictness


def _write(text: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "lms.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_file_raises() -> None:
    try:
        load_table(Path("/nonexistent/lms.csv"))
    except ReferenceDataMissingError:
        return
    raise AssertionError("absent table must raise")


def test_empty_table_raises_rather_than_returning_empty() -> None:
    try:
        load_table(_write("measure,sex,age_days,L,M,S\n"))
    except ReferenceDataMissingError as exc:
        assert "R2" in str(exc), "error should name the outstanding task"
        return
    raise AssertionError("header-only table must raise")


def test_malformed_row_raises() -> None:
    csv = "measure,sex,age_days,L,M,S\nweight,female,0,notanumber,3400,0.1\n"
    try:
        load_table(_write(csv))
    except ReferenceDataMissingError:
        return
    raise AssertionError("malformed row must raise")


def test_single_row_series_rejected() -> None:
    csv = "measure,sex,age_days,L,M,S\nweight,female,0,1,3400,0.1\n"
    try:
        load_table(_write(csv))
    except ReferenceDataMissingError:
        return
    raise AssertionError("a series needs two rows to interpolate")


def test_duplicate_ages_rejected() -> None:
    csv = "measure,sex,age_days,L,M,S\nweight,female,0,1,3400,0.1\nweight,female,0,1,3500,0.1\n"
    try:
        load_table(_write(csv))
    except ReferenceDataMissingError:
        return
    raise AssertionError("duplicate ages must raise")


def test_valid_table_loads_and_reports_version() -> None:
    csv = "measure,sex,age_days,L,M,S\nweight,female,0,1,3400,0.1\nweight,female,10,1,3600,0.1\n"
    version_file = _write("test-v1\nprovenance notes\n")
    t = load_table(_write(csv), version_file)
    assert t.version == "test-v1"
    assert t.age_range(W, F) == (0, 10)
    assert t.zscore(W, F, 0, 3400.0).table_version == "test-v1"
