"""WhatsApp echo audit trail and destination setting (task N5)."""

from datetime import date, datetime

from cradle.repos.db import Db


class ChatLogRepo:
    def __init__(self, db: Db) -> None:
        self._db = db

    def get_chat_id(self) -> str:
        """The runtime-editable WhatsApp destination (set from /settings).

        Not TOML-backed like N3's ntfy topic - see migration 0008's comment
        for why: this task's touches list has no room for rules_config.toml.
        """
        row = self._db.conn.execute("SELECT chat_id FROM whatsapp_settings WHERE id = 1").fetchone()
        return str(row["chat_id"]) if row is not None else ""

    def set_chat_id(self, chat_id: str) -> None:
        self._db.conn.execute("UPDATE whatsapp_settings SET chat_id = ? WHERE id = 1", (chat_id,))
        self._db.conn.commit()

    def last_local_date(self) -> date | None:
        """The latest local_date any message has ever been filed under.

        Drives the date-header logic: a new local day since this one gets a
        'DD/MM/YY' line prepended, regardless of whether that last attempt
        actually succeeded (task N5). Deliberately MAX(local_date) rather
        than the most-recently-inserted row's date: log_* accepts an
        explicit backdated `ts` (the "adjust time" flow, U2), so a backdated
        entry logged after a same-day real-time one must not make the next
        real-time message think its day's header hasn't gone out yet.
        """
        row = self._db.conn.execute("SELECT MAX(local_date) AS local_date FROM chat_log").fetchone()
        local_date = row["local_date"] if row is not None else None
        return date.fromisoformat(local_date) if local_date is not None else None

    def record(
        self,
        table: str,
        event_id: int,
        ts: datetime,
        local_date: date,
        text: str,
        sent_at: datetime,
        success: bool,
    ) -> int:
        cur = self._db.conn.execute(
            "INSERT INTO chat_log"
            " (table_name, event_id, ts, local_date, text, sent_at, success)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                table,
                event_id,
                ts.isoformat(),
                local_date.isoformat(),
                text,
                sent_at.isoformat(),
                int(success),
            ),
        )
        self._db.conn.commit()
        return int(cur.lastrowid or 0)
