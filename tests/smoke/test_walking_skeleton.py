"""Phase 0 smoke test: log a feed -> stored in SQLite -> visible in history.

Runs under pytest or scripts/offline_runner.py (stdlib only - no fastapi needed:
the skeleton path is exercised at the service/repo layer; route-level coverage
is added in T1 once fastapi is installable).
"""

from datetime import UTC, date, datetime

from cradle.models import Baby, FeedMethod, Sex
from cradle.ports.clock import FixedClock
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.db import Db
from cradle.repos.events_repo import EventsRepo
from cradle.services.logging_service import LoggingService


def test_feed_roundtrip(tmp_path=None) -> None:
    import tempfile
    from pathlib import Path

    workdir = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    db = Db(workdir / "smoke.db")
    assert db.migrate() >= 1

    BabyRepo(db).upsert(
        Baby(
            baby_id=1,
            name="Test Baby",
            sex=Sex.FEMALE,
            dob=date(2026, 7, 1),
            due_date=date(2026, 7, 8),
            birth_weight_g=3400,
        )
    )
    assert BabyRepo(db).get() is not None

    now = datetime(2026, 7, 13, 3, 15, tzinfo=UTC)
    svc = LoggingService(EventsRepo(db), FixedClock(now))

    event_id = svc.log_feed(FeedMethod.BREAST_LEFT, logged_by="test-device")
    assert event_id == 1

    feeds = svc.recent_feeds()
    assert len(feeds) == 1
    assert feeds[0].method is FeedMethod.BREAST_LEFT
    assert feeds[0].ts == now
    assert feeds[0].logged_by == "test-device"
    db.close()


def test_public_api_importable() -> None:
    import cradle
    import cradle.alerts
    import cradle.models
    import cradle.ports
    import cradle.reference
    import cradle.repos
    import cradle.services

    assert cradle.__version__
