"""V1: service-level logging, sleep toggle branches, undo, adjust-time.

N5: WhatsApp echo. history/whatsapp/chat_log are optional constructor args
(default None), so every plain LoggingService(repo, clock()) call above keeps
exercising the no-echo path unchanged.
"""

from datetime import UTC, datetime, timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import (
    ActivityCategory,
    BottleColour,
    BreastSide,
    FeedMethod,
    GrowthMeasure,
    MilkStore,
    NappyKind,
    StoolColour,
    StoolConsistency,
    to_local,
)
from cradle.repos.chat_log_repo import ChatLogRepo
from cradle.services.history_service import HistoryService
from cradle.services.logging_service import LoggingService
from cradle.services.milk_service import MilkStockService


def _svc() -> LoggingService:
    return LoggingService(make_repo(make_db()), clock())


def test_clock_supplies_now_when_ts_omitted() -> None:
    svc = _svc()
    svc.log_feed(FeedMethod.BREAST_LEFT)
    assert svc.recent_feeds()[0].ts == NOW


def test_explicit_ts_wins_over_clock() -> None:
    svc = _svc()
    earlier = NOW - timedelta(hours=3)
    svc.log_feed(FeedMethod.BREAST_RIGHT, ts=earlier)
    assert svc.recent_feeds()[0].ts == earlier


def test_log_every_domain() -> None:
    svc = _svc()
    assert svc.log_feed(FeedMethod.BOTTLE_EXPRESSED, volume_ml=60) > 0
    assert svc.log_nappy(NappyKind.MIXED, StoolColour.GREEN) > 0
    assert svc.log_growth(GrowthMeasure.WEIGHT, 3550) > 0
    assert svc.log_temperature(36.9) > 0
    assert svc.log_milestone("motor", "Lifts head") > 0
    assert svc.log_note("slept through", ("win",)) > 0


def test_log_nappy_defaults_consistency_to_unset() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    svc.log_nappy(NappyKind.WET)
    assert repo.list_nappies()[0].consistency == StoolConsistency.UNSET


def test_log_nappy_persists_consistency() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    svc.log_nappy(NappyKind.DIRTY, StoolColour.GREEN, StoolConsistency.SEEDY)
    assert repo.list_nappies()[0].consistency == StoolConsistency.SEEDY


def test_toggle_sleep_starts_then_ends() -> None:
    svc = _svc()
    start_id = svc.toggle_sleep()
    running = svc.running_sleep()
    assert running is not None and running.event_id == start_id
    assert running.ts_end is None

    end_id = svc.toggle_sleep(ts=NOW + timedelta(minutes=50))
    assert end_id == start_id, "toggle must close the running sleep, not open a new one"
    assert svc.running_sleep() is None


def test_undo_soft_deletes() -> None:
    svc = _svc()
    fid = svc.log_feed(FeedMethod.BREAST_LEFT)
    svc.undo("feed", fid)
    assert svc.recent_feeds() == []


def test_adjust_time_persists() -> None:
    svc = _svc()
    fid = svc.log_feed(FeedMethod.BREAST_LEFT)
    corrected = NOW - timedelta(minutes=35)
    svc.adjust_time("feed", fid, corrected)
    assert svc.recent_feeds()[0].ts == corrected


# ------------------------------------------------------------ activity (V4)
def test_log_activity_stamps_from_clock_when_ts_omitted() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    from cradle.models.enums import ActivityCategory

    svc.log_activity(ActivityCategory.TUMMY_TIME, duration_min=3)
    assert repo.list_activities()[0].ts == NOW


def test_log_activity_explicit_ts_wins() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    from cradle.models.enums import ActivityCategory

    earlier = NOW - timedelta(hours=2)
    svc.log_activity(ActivityCategory.READING_TALKING, ts=earlier)
    assert repo.list_activities()[0].ts == earlier


def test_log_activity_persists_all_fields() -> None:
    repo = make_repo(make_db())
    svc = LoggingService(repo, clock())
    from cradle.models.enums import ActivityCategory

    event_id = svc.log_activity(
        category=ActivityCategory.SENSORY_PLAY,
        duration_min=7,
        note="enjoyed it",
    )
    assert event_id > 0
    ev = repo.list_activities()[0]
    assert ev.category == ActivityCategory.SENSORY_PLAY
    assert ev.duration_min == 7
    assert ev.note == "enjoyed it"


