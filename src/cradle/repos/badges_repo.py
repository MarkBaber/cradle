"""Achievement catalog + award repository (task U42).

Two tables (migration 0007): achievement_definition (predefined, seeded by
the app; and custom, authored via /achievements' builder form) and
achievement_award, one row per (baby_id, badge_key), additive-only - a
repeat qualifying event updates the existing row's count/last_awarded_at,
never inserts a second one and never decrements anything.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from sqlite3 import Row

from cradle.models import AchievementAward, AchievementDef, AchievementSource, Rarity, RuleType
from cradle.repos.db import Db


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _def_of(r: Row) -> AchievementDef:
    return AchievementDef(
        key=r["key"],
        name=r["name"],
        description=r["description"],
        rarity=Rarity(r["rarity"]),
        rule_type=RuleType(r["rule_type"]),
        domain=r["domain"],
        field=r["field"],
        match_value=r["match_value"],
        threshold=r["threshold"],
        repeatable=bool(r["repeatable"]),
        icon=r["icon"],
        source=AchievementSource(r["source"]),
        celebrate_every=tuple(int(x) for x in r["celebrate_every"].split(",") if x),
    )


def _award_of(r: Row) -> AchievementAward:
    return AchievementAward(
        baby_id=r["baby_id"],
        badge_key=r["badge_key"],
        count=r["count"],
        first_awarded_at=datetime.fromisoformat(r["first_awarded_at"]),
        last_awarded_at=datetime.fromisoformat(r["last_awarded_at"]),
    )


class BadgesRepo:
    def __init__(self, db: Db) -> None:
        self._db = db

    # ------------------------------------------------------------ catalog
    def seed_predefined(self, defs: Iterable[AchievementDef]) -> None:
        """Idempotent: INSERT OR IGNORE by key, so re-seeding on every app
        start never overwrites a predefined entry's award history and never
        touches a custom entry's key namespace."""
        for d in defs:
            self._db.conn.execute(
                "INSERT OR IGNORE INTO achievement_definition"
                " (key,name,description,rarity,rule_type,domain,field,match_value,"
                "  threshold,repeatable,icon,source,celebrate_every,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    d.key,
                    d.name,
                    d.description,
                    d.rarity.value,
                    d.rule_type.value,
                    d.domain,
                    d.field,
                    d.match_value,
                    d.threshold,
                    int(d.repeatable),
                    d.icon,
                    d.source.value,
                    ",".join(str(c) for c in d.celebrate_every),
                    _now_iso(),
                ),
            )
        self._db.conn.commit()

    def insert_custom(self, d: AchievementDef) -> None:
        """Raises sqlite3.IntegrityError if d.key already exists (the key
        PRIMARY KEY doubles as the dedup-by-name guard for custom entries)."""
        self._db.conn.execute(
            "INSERT INTO achievement_definition"
            " (key,name,description,rarity,rule_type,domain,field,match_value,"
            "  threshold,repeatable,icon,source,celebrate_every,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d.key,
                d.name,
                d.description,
                d.rarity.value,
                d.rule_type.value,
                d.domain,
                d.field,
                d.match_value,
                d.threshold,
                int(d.repeatable),
                d.icon,
                d.source.value,
                ",".join(str(c) for c in d.celebrate_every),
                _now_iso(),
            ),
        )
        self._db.conn.commit()

    def list_definitions(self) -> list[AchievementDef]:
        rows = self._db.conn.execute(
            "SELECT * FROM achievement_definition ORDER BY source, key"
        ).fetchall()
        return [_def_of(r) for r in rows]

    def get_definition(self, key: str) -> AchievementDef | None:
        r = self._db.conn.execute(
            "SELECT * FROM achievement_definition WHERE key = ?", (key,)
        ).fetchone()
        return None if r is None else _def_of(r)

    # -------------------------------------------------------------- awards
    def get_award(self, baby_id: int, badge_key: str) -> AchievementAward | None:
        r = self._db.conn.execute(
            "SELECT * FROM achievement_award WHERE baby_id = ? AND badge_key = ?",
            (baby_id, badge_key),
        ).fetchone()
        return None if r is None else _award_of(r)

    def list_awards(self, baby_id: int) -> dict[str, AchievementAward]:
        rows = self._db.conn.execute(
            "SELECT * FROM achievement_award WHERE baby_id = ?", (baby_id,)
        ).fetchall()
        return {r["badge_key"]: _award_of(r) for r in rows}

    def record_award(
        self, baby_id: int, badge_key: str, at: datetime, increment: int = 1
    ) -> AchievementAward:
        """Additive-only upsert (task U42): a badge_key's first qualifying
        event inserts the row at count=increment; every later one updates
        count += increment and last_awarded_at, never a second row - the
        same ON CONFLICT ... DO UPDATE shape repos/baby_repo.py's upsert
        already uses for this app's other per-baby singleton."""
        at_iso = at.isoformat()
        self._db.conn.execute(
            "INSERT INTO achievement_award"
            " (baby_id,badge_key,count,first_awarded_at,last_awarded_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(baby_id,badge_key) DO UPDATE SET"
            " count = count + excluded.count, last_awarded_at = excluded.last_awarded_at",
            (baby_id, badge_key, increment, at_iso, at_iso),
        )
        self._db.conn.commit()
        award = self.get_award(baby_id, badge_key)
        assert award is not None
        return award
