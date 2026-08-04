"""Baby profile + notification settings (task U7)."""

from datetime import date

from cradle.models import Baby, Sex
from cradle.ports.notifier import Notifier
from cradle.repos.baby_repo import BabyRepo

BABY_ID = 1


class SettingsService:
    def __init__(self, baby_repo: BabyRepo, notifier: Notifier) -> None:
        self._baby_repo = baby_repo
        self._notifier = notifier

    def profile(self) -> Baby | None:
        return self._baby_repo.get()

    def has_profile(self) -> bool:
        return self._baby_repo.get() is not None

    def save_profile(
        self,
        name: str,
        sex: str,
        dob: str,
        due_date: str,
        birth_weight_g: int,
    ) -> None:
        self._baby_repo.upsert(
            Baby(
                baby_id=BABY_ID,
                name=name.strip(),
                sex=Sex(sex),
                dob=date.fromisoformat(dob),
                due_date=date.fromisoformat(due_date),
                birth_weight_g=birth_weight_g,
            )
        )

    def test_notification(self) -> None:
        """Send a harmless test finding so the household can verify ntfy setup."""
        from datetime import UTC, datetime

        from cradle.models import AlertSeverity, Finding

        self._notifier.send(
            Finding(
                rule_id="TEST",
                severity=AlertSeverity.INFO,
                message="CRADLE notifications are working.",
                fingerprint="TEST:setup",
                ts=datetime.now(UTC),
            )
        )