# ------------------------------------------------------------ WhatsApp echo (N5)
class WhatsAppRecorder:
    """Test double for ports.whatsapp.WhatsAppNotifier."""

    def __init__(self, configured: bool = True, ok: bool = True) -> None:
        self.configured = configured
        self._ok = ok
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return self._ok


class RaisingWhatsApp:
    """A whatsapp double whose send() misbehaves by raising, not just failing."""

    configured = True

    def send(self, text: str) -> bool:
        raise ConnectionError("boom")


def _svc_with_whatsapp(
    configured: bool = True, ok: bool = True
) -> tuple[LoggingService, WhatsAppRecorder, ChatLogRepo, HistoryService]:
    db = make_db()
    repo = make_repo(db)
    history = HistoryService(repo)
    chat_log = ChatLogRepo(db)
    whatsapp = WhatsAppRecorder(configured=configured, ok=ok)
    return LoggingService(repo, clock(), history, whatsapp, chat_log), whatsapp, chat_log, history


def _first_message(history: HistoryService, table: str, event_id: int, ts: datetime) -> str:
    """The expected WhatsApp line for the first message of a fresh chat_log's day."""
    row = history.get_row(table, event_id)
    assert row is not None
    local_dt = to_local(ts)
    return f"{local_dt:%d/%m/%y}\n{local_dt:%H:%M} {row.detail}"


def test_log_feed_echoes_history_detail() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    fid = svc.log_feed(FeedMethod.BOTTLE_EXPRESSED, volume_ml=30, ts=NOW)
    assert wa.sent == [_first_message(history, "feed", fid, NOW)]


def test_log_nappy_echoes_history_detail() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    nid = svc.log_nappy(NappyKind.MIXED, StoolColour.GREEN, ts=NOW)
    assert wa.sent == [_first_message(history, "nappy", nid, NOW)]


def test_toggle_sleep_echoes_start_and_end() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    start_id = svc.toggle_sleep(ts=NOW)
    end_ts = NOW + timedelta(minutes=45)
    end_id = svc.toggle_sleep(ts=end_ts)
    assert end_id == start_id
    assert len(wa.sent) == 2
    row = history.get_row("sleep", start_id)
    assert row is not None
    assert "slept 45 min" in row.detail
    assert wa.sent[1].endswith(f"{to_local(end_ts):%H:%M} {row.detail}")


def test_log_growth_echoes_history_detail() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    gid = svc.log_growth(GrowthMeasure.WEIGHT, 3550, ts=NOW)
    assert wa.sent == [_first_message(history, "growth", gid, NOW)]


def test_log_temperature_echoes_history_detail() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    tid = svc.log_temperature(36.9, ts=NOW)
    assert wa.sent == [_first_message(history, "temperature", tid, NOW)]


def test_log_milestone_echoes_history_detail() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    mid = svc.log_milestone("motor", "Lifts head", ts=NOW)
    assert wa.sent == [_first_message(history, "milestone", mid, NOW)]


def test_log_note_echoes_history_detail() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    nid = svc.log_note("slept through", ts=NOW)
    assert wa.sent == [_first_message(history, "note", nid, NOW)]


def test_log_expression_echoes_history_detail() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    xid = svc.log_expression(BreastSide.BOTH, ts=NOW)
    assert wa.sent == [_first_message(history, "expression", xid, NOW)]


def test_log_activity_echoes_history_detail() -> None:
    svc, wa, _, history = _svc_with_whatsapp()
    aid = svc.log_activity(ActivityCategory.TUMMY_TIME, duration_min=3, ts=NOW)
    assert wa.sent == [_first_message(history, "activity", aid, NOW)]


