"""X1: export completeness and exact round-trip."""

import json
from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import (
    FeedMethod,
    GrowthMeasure,
    NappyKind,
    StoolColour,
    UnknownTableError,
)
from cradle.repos.alert_log_repo import AlertLogRepo
from cradle.repos.baby_repo import BabyRepo
from cradle.services.export_service import DOMAINS, ExportService
from cradle.services.logging_service import LoggingService


def _seeded():
    db = make_db()
    repo = make_repo(db)
    log = LoggingService(repo, clock())
    log.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=90, ts=NOW - timedelta(hours=2))
    log.log_nappy(NappyKind.DIRTY, StoolColour.YELLOW, ts=NOW - timedelta(hours=1))
    log.toggle_sleep(ts=NOW - timedelta(hours=3))
    log.log_growth(GrowthMeasure.WEIGHT, 3500, ts=NOW)
    log.log_temperature(36.9, ts=NOW)
    log.log_milestone("social", "First smile", ts=NOW)
    log.log_note("vitamin D", ("meds",), ts=NOW)
    deleted = log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(hours=6))
    log.undo("feed", deleted)
    svc = ExportService(repo, BabyRepo(db), AlertLogRepo(db), "0.1.0")
    return db, repo, svc


def test_json_export_covers_every_domain_and_the_profile() -> None:
    _, _, svc = _seeded()
    data = json.loads(svc.export_json())
    assert data["format"] == 1
    assert data["baby"]["name"] == "Test"
    assert set(data["events"]) == set(DOMAINS)
    assert len(data["events"]["feed"]) == 2, "soft-deleted rows are part of the record"


def test_round_trip_is_exact() -> None:
    _, _, svc = _seeded()
    original = json.loads(svc.export_json())

    fresh_db = make_db(seed_baby=False)
    target = ExportService(make_repo(fresh_db), BabyRepo(fresh_db), AlertLogRepo(fresh_db), "0.1.0")
    restored_rows = target.import_json(json.dumps(original))
    assert restored_rows > 0

    restored = json.loads(target.export_json())
    assert restored["baby"] == original["baby"]
    assert restored["events"] == original["events"]
    assert restored["alert_log"] == original["alert_log"]


def test_import_rejects_unknown_format() -> None:
    _, _, svc = _seeded()
    try:
        svc.import_json(json.dumps({"format": 99}))
    except ValueError:
        return
    raise AssertionError("unknown export format must be rejected")


def test_csv_omits_deleted_rows() -> None:
    _, _, svc = _seeded()
    lines = svc.export_csv("feed").strip().splitlines()
    assert len(lines) == 2, "header plus the one live feed"
    assert "bottle_formula" in lines[1]


def test_csv_header_stable_when_domain_empty() -> None:
    db = make_db()
    svc = ExportService(make_repo(db), BabyRepo(db), AlertLogRepo(db), "0.1.0")
    header = svc.export_csv("milestone").strip()
    assert header.startswith("id,baby_id,ts,logged_by")
    assert "title" in header


def test_nappy_csv_header_identical_empty_and_populated() -> None:
    empty_db = make_db()
    empty_svc = ExportService(
        make_repo(empty_db), BabyRepo(empty_db), AlertLogRepo(empty_db), "0.1.0"
    )
    empty_header = empty_svc.export_csv("nappy").splitlines()[0]

    _, _, populated_svc = _seeded()
    populated_header = populated_svc.export_csv("nappy").splitlines()[0]

    assert empty_header == populated_header


def test_every_domain_exports() -> None:
    _, _, svc = _seeded()
    for domain in DOMAINS:
        assert svc.export_csv(domain).startswith("id,")


def test_unknown_domain_rejected() -> None:
    _, _, svc = _seeded()
    try:
        svc.export_csv("sqlite_master")
    except UnknownTableError:
        return
    raise AssertionError("domain allow-list not enforced")
