"""P2: soft delete, edit, and allow-list enforcement."""

from datetime import timedelta

from _helpers import NOW, make_db, make_repo

from cradle.models import (
    FeedEvent,
    FeedMethod,
    UneditableFieldError,
    UnknownTableError,
)

BASE = {"event_id": None, "baby_id": 1, "logged_by": "phone"}


def _seed() -> tuple[object, int]:
    repo = make_repo(make_db())
    fid = repo.insert_feed(FeedEvent(ts=NOW, method=FeedMethod.BREAST_LEFT, **BASE))
    return repo, fid


def test_soft_delete_excludes_from_list() -> None:
    repo, fid = _seed()
    assert len(repo.list_feeds()) == 1
    repo.soft_delete("feed", fid)
    assert repo.list_feeds() == []


def test_edit_sets_value_and_edited_at() -> None:
    repo, fid = _seed()
    new_ts = NOW - timedelta(minutes=20)
    repo.edit_event("feed", fid, {"ts": new_ts, "volume_ml": 70})
    (f,) = repo.list_feeds()
    assert f.ts == new_ts
    assert f.volume_ml == 70
    row = repo._db.conn.execute("SELECT edited_at FROM feed WHERE id=?", (fid,)).fetchone()
    assert row["edited_at"] is not None


def test_unknown_table_rejected() -> None:
    repo, fid = _seed()
    for call in (lambda: repo.soft_delete("baby", fid),
                 lambda: repo.edit_event("sqlite_master", fid, {"ts": NOW})):
        try:
            call()
        except UnknownTableError:
            continue
        raise AssertionError("allow-list not enforced")


def test_uneditable_field_rejected() -> None:
    repo, fid = _seed()
    try:
        repo.edit_event("feed", fid, {"baby_id": 99})
    except UneditableFieldError:
        return
    raise AssertionError("column allow-list not enforced")


def test_edit_ignores_deleted_rows() -> None:
    repo, fid = _seed()
    repo.soft_delete("feed", fid)
    repo.edit_event("feed", fid, {"volume_ml": 999})
    assert repo.list_feeds() == []
