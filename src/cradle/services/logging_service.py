"""Event logging use-cases (task V1).

Every method accepts an optional explicit `ts` (the "adjust time" flow, U2);
when omitted the injected Clock supplies "now" so tests are deterministic.

Task N5: every write also echoes a brief line to WhatsApp via echo_event(),
called after the repo write succeeds so a failed WhatsApp send can never
prevent the event itself from being recorded. history/whatsapp/chat_log are
optional constructor args (default None => echo is a no-op) so every existing
caller that only ever passed (repo, clock) keeps working unchanged.
"""

import logging
from datetime import datetime

from cradle.models import (
    BreastSide,
    ExpressionEvent,
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
    StoolConsistency,
    TemperatureEvent,
    to_local,
)
from cradle.models.enums import ActivityCategory
from cradle.models.events import ActivityEvent
from cradle.ports.clock import Clock
from cradle.ports.whatsapp import WhatsAppNotifier
from cradle.repos.chat_log_repo import ChatLogRepo
from cradle.repos.events_repo import EventsRepo
from cradle.services.history_service import HistoryService

log = logging.getLogger(__name__)

BABY_ID = 1  # single-baby v1 (D11)


class LoggingService:
    def __init__(
        self,
        repo: EventsRepo,
        clock: Clock,
        history: HistoryService | None = None,
        whatsapp: WhatsAppNotifier | None = None,
        chat_log: ChatLogRepo | None = None,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._history = history
        self._whatsapp = whatsapp
        self._chat_log = chat_log

    def _at(self, ts: datetime | None) -> datetime:
        return ts if ts is not None else self._clock.now()

    def echo_event(self, table: str, event_id: int, ts: datetime) -> None:
        """Post one brief WhatsApp line for an already-written event (N5).

        The message text reuses HistoryService's per-domain `detail` string
        (the same one /history renders) via get_row(), rather than a second,
        independently-drifting description of "how do we describe a feed
        tersely". Never raises: history/chat_log lookups and the send itself
        are all inside the try, so a failure here can never surface to the
        log_* caller or cast doubt on the write that already happened.
        """
        if self._whatsapp is None or self._history is None or self._chat_log is None:
            return
        try:
            if not self._whatsapp.configured:
                return
            row = self._history.get_row(table, event_id)
            if row is None:
                return
            local_dt = to_local(ts)
            day = local_dt.date()
            lines = []
            if self._chat_log.last_local_date() != day:
                lines.append(day.strftime("%d/%m/%y"))
            lines.append(f"{local_dt:%H:%M} {row.detail}")
            text = "\n".join(lines)
            success = self._whatsapp.send(text)
            self._chat_log.record(table, event_id, ts, day, text, self._clock.now(), success)
        except Exception:
            log.exception("whatsapp echo failed for %s#%s", table, event_id)

    # ------------------------------------------------------------ expression
    def log_expression(
        self,
        side: BreastSide = BreastSide.BOTH,
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        """One tap (T1/U14): volume and duration are post-hoc edits (U10)."""
        at = self._at(ts)
        event_id = self._repo.insert_expression(
            ExpressionEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                side=side,
            )
        )
        self.echo_event("expression", event_id, at)
        return event_id

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
        at = self._at(ts)
        event_id = self._repo.insert_feed(
            FeedEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                method=method,
                duration_min=duration_min,
                volume_ml=volume_ml,
                note=note,
            )
        )
        self.echo_event("feed", event_id, at)
        return event_id

    def recent_feeds(self, limit: int = 50) -> list[FeedEvent]:
        return self._repo.list_feeds(limit)

    # ----------------------------------------------------------------- nappy
    def log_nappy(
        self,
        kind: NappyKind,
        stool_colour: StoolColour = StoolColour.UNSET,
        consistency: StoolConsistency = StoolConsistency.UNSET,
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        at = self._at(ts)
        event_id = self._repo.insert_nappy(
            NappyEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                kind=kind,
                stool_colour=stool_colour,
                consistency=consistency,
            )
        )
        self.echo_event("nappy", event_id, at)
        return event_id

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
            self.echo_event("sleep", running.event_id, at)
            return running.event_id
        event_id = self._repo.insert_sleep_start(
            SleepEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                ts_end=None,
                location=location,
            )
        )
        self.echo_event("sleep", event_id, at)
        return event_id

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
        at = self._at(ts)
        event_id = self._repo.insert_growth(
            GrowthEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                measure=measure,
                value=value,
                source=source,
            )
        )
        self.echo_event("growth", event_id, at)
        return event_id

    # ----------------------------------------------------------- temperature
    def log_temperature(
        self,
        temp_c: float,
        site: str = "axilla",
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        at = self._at(ts)
        event_id = self._repo.insert_temperature(
            TemperatureEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                temp_c=temp_c,
                site=site,
            )
        )
        self.echo_event("temperature", event_id, at)
        return event_id

    # ------------------------------------------------------------- milestone
    def log_milestone(
        self,
        category: str,
        title: str,
        note: str = "",
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        at = self._at(ts)
        event_id = self._repo.insert_milestone(
            Milestone(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                category=category,
                title=title,
                note=note,
            )
        )
        self.echo_event("milestone", event_id, at)
        return event_id

    # ------------------------------------------------------------------ note
    def log_note(
        self,
        text: str,
        tags: tuple[str, ...] = (),
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        at = self._at(ts)
        event_id = self._repo.insert_note(
            Note(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                text=text,
                tags=tags,
            )
        )
        self.echo_event("note", event_id, at)
        return event_id

    # -------------------------------------------------------------- activity
    def log_activity(
        self,
        category: ActivityCategory,
        duration_min: int | None = None,
        note: str = "",
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        at = self._at(ts)
        event_id = self._repo.insert_activity(
            ActivityEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                category=category,
                duration_min=duration_min,
                note=note,
            )
        )
        self.echo_event("activity", event_id, at)
        return event_id

    # --------------------------------------------------------- undo / adjust
    def undo(self, table: str, event_id: int) -> None:
        """Undo == soft delete (D8). Raises UnknownTableError on a bad table."""
        self._repo.soft_delete(table, event_id)

    def adjust_time(self, table: str, event_id: int, ts: datetime) -> None:
        self._repo.edit_event(table, event_id, {"ts": ts})

    def edit(self, table: str, event_id: int, fields: dict[str, object]) -> None:
        self._repo.edit_event(table, event_id, fields)
