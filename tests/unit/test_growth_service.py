"""G1: growth assessment, weight-loss maths, chart series, honest degradation."""

from datetime import date, timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import GrowthMeasure
from cradle.reference.lms import LmsRow, LmsTable
from cradle.repos.baby_repo import BabyRepo
from cradle.services.growth_service import CENTILES, GrowthService
from cradle.services.logging_service import LoggingService

W = GrowthMeasure.WEIGHT
DOB = date(2026, 7, 1)


def _table() -> LmsTable:
    """Flat synthetic reference: M grows 3400 -> 4400 over 60 days, L=1, S=0.12."""
    rows = [LmsRow(a, 1.0, 3400.0 + (1000.0 * a / 60.0), 0.12) for a in (0, 30, 60)]
    return LmsTable({(W.value, "female"): rows}, "synthetic-v1")


def _build(
    table: LmsTable | None = None, birth_g: int = 3400, due: date = DOB
) -> tuple[LoggingService, GrowthService]:
    db = make_db(dob=DOB, due=due)
    BabyRepo(db).upsert(
        BabyRepo(db)
        .get()
        .__class__(
            baby_id=1,
            name="Test",
            sex=BabyRepo(db).get().sex,
            dob=DOB,
            due_date=due,
            birth_weight_g=birth_g,
        )
    )
    repo = make_repo(db)
    return (
        LoggingService(repo, clock()),
        GrowthService(repo, BabyRepo(db), table, None if table else "reference data not installed"),
    )


def test_no_profile_returns_none() -> None:
    db = make_db(seed_baby=False)
    svc = GrowthService(make_repo(db), BabyRepo(db), _table())
    assert svc.assessment() is None
    assert svc.centile_chart_series(W) is None


def test_weight_loss_percentage() -> None:
    log, growth = _build(_table(), birth_g=3400)
    log.log_growth(W, 3060, ts=NOW)  # exactly 10% below birth weight
    a = growth.assessment()
    assert a is not None
    assert abs(a.weight_loss_pct - 10.0) < 1e-9
    assert a.regained_birth_weight is False


def test_regain_detected() -> None:
    log, growth = _build(_table(), birth_g=3400)
    log.log_growth(W, 3400, ts=NOW)
    a = growth.assessment()
    assert a is not None
    assert a.regained_birth_weight is True
    assert abs(a.weight_loss_pct) < 1e-9


def test_zscore_uses_corrected_age_for_preterm() -> None:
    """Born 8 weeks early: a 60-day-old is assessed at corrected age 4 days."""
    preterm_due = DOB + timedelta(weeks=8)
    log, growth = _build(_table(), due=preterm_due)
    on = NOW.replace(year=2026, month=8, day=30)  # 60 days old
    log.log_growth(W, 3400, ts=on)
    a = growth.assessment()
    assert a is not None
    assert a.corrected is True
    (m,) = a.measures
    assert m.age_days == 60
    assert m.corrected_age_days == 4
    # At corrected age 4, M ~= 3467, so 3400g sits just below the median.
    assert m.z is not None and -0.3 < m.z < 0.0


def test_term_baby_uses_chronological_age() -> None:
    log, growth = _build(_table())
    log.log_growth(W, 3400, ts=NOW)
    a = growth.assessment()
    assert a is not None
    assert a.corrected is False
    (m,) = a.measures
    assert m.age_days == m.corrected_age_days == 14


def test_baseline_and_latest_z_exposed_for_alerts() -> None:
    log, growth = _build(_table())
    log.log_growth(W, 3400, ts=NOW - timedelta(days=13))
    log.log_growth(W, 3900, ts=NOW)
    a = growth.assessment()
    assert a is not None
    assert a.baseline_weight_z is not None and a.latest_weight_z is not None
    assert a.latest_weight_z > a.baseline_weight_z


def test_missing_reference_degrades_without_inventing_centiles() -> None:
    log, growth = _build(table=None)
    log.log_growth(W, 3200, ts=NOW)
    a = growth.assessment()
    assert a is not None
    assert a.table_version is None
    (m,) = a.measures
    assert m.z is None and m.centile is None
    assert m.unavailable_reason
    assert m.value == 3200, "raw measurements still shown"
    assert a.weight_loss_pct is not None, "weight loss needs no reference table"


def test_out_of_range_age_reports_reason_not_crash() -> None:
    log, growth = _build(_table())
    log.log_growth(W, 6000, ts=NOW + timedelta(days=400))
    a = growth.assessment()
    assert a is not None
    (m,) = a.measures
    assert m.z is None
    assert "outside" in (m.unavailable_reason or "")


def test_chart_series_has_all_centile_curves_and_trajectory() -> None:
    log, growth = _build(_table())
    log.log_growth(W, 3400, ts=NOW - timedelta(days=13))
    log.log_growth(W, 3900, ts=NOW)
    s = growth.centile_chart_series(W)
    assert s is not None
    assert s.unavailable_reason is None
    assert set(s.curves) == {f"{c:g}" for c in CENTILES}
    assert all(len(v) == len(s.ages) for v in s.curves.values())
    assert s.trajectory == ((1, 3400.0, "02 Jul 2026"), (14, 3900.0, "15 Jul 2026"))


def test_chart_curves_are_ordered_by_centile() -> None:
    _, growth = _build(_table())
    s = growth.centile_chart_series(W)
    assert s is not None
    at_30 = [s.curves[f"{c:g}"][len(s.ages) // 2] for c in CENTILES]
    assert at_30 == sorted(at_30), "centile curves must not cross"


def test_frames_are_trajectory_prefixes() -> None:
    log, growth = _build(_table())
    for d in (0, 5, 10):
        log.log_growth(W, 3400 + d * 20, ts=NOW - timedelta(days=d))
    s = growth.centile_chart_series(W)
    assert s is not None
    assert s.frames == (1, 2, 3), "one animation frame per measurement (C3)"


def test_chart_series_without_reference_still_plots_trajectory() -> None:
    log, growth = _build(table=None)
    log.log_growth(W, 3400, ts=NOW)
    s = growth.centile_chart_series(W)
    assert s is not None
    assert s.curves == {}
    assert s.unavailable_reason
    assert len(s.trajectory) == 1, "logged data is still the parent's to see"
