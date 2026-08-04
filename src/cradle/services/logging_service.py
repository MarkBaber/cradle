"""Event logging use-cases (task V1).

Every method accepts an optional explicit `ts` (the "adjust time" flow, U2);
when omitted the injected Clock supplies "now" so tests are deterministic.
"""

from datetime import datetime

from cradle.models import (
    FeedEvent,
    FeedMethod,
    GrowthEvent,
    GrowthMeasure,
    Milestone,
    NappyEvent,
    NappyKind,
    Note,
    SleepEvent,
    StoolColour,
    TemperatureEvent,
)
from cradle.ports.clock import Clock
from cradle.repos.events_repo import EventsRepo

BABY_ID = 1  # single-baby v1 (D11)


class LoggingService:
    def __init__(self, repo: EventsRepo, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    def _at(self, ts: datetime | None) -> datetime:
        return ts if ts is not None else self._clock.now()

    # ------------------------------------------------------------------ feed
    def log_feed(
        self,
        method: FeedMethod,
        logged_by: str = "",
        ts: datetime | None = None,
        duration_min: int | None = None,
        volume_ml: int | None = None,
        note: str = "",
    ) -> int:
        return self._repo.insert_feed(
            FeedEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=self._at(ts),
                logged_by=logged_by,
                method=method,
                duration_min=duration_min,
                volume_ml=volume_ml,
                note=note,
            )
        )

    def recent_feeds(self, limit: int = 50) -> list[FeedEvent]:
        return self._repo.list_feeds(limit)

    # ----------------------------------------------------------------- nappy
    def log_nappy(
        self,
        kind: NappyKind,
        stool_colour: StoolColour = StoolColour.UNSET,
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        return self._repo.insert_nappy(
            NappyEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=self._at(ts),
                logged_by=logged_by,
                kind=kind,
                stool_colour=stool_colour,
            )
        )

    # ----------------------------------------------------------------- sleep
    def toggle_sleep(
        self,
        logged_by: str = "",
        ts: datetime | None = None,
        location: str = "cot",
    ) -> int:
        """End the running sleep if there is one, else start a new one.

        Returns the affected sleep row id, so the caller can offer undo (U2).
        """
        at = self._at(ts)
        running = self._repo.running_sleep()
        if running is not None and running.event_id is not None:
            self._repo.end_sleep(running.event_id, at)
            return running.event_id
        return self._repo.insert_sleep_start(
            SleepEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                ts_end=None,
                location=location,
            )
        )

    def running_sleep(self) -> SleepEvent | None:
        return self._repo.running_sleep()

    # ---------------------------------------------------------------- growth
    def log_growth(
        self,
        measure: GrowthMeasure,
        value: int,
        source: str = "home",
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        return self._repo.insert_growth(
            GrowthEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=self._at(ts),
                logged_by=logged_by,
                measure=measure,
                value=value,
                source=source,
            )
        )

    # ----------------------------------------------------------- temperature
    def log_temperature(
        self,
        temp_c: float,
        site: str = "axilla",
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        return self._repo.insert_temperature(
            TemperatureEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=self._at(ts),
                logged_by=logged_by,
                temp_c=temp_c,
                site=site,
            )
        )

    # ------------------------------------------------------------- milestone
    def log_milestone(
        self,
        category: str,
        title: str,
        note: str = "",
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        return self._repo.insert_milestone(
            Milestone(
                event_id=None,
                baby_id=BABY_ID,
                ts=self._at(ts),
                logged_by=logged_by,
                category=category,
                title=title,
                note=note,
            )
        )

    # ------------------------------------------------------------------ note
    def log_note(
        self,
        text: str,
        tags: tuple[str, ...] = (),
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        return self._repo.insert_note(
            Note(
                event_id=None,
                baby_id=BABY_ID,
                ts=self._at(ts),
                logged_by=logged_by,
                text=text,
                tags=tags,
            )
        )

    # --------------------------------------------------------- undo / adjust
    def undo(self, table: str, event_id: int) -> None:
        """Undo == soft delete (D8). Raises UnknownTableError on a bad table."""
        self._repo.soft_delete(table, event_id)

    def adjust_time(self, table: str, event_id: int, ts: datetime) -> None:
        self._repo.edit_event(table, event_id, {"ts": ts})

    def edit(self, table: str, event_id: int, fields: dict[str, object]) -> None:
        self._repo.edit_event(table, event_id, fields)
