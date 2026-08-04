"""Single-row baby profile (D11: baby_id fixed at 1)."""

from datetime import date

from cradle.models import Baby, Sex
from cradle.repos.db import Db


class BabyRepo:
    def __init__(self, db: Db) -> None:
        self._db = db

    def upsert(self, baby: Baby) -> None:
        self._db.conn.execute(
            "INSERT INTO baby (baby_id, name, sex, dob, due_date, birth_weight_g)"
            " VALUES (1,?,?,?,?,?) ON CONFLICT(baby_id) DO UPDATE SET"
            " name=excluded.name, sex=excluded.sex, dob=excluded.dob,"
            " due_date=excluded.due_date, birth_weight_g=excluded.birth_weight_g",
            (baby.name, baby.sex.value, baby.dob.isoformat(),
             baby.due_date.isoformat(), baby.birth_weight_g),
        )
        self._db.conn.commit()

    def get(self) -> Baby | None:
        r = self._db.conn.execute("SELECT * FROM baby WHERE baby_id = 1").fetchone()
        if r is None:
            return None
        return Baby(
            baby_id=1, name=r["name"], sex=Sex(r["sex"]),
            dob=date.fromisoformat(r["dob"]), due_date=date.fromisoformat(r["due_date"]),
            birth_weight_g=r["birth_weight_g"],
        )
