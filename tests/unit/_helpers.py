"""Shared fixtures. Stdlib only so the offline runner can use them."""

import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.models import Baby, Sex  # noqa: E402
from cradle.ports.clock import FixedClock  # noqa: E402
from cradle.repos.baby_repo import BabyRepo  # noqa: E402
from cradle.repos.db import Db  # noqa: E402
from cradle.repos.events_repo import EventsRepo  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
DOB = date(2026, 7, 1)


def make_db(seed_baby: bool = True, dob: date = DOB, due: date | None = None) -> Db:
    db = Db(Path(tempfile.mkdtemp()) / "t.db")
    db.migrate()
    if seed_baby:
        BabyRepo(db).upsert(Baby(
            baby_id=1, name="Test", sex=Sex.FEMALE, dob=dob,
            due_date=due or dob, birth_weight_g=3400,
        ))
    return db


def make_repo(db: Db) -> EventsRepo:
    return EventsRepo(db)


def clock(at: datetime = NOW) -> FixedClock:
    return FixedClock(at)