def test_milk_batch_echo_shares_the_same_mechanism() -> None:
    """MilkStockService does not call echo_event itself: milk_batch's insert
    site is milk_service.py, not logging_service.py, and this task's touches
    list has no room for milk_service.py (task N5 notes flag this gap). This
    proves the shared, domain-agnostic mechanism LoggingService exposes
    already produces the correct message for a milk_batch row, so wiring
    MilkStockService up to call echo_event() is the only remaining step.
    """
    db = make_db()
    repo = make_repo(db)
    history = HistoryService(repo)
    chat_log = ChatLogRepo(db)
    wa = WhatsAppRecorder()
    svc = LoggingService(repo, clock(), history, wa, chat_log)
    milk = MilkStockService(repo, clock())

    batch_id = milk.store_now(MilkStore.FRIDGE, BottleColour.WHITE, 100)
    svc.echo_event("milk_batch", batch_id, NOW)

    assert wa.sent == [_first_message(history, "milk_batch", batch_id, NOW)]


def test_date_header_precedes_first_message_of_a_new_local_day() -> None:
    svc, wa, _, _ = _svc_with_whatsapp()
    ts1 = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)  # local (BST) 2026-07-15 23:00
    ts2 = datetime(2026, 7, 15, 23, 30, tzinfo=UTC)  # local (BST) 2026-07-16 00:30
    ts3 = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)  # local (BST) 2026-07-16 09:00

    svc.log_feed(FeedMethod.BREAST_LEFT, ts=ts1)
    svc.log_feed(FeedMethod.BREAST_LEFT, ts=ts2)
    svc.log_feed(FeedMethod.BREAST_LEFT, ts=ts3)

    assert wa.sent[0].startswith("15/07/26\n23:00 ")
    assert wa.sent[1].startswith("16/07/26\n00:30 ")
    assert wa.sent[2].startswith("09:00 ")
    assert "\n" not in wa.sent[2]


def test_backdated_entry_does_not_reissue_a_spurious_date_header() -> None:
    """A same-day backdated write (the "adjust time" flow, U2) must not make
    the *next* real-time message on that day think its header hasn't gone
    out yet, even though the backdated row is inserted later with an earlier
    local_date than the row before it (task N5: last_local_date() must track
    MAX(local_date) across all rows, not the most-recently-inserted one)."""
    svc, wa, _, _ = _svc_with_whatsapp()
    today = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)  # local (BST) 2026-07-15 10:00
    yesterday = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)  # local (BST) 2026-07-14 10:00
    later_today = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)  # local (BST) 2026-07-15 16:00

    svc.log_feed(FeedMethod.BREAST_LEFT, ts=today)  # real-time: files today's header
    svc.log_feed(FeedMethod.BREAST_LEFT, ts=yesterday)  # backdated edit, inserted after
    svc.log_feed(FeedMethod.BREAST_LEFT, ts=later_today)  # real-time again, same day as msg 1

    assert wa.sent[0].startswith("15/07/26\n")
    assert wa.sent[1].startswith("14/07/26\n")
    assert wa.sent[2].startswith("16:00 ")  # no header: today's already went out
    assert "\n" not in wa.sent[2]


def test_unconfigured_whatsapp_no_ops_silently() -> None:
    svc, wa, chat_log, _ = _svc_with_whatsapp(configured=False)
    fid = svc.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)  # must not raise
    assert fid > 0
    assert wa.sent == []
    assert chat_log.last_local_date() is None


def test_no_history_or_whatsapp_wired_is_a_no_op() -> None:
    """Every existing 2-arg LoggingService(repo, clock()) call above must keep
    working: history/whatsapp/chat_log default to None => echo is inert."""
    svc = LoggingService(make_repo(make_db()), clock())
    fid = svc.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)  # must not raise
    assert fid > 0


def test_whatsapp_send_failure_does_not_raise_and_does_not_block_the_write() -> None:
    svc, wa, chat_log, _ = _svc_with_whatsapp(ok=False)
    fid = svc.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)  # must not raise
    assert fid > 0
    assert svc.recent_feeds()[0].event_id == fid
    assert len(wa.sent) == 1  # the send was attempted
    assert chat_log.last_local_date() == to_local(NOW).date()  # attempt recorded, success=False


def test_whatsapp_exception_never_propagates_to_the_caller() -> None:
    db = make_db()
    repo = make_repo(db)
    history = HistoryService(repo)
    chat_log = ChatLogRepo(db)
    svc = LoggingService(repo, clock(), history, RaisingWhatsApp(), chat_log)
    fid = svc.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)  # must not raise
    assert fid > 0
    assert repo.list_feeds()[0].event_id == fid
