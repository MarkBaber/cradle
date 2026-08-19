"""X1: export completeness and exact round-trip."""

import json
from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import (
    BatchState,
    BottleColour,
    BreastSide,
    ExpressionEvent,
    FeedMethod,
    GrowthMeasure,
    MilkBatch,
    MilkStore,
    NappyKind,
    StoolColour,
    UnknownTableError,
)
from cradle.repos.alert_log_repo import AlertLogRepo
from cradle.repos.baby_repo import BabyRepo
from cradle.services.export_service import DOMAINS, ExportService
from cradle.services.logging_service import LoggingService


def _batch(colour: BottleColour, state: BatchState, **kw: object) -> MilkBatch:
    return MilkBatch(
        batch_id=None,
        baby_id=1,
        expressed_at=NOW - timedelta(hours=1),
        stored_at=NOW,
        store=MilkStore.FRIDGE,
        colour=colour,
        volume_ml=60,
        state=state,
        logged_by="phone",
        **kw,
    )


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

    repo.insert_expression(
        ExpressionEvent(
            event_id=None,
            baby_id=1,
            ts=NOW,
            logged_by="phone",
            side=BreastSide.LEFT,
            volume_ml=45,
            duration_min=12,
            note="both sides sore",
        )
    )
    deleted_expression = repo.insert_expression(
        ExpressionEvent(
            event_id=None,
            baby_id=1,
            ts=NOW - timedelta(hours=5),
            logged_by="phone",
            side=BreastSide.RIGHT,
            volume_ml=30,
            duration_min=8,
            note="",
        )
    )
    repo.soft_delete("expression", deleted_expression)

    repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.STORED))
    repo.insert_milk_batch(
        _batch(BottleColour.GREEN, BatchState.THAWED, thawed_at=NOW + timedelta(hours=2))
    )
    repo.insert_milk_batch(
        _batch(
            BottleColour.RED,
            BatchState.OPENED,
            thawed_at=NOW + timedelta(hours=2),
            opened_at=NOW + timedelta(hours=3),
        )
    )
    repo.insert_milk_batch(
        _batch(BottleColour.YELLOW, BatchState.USED, used_at=NOW + timedelta(hours=4))
    )
    repo.insert_milk_batch(_batch(BottleColour.ORANGE, BatchState.DISCARDED))
    deleted_batch = repo.insert_milk_batch(_batch(BottleColour.PURPLE, BatchState.STORED))
    repo.soft_delete("milk_batch", deleted_batch)

    svc = ExportService(repo, BabyRepo(db), AlertLogRepo(db), "0.1.0")
    return db, repo, svc


def test_json_export_covers_every_domain_and_the_profile() -> None:
    _, _, svc = _seeded()
    data = json.loads(svc.export_json())
    assert data["format"] == 1
    assert data["baby"]["name"] == "Test"
    assert set(data["events"]) == set(DOMAINS)
    assert len(data["events"]["feed"]) == 2, "soft-deleted rows are part of the record"
    assert len(data["events"]["expression"]) == 2, "soft-deleted expression is part of the record"
    assert len(data["events"]["milk_batch"]) == 6, "soft-deleted batch is part of the record"
    assert {r["state"] for r in data["events"]["milk_batch"]} == {
        "stored",
        "thawed",
        "opened",
        "used",
        "discarded",
    }


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


def test_restore_precondition_holds_for_new_domains() -> None:
    """Restoring the new domains into a fresh, empty target must not trip the
    milk_batch colour-uniqueness index or any other schema constraint. Restore
    into an already-populated target is a separate, known defect (P3) and is
    not this task's concern."""
    _, _, svc = _seeded()
    original = json.loads(svc.export_json())

    fresh_db = make_db(seed_baby=False)
    target = ExportService(make_repo(fresh_db), BabyRepo(fresh_db), AlertLogRepo(fresh_db), "0.1.0")
    target.import_json(json.dumps(original))

    restored = json.loads(target.export_json())
    assert restored["events"]["expression"] == original["events"]["expression"]
    assert restored["events"]["milk_batch"] == original["events"]["milk_batch"]


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


def test_milk_batch_csv_omits_deleted_rows() -> None:
    _, _, svc = _seeded()
    lines = svc.export_csv("milk_batch").strip().splitlines()
    assert len(lines) == 6, "header plus five non-deleted batches; the soft-deleted one is excluded"


def test_expression_csv_header_pinned() -> None:
    _, _, svc = _seeded()
    header = svc.export_csv("expression").splitlines()[0]
    assert header == (
        "id,baby_id,ts,logged_by,side,volume_ml,duration_min,note,created_at,edited_at,deleted_at"
    )


def test_milk_batch_csv_header_pinned() -> None:
    _, _, svc = _seeded()
    header = svc.export_csv("milk_batch").splitlines()[0]
    assert header == (
        "id,baby_id,expressed_at,stored_at,store,colour,volume_ml,state,"
        "thawed_at,opened_at,used_at,expression_id,logged_by,created_at,edited_at,deleted_at"
    )


def test_csv_header_stable_when_domain_empty() -> None:
    db = make_db()
    svc = ExportService(make_repo(db), BabyRepo(db), AlertLogRepo(db), "0.1.0")
    header = svc.export_csv("milestone").strip()
    assert header.startswith("id,baby_id,ts,logged_by")
    assert "title" in header


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
