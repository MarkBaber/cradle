"""Notification de-duplication and acknowledgement (task A2)."""

from datetime import UTC, datetime

from cradle.models import AlertSeverity, Finding
from cradle.repos.db import Db


class AlertLogRepo:
    def __init__(self, db: Db) -> None:
        self._db = db

    def record_if_new(self, finding: Finding) -> bool:
        """True if this fingerprint had not been seen, i.e. it should notify.

        The UNIQUE constraint on fingerprint is the actual guard, so concurrent
        sweeps cannot both decide to notify.
        """
        cur = self._db.conn.execute(
            "INSERT OR IGNORE INTO alert_log"
            " (fingerprint, rule_id, severity, message, ts) VALUES (?,?,?,?,?)",
            (
                finding.fingerprint,
                finding.rule_id,
                finding.severity.value,
                finding.message,
                finding.ts.isoformat(),
            ),
        )
        self._db.conn.commit()
        return cur.rowcount == 1

    def unacknowledged(self, severity: AlertSeverity | None = None) -> list[Finding]:
        sql = "SELECT * FROM alert_log WHERE acknowledged_at IS NULL"
        params: list[object] = []
        if severity is not None:
            sql += " AND severity = ?"
            params.append(severity.value)
        sql += " ORDER BY ts DESC"
        return [
            Finding(
                rule_id=r["rule_id"],
                severity=AlertSeverity(r["severity"]),
                message=r["message"],
                fingerprint=r["fingerprint"],
                ts=datetime.fromisoformat(r["ts"]),
                acknowledged_at=datetime.fromisoformat(r["acknowledged_at"])
                if r["acknowledged_at"]
                else None,
            )
            for r in self._db.conn.execute(sql, params).fetchall()
        ]

    def all(self, severity: AlertSeverity | None = None) -> list[Finding]:
        sql = "SELECT * FROM alert_log"
        params: list[object] = []
        if severity is not None:
            sql += " WHERE severity = ?"
            params.append(severity.value)
        sql += " ORDER BY ts DESC"
        return [
            Finding(
                rule_id=r["rule_id"],
                severity=AlertSeverity(r["severity"]),
                message=r["message"],
                fingerprint=r["fingerprint"],
                ts=datetime.fromisoformat(r["ts"]),
                acknowledged_at=datetime.fromisoformat(r["acknowledged_at"])
                if r["acknowledged_at"]
                else None,
            )
            for r in self._db.conn.execute(sql, params).fetchall()
        ]

    def acknowledge(self, fingerprint: str, acknowledged_at: datetime | None = None) -> bool:
        ack_str = (acknowledged_at or datetime.now(UTC)).isoformat()
        cur = self._db.conn.execute(
            "UPDATE alert_log SET acknowledged_at = ?"
            " WHERE fingerprint = ? AND acknowledged_at IS NULL",
            (ack_str, fingerprint),
        )
        self._db.conn.commit()
        return cur.rowcount == 1

    def auto_dismiss(self, older_than: datetime, as_of: datetime | None = None) -> int:
        ack_str = (as_of or datetime.now(UTC)).isoformat()
        cur = self._db.conn.execute(
            "UPDATE alert_log SET acknowledged_at = ?"
            " WHERE acknowledged_at IS NULL AND ts < ?",
            (ack_str, older_than.isoformat()),
        )
        self._db.conn.commit()
        return cur.rowcount

    def dump(self) -> list[dict[str, object]]:
        return [dict(r) for r in self._db.conn.execute("SELECT * FROM alert_log ORDER BY id")]

    def restore(self, rows: list[dict[str, object]]) -> int:
        if not rows:
            return 0
        cols = list(rows[0])
        self._db.conn.executemany(
            f"INSERT OR IGNORE INTO alert_log ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows],
        )
        self._db.conn.commit()
        return len(rows)
