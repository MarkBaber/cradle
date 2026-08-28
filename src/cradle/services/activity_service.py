"""ActivityService: trailing-24h cumulative activity totals vs targets (task V4).

Report only, never grade: whether a day's total is 'enough' is a judgement
this service does not make.  It hands back the number and the target text side
by side, leaving any framing to U27.
"""

import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from cradle.models.enums import ActivityCategory
from cradle.ports.clock import Clock
from cradle.repos.events_repo import EventsRepo

WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ActivitySummary:
    """Per-category trailing-24h totals, paired with the configured target text.

    ``target_text`` is read verbatim from rules_config.toml's [activity_targets]
    table; it is never reworded, truncated or graded here.  An empty string
    means the key was absent from the config.
    """

    category: ActivityCategory
    duration_min: int  # cumulative minutes in the trailing 24h; 0 if none
    session_count: int  # number of sessions in the trailing 24h; 0 if none
    target_text: str  # verbatim from rules_config.toml, or "" if absent


class ActivityService:
    """Read-only view over the activity domain (task V4).

    The [activity_targets] table in rules_config.toml is read here but never
    written: a future settings task owns the writer if one is ever wanted.
    """

    def __init__(
        self,
        repo: EventsRepo,
        clock: Clock,
        config_path: Path,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._config_path = config_path

    def _config(self) -> dict[str, object]:
        if not self._config_path.exists():
            return {}
        with self._config_path.open("rb") as fh:
            return tomllib.load(fh)

    def _target_text(self, cfg: dict[str, object], category: ActivityCategory) -> str:
        table = cfg.get("activity_targets", {})
        if not isinstance(table, dict):
            return ""
        value = table.get(category.value, "")
        return str(value) if isinstance(value, str) else ""

    def summaries(self) -> list[ActivitySummary]:
        """Trailing-24h totals for every ActivityCategory, in enum definition order.

        Categories with no events in the window return zero minutes and zero
        sessions - never an error.
        """
        now = self._clock.now()
        since = now - WINDOW
        cfg = self._config()

        events = self._repo.list_activities(limit=1000, since=since)

        result: list[ActivitySummary] = []
        for category in ActivityCategory:
            matching = [e for e in events if e.category == category]
            total_min = sum(e.duration_min or 0 for e in matching)
            result.append(
                ActivitySummary(
                    category=category,
                    duration_min=total_min,
                    session_count=len(matching),
                    target_text=self._target_text(cfg, category),
                )
            )
        return result
