"""Export and restore (task X1).

The point of this app is that in ten years someone can still read the record.
So the JSON export is complete (profile, every event including soft-deleted
ones, and the alert log), self-describing, and round-trips exactly. CSV is the
convenience format for spreadsheets and loses the deleted rows.
"""

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

from cradle.models import Baby, Sex, UnknownTableError
from cradle.repos.alert_log_repo import AlertLogRepo
from cradle.repos.baby_repo import BabyRepo
from cradle.repos.events_repo import EDITABLE, EventsRepo

EXPORT_FORMAT = 1
DOMAINS = tuple(sorted(EDITABLE))


class ExportService:
    def __init__(
        self,
        repo: EventsRepo,
        baby_repo: BabyRepo,
        alert_log: AlertLogRepo,
        app_version: str,
    ) -> None:
        self._repo = repo
        self._baby_repo = baby_repo
        self._alert_log = alert_log
        self._app_version = app_version

    # -------------------------------------------------------------- export
    def payload(self) -> dict[str, Any]:
        baby = self._baby_repo.get()
        return {
            "format": EXPORT_FORMAT,
            "app_version": self._app_version,
            "exported_at": datetime.now(UTC).isoformat(),
            "baby": None
            if baby is None
            else {
                "baby_id": baby.baby_id,
                "name": baby.name,
                "sex": baby.sex.value,
                "dob": baby.dob.isoformat(),
                "due_date": baby.due_date.isoformat(),
                "birth_weight_g": baby.birth_weight_g,
            },
            "events": {d: self._repo.dump(d) for d in DOMAINS},
            "alert_log": self._alert_log.dump(),
        }

    def export_json(self) -> str:
        return json.dumps(self.payload(), indent=2, sort_keys=True)

    def export_csv(self, domain: str) -> str:
        if domain not in EDITABLE:
            raise UnknownTableError(domain)
        rows = self._repo.dump(domain, include_deleted=False)
        buffer = io.StringIO()
        header = list(rows[0]) if rows else _csv_header(domain)
        writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()

    # ------------------------------------------------------------- restore
    def import_json(self, text: str) -> int:
        """Restore a previous export into an empty database. Returns row count."""
        data = json.loads(text)
        if data.get("format") != EXPORT_FORMAT:
            raise ValueError(f"unsupported export format: {data.get('format')!r}")

        baby = data.get("baby")
        if baby is not None:
            from datetime import date  # noqa: PLC0415

            self._baby_repo.upsert(
                Baby(
                    baby_id=baby["baby_id"],
                    name=baby["name"],
                    sex=Sex(baby["sex"]),
                    dob=date.fromisoformat(baby["dob"]),
                    due_date=date.fromisoformat(baby["due_date"]),
                    birth_weight_g=baby["birth_weight_g"],
                )
            )

        restored = 0
        for domain, rows in data.get("events", {}).items():
            restored += self._repo.restore(domain, rows)
        restored += self._alert_log.restore(data.get("alert_log", []))
        return restored


def _csv_header(domain: str) -> list[str]:
    """Stable header even when a domain has no rows yet."""
    return [
        "id",
        "baby_id",
        "ts",
        "logged_by",
        *sorted(EDITABLE[domain] - {"ts"}),
        "created_at",
        "edited_at",
        "deleted_at",
    ]
