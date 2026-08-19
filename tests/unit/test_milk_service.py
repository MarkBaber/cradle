"""V2: MilkStockService - production totals, stock on hand, batch lifecycle."""

from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import (
    BatchState,
    BottleColour,
    BreastSide,
    ExpressionEvent,
    FeedMethod,
    MilkBatch,
    MilkStore,
)
from cradle.repos.events_repo import EventsRepo
from cradle.services.milk_service import (
    InvalidBatchTransitionError,
    MilkStockService,
    UnknownBatchError,
)

BASE_EXPR = {"event_id": None, "baby_id": 1, "logged_by": "phone"}


def _build() -> tuple[EventsRepo, MilkStockService]:
    db = make_db()
    repo = make_repo(db)
    return repo, MilkStockService(repo, clock())


def _batch(
    colour: BottleColour,
    state: BatchState = BatchState.STORED,
    store: MilkStore = MilkStore.FRIDGE,
    volume_ml: int = 60,
    expressed_at=NOW - timedelta(hours=1),
    stored_at=NOW,
    thawed_at=None,
    opened_at=None,
    used_at=None,
) -> MilkBatch:
    return MilkBatch(
        batch_id=None,
        baby_id=1,
        expressed_at=expressed_at,
        stored_at=stored_at,
        store=store,
        colour=colour,
        volume_ml=volume_ml,
        state=state,
        logged_by="phone",
        thawed_at=thawed_at,
        opened_at=opened_at,
        used_at=used_at,
    )


def _seed(repo: EventsRepo, **kw: object) -> int:
    return repo.insert_milk_batch(_batch(**kw))  # type: ignore[arg-type]


def _get(repo: EventsRepo, batch_id: int) -> MilkBatch:
    for b in repo.list_milk_batches():
        if b.batch_id == batch_id:
            return b
    raise AssertionError(f"batch {batch_id} not found")


# --------------------------------------------------------------- criterion 1
def test_production_summary_windows_sum_and_split_by_side() -> None:
    repo, svc = _build()
    repo.insert_expression(
        ExpressionEvent(
            ts=NOW - timedelta(hours=1), side=BreastSide.LEFT, volume_ml=30, **BASE_EXPR
        )
    )
    repo.insert_expression(
        ExpressionEvent(
            ts=NOW - timedelta(hours=20), side=BreastSide.RIGHT, volume_ml=20, **BASE_EXPR
        )
    )
    repo.insert_expression(
        ExpressionEvent(
            ts=NOW - timedelta(hours=22), side=BreastSide.RIGHT, volume_ml=None, **BASE_EXPR
        )
    )
    repo.insert_expression(
        ExpressionEvent(ts=NOW - timedelta(days=3), side=BreastSide.BOTH, volume_ml=50, **BASE_EXPR)
    )
    repo.insert_expression(
        ExpressionEvent(
            ts=NOW - timedelta(days=8), side=BreastSide.LEFT, volume_ml=999, **BASE_EXPR
        )
    )

    summary = svc.production_summary()

    assert summary.trailing_24h.total_ml == 50, (
        "30 + 20 + 0(None); the 8-day-old event is out of range"
    )
    assert summary.trailing_24h.by_side == {
        BreastSide.LEFT: 30,
        BreastSide.RIGHT: 20,
        BreastSide.BOTH: 0,
    }

    assert summary.trailing_7d.total_ml == 100, (
        "adds the 3-day-old BOTH session; still excludes the 8-day one"
    )
    assert summary.trailing_7d.by_side == {
        BreastSide.LEFT: 30,
        BreastSide.RIGHT: 20,
        BreastSide.BOTH: 50,
    }


def test_production_summary_includes_all_sides_with_zero_when_no_events() -> None:
    _, svc = _build()

    summary = svc.production_summary()

    assert summary.trailing_24h.total_ml == 0
    assert summary.trailing_24h.by_side == {
        BreastSide.LEFT: 0,
        BreastSide.RIGHT: 0,
        BreastSide.BOTH: 0,
    }
    assert summary.trailing_7d.total_ml == 0
    assert summary.trailing_7d.by_side == {
        BreastSide.LEFT: 0,
        BreastSide.RIGHT: 0,
        BreastSide.BOTH: 0,
    }


def test_production_summary_boundary_exactly_24h_and_7d_ago_are_included() -> None:
    repo, svc = _build()
    repo.insert_expression(
        ExpressionEvent(
            ts=NOW - timedelta(hours=24), side=BreastSide.LEFT, volume_ml=15, **BASE_EXPR
        )
    )
    repo.insert_expression(
        ExpressionEvent(
            ts=NOW - timedelta(days=7), side=BreastSide.RIGHT, volume_ml=25, **BASE_EXPR
        )
    )

    summary = svc.production_summary()

    assert summary.trailing_24h.total_ml == 15, "ts >= now-24h is inclusive of the boundary"
    assert summary.trailing_7d.total_ml == 40, "both events fall inside the >=now-7d window"


# --------------------------------------------------------------- criterion 2
def test_stock_on_hand_counts_only_live_batches_per_store() -> None:
    repo, svc = _build()
    repo.insert_milk_batch(
        _batch(BottleColour.BLUE, BatchState.STORED, MilkStore.FRIDGE, volume_ml=60)
    )
    repo.insert_milk_batch(
        _batch(
            BottleColour.GREEN,
            BatchState.THAWED,
            MilkStore.FRIDGE,
            volume_ml=50,
            thawed_at=NOW - timedelta(hours=3),
        )
    )
    repo.insert_milk_batch(
        _batch(
            BottleColour.RED,
            BatchState.OPENED,
            MilkStore.FRIDGE,
            volume_ml=40,
            thawed_at=NOW - timedelta(hours=2),
            opened_at=NOW - timedelta(hours=1),
        )
    )
    repo.insert_milk_batch(
        _batch(BottleColour.YELLOW, BatchState.USED, MilkStore.FRIDGE, volume_ml=999)
    )
    repo.insert_milk_batch(
        _batch(BottleColour.ORANGE, BatchState.DISCARDED, MilkStore.FRIDGE, volume_ml=999)
    )
    repo.insert_milk_batch(
        _batch(BottleColour.PURPLE, BatchState.STORED, MilkStore.FREEZER, volume_ml=30)
    )
    repo.insert_milk_batch(
        _batch(BottleColour.PINK, BatchState.USED, MilkStore.FREEZER, volume_ml=999)
    )
    deleted_id = repo.insert_milk_batch(
        _batch(BottleColour.WHITE, BatchState.STORED, MilkStore.FRIDGE, volume_ml=999)
    )
    repo.soft_delete("milk_batch", deleted_id)

    stock = svc.stock_on_hand()

    assert set(stock) == {MilkStore.FRIDGE, MilkStore.FREEZER}

    fridge_colours = {ba.batch.colour for ba in stock[MilkStore.FRIDGE].batches}
    assert fridge_colours == {BottleColour.BLUE, BottleColour.GREEN, BottleColour.RED}
    assert stock[MilkStore.FRIDGE].total_ml == 150, (
        "used, discarded and soft-deleted batches never count"
    )

    freezer_colours = {ba.batch.colour for ba in stock[MilkStore.FREEZER].batches}
    assert freezer_colours == {BottleColour.PURPLE}
    assert stock[MilkStore.FREEZER].total_ml == 30


def test_stock_on_hand_excludes_store_with_only_dead_batches() -> None:
    repo, svc = _build()
    repo.insert_milk_batch(
        _batch(BottleColour.WHITE, BatchState.DISCARDED, MilkStore.ROOM, volume_ml=999)
    )

    stock = svc.stock_on_hand()

    assert MilkStore.ROOM not in stock


# --------------------------------------------------------------- criterion 3
def test_fifo_next_returns_oldest_by_effective_clock_not_raw_stored_at() -> None:
    repo, svc = _build()
    oldest_id = repo.insert_milk_batch(
        _batch(
            BottleColour.BLUE,
            BatchState.STORED,
            MilkStore.FRIDGE,
            stored_at=NOW - timedelta(days=3),
        )
    )
    repo.insert_milk_batch(
        _batch(
            BottleColour.GREEN,
            BatchState.STORED,
            MilkStore.FRIDGE,
            stored_at=NOW - timedelta(days=1),
        )
    )
    # Stored longest ago, but thawed recently: the effective clock is thawed_at,
    # so this one must NOT win FIFO despite the oldest raw stored_at.
    repo.insert_milk_batch(
        _batch(
            BottleColour.RED,
            BatchState.THAWED,
            MilkStore.FRIDGE,
            stored_at=NOW - timedelta(days=5),
            thawed_at=NOW - timedelta(hours=12),
        )
    )

    picked = svc.fifo_next(MilkStore.FRIDGE)

    assert picked is not None
    assert picked.batch_id == oldest_id


def test_fifo_next_returns_none_for_store_with_no_batches() -> None:
    _, svc = _build()

    assert svc.fifo_next(MilkStore.ROOM) is None


def test_fifo_next_returns_none_when_only_dead_batches_present() -> None:
    repo, svc = _build()
    repo.insert_milk_batch(_batch(BottleColour.BLUE, BatchState.USED, MilkStore.FREEZER))
    repo.insert_milk_batch(_batch(BottleColour.GREEN, BatchState.DISCARDED, MilkStore.FREEZER))

    assert svc.fifo_next(MilkStore.FREEZER) is None


# --------------------------------------------------------------- criterion 4
def test_thaw_raises_when_batch_already_in_fridge_stored() -> None:
    repo, svc = _build()
    bid = _seed(repo, colour=BottleColour.BLUE, state=BatchState.STORED, store=MilkStore.FRIDGE)
    try:
        svc.thaw(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("thawing a fridge-stored batch should raise")


def test_thaw_raises_when_batch_already_thawed() -> None:
    repo, svc = _build()
    bid = _seed(
        repo,
        colour=BottleColour.GREEN,
        state=BatchState.THAWED,
        store=MilkStore.FRIDGE,
        thawed_at=NOW - timedelta(hours=1),
    )
    try:
        svc.thaw(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("re-thawing an already-thawed batch should raise")


def test_open_batch_raises_when_still_frozen() -> None:
    repo, svc = _build()
    bid = _seed(repo, colour=BottleColour.RED, state=BatchState.STORED, store=MilkStore.FREEZER)
    try:
        svc.open_batch(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("opening a still-frozen batch should raise")


def test_open_batch_raises_when_already_opened() -> None:
    repo, svc = _build()
    bid = _seed(
        repo,
        colour=BottleColour.YELLOW,
        state=BatchState.OPENED,
        store=MilkStore.FRIDGE,
        opened_at=NOW - timedelta(hours=1),
    )
    try:
        svc.open_batch(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("re-opening an already-opened batch should raise")


def test_open_batch_raises_when_used() -> None:
    repo, svc = _build()
    bid = _seed(repo, colour=BottleColour.ORANGE, state=BatchState.USED, store=MilkStore.FRIDGE)
    try:
        svc.open_batch(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("opening a used batch should raise")


def test_open_batch_raises_when_discarded() -> None:
    repo, svc = _build()
    bid = _seed(
        repo, colour=BottleColour.PURPLE, state=BatchState.DISCARDED, store=MilkStore.FRIDGE
    )
    try:
        svc.open_batch(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("opening a discarded batch should raise")


def test_use_raises_when_batch_not_yet_opened_stored() -> None:
    repo, svc = _build()
    bid = _seed(repo, colour=BottleColour.PINK, state=BatchState.STORED, store=MilkStore.FRIDGE)
    try:
        svc.use(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("using a stored, not-yet-opened batch should raise")


def test_use_raises_when_batch_not_yet_opened_thawed() -> None:
    repo, svc = _build()
    bid = _seed(
        repo,
        colour=BottleColour.WHITE,
        state=BatchState.THAWED,
        store=MilkStore.FRIDGE,
        thawed_at=NOW - timedelta(hours=1),
    )
    try:
        svc.use(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("using a thawed, not-yet-opened batch should raise")


def test_use_raises_when_already_used() -> None:
    repo, svc = _build()
    bid = _seed(repo, colour=BottleColour.BLUE, state=BatchState.USED, store=MilkStore.FRIDGE)
    try:
        svc.use(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("using an already-used batch should raise")


def test_discard_raises_when_already_discarded() -> None:
    repo, svc = _build()
    bid = _seed(repo, colour=BottleColour.GREEN, state=BatchState.DISCARDED, store=MilkStore.FRIDGE)
    try:
        svc.discard(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("discarding an already-discarded batch should raise")


def test_discard_raises_when_used() -> None:
    repo, svc = _build()
    bid = _seed(repo, colour=BottleColour.RED, state=BatchState.USED, store=MilkStore.FRIDGE)
    try:
        svc.discard(bid)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("discarding a used batch should raise")


def test_feed_from_batch_raises_when_discarded() -> None:
    """Explicitly named in the V2 exit criteria: using a discarded batch."""
    repo, svc = _build()
    bid = _seed(
        repo,
        colour=BottleColour.YELLOW,
        state=BatchState.DISCARDED,
        store=MilkStore.FRIDGE,
        volume_ml=60,
    )
    try:
        svc.feed_from_batch(bid, 10)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("feeding from a discarded batch should raise")


def test_feed_from_batch_raises_when_used() -> None:
    repo, svc = _build()
    bid = _seed(
        repo,
        colour=BottleColour.ORANGE,
        state=BatchState.USED,
        store=MilkStore.FRIDGE,
        volume_ml=60,
    )
    try:
        svc.feed_from_batch(bid, 10)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("feeding from a used batch should raise")


def test_feed_from_batch_raises_when_still_frozen() -> None:
    repo, svc = _build()
    bid = _seed(
        repo,
        colour=BottleColour.PURPLE,
        state=BatchState.STORED,
        store=MilkStore.FREEZER,
        volume_ml=60,
    )
    try:
        svc.feed_from_batch(bid, 10)
    except InvalidBatchTransitionError:
        return
    raise AssertionError("feeding from a still-frozen batch should raise")


def test_store_expression_into_room_raises() -> None:
    repo, svc = _build()
    try:
        svc.store_expression(MilkStore.ROOM, BottleColour.PINK, 60, NOW)
    except InvalidBatchTransitionError:
        pass
    else:
        raise AssertionError("storing into MilkStore.ROOM should raise")
    assert repo.list_milk_batches() == [], "a rejected store must not persist a batch"


def test_unknown_batch_id_raises_unknown_batch_error() -> None:
    _, svc = _build()
    calls = (
        lambda: svc.thaw(999999),
        lambda: svc.open_batch(999999),
        lambda: svc.use(999999),
        lambda: svc.discard(999999),
        lambda: svc.feed_from_batch(999999, 10),
    )
    for call in calls:
        try:
            call()
        except UnknownBatchError:
            continue
        raise AssertionError("an unknown batch id should raise UnknownBatchError")


# --------------------------------------------------------------- criterion 5
def test_feed_from_batch_decrements_and_opens_with_matching_timestamp() -> None:
    repo, svc = _build()
    bid = svc.store_expression(
        MilkStore.FRIDGE,
        BottleColour.BLUE,
        100,
        expressed_at=NOW - timedelta(hours=2),
        stored_at=NOW - timedelta(hours=1),
    )
    feed_ts = NOW + timedelta(minutes=30)

    feed_id = svc.feed_from_batch(bid, 40, logged_by="phone", ts=feed_ts)

    batch = _get(repo, bid)
    assert batch.state is BatchState.OPENED
    assert batch.opened_at == feed_ts
    assert batch.volume_ml == 60

    matches = [f for f in repo.list_feeds() if f.event_id == feed_id]
    assert len(matches) == 1
    (feed,) = matches
    assert feed.method is FeedMethod.BOTTLE_EXPRESSED
    assert feed.volume_ml == 40


def test_feed_from_batch_without_explicit_ts_uses_clock_now() -> None:
    repo, svc = _build()
    bid = svc.store_expression(
        MilkStore.FRIDGE, BottleColour.GREEN, 80, expressed_at=NOW - timedelta(hours=1)
    )

    feed_id = svc.feed_from_batch(bid, 20)

    batch = _get(repo, bid)
    assert batch.opened_at == NOW
    assert batch.volume_ml == 60

    matches = [f for f in repo.list_feeds() if f.event_id == feed_id]
    (feed,) = matches
    assert feed.ts == NOW


# --------------------------------------------------------------- criterion 6
def test_partial_feed_leaves_remainder_live_and_opened() -> None:
    repo, svc = _build()
    bid = svc.store_expression(
        MilkStore.FRIDGE, BottleColour.RED, 100, expressed_at=NOW - timedelta(hours=1)
    )

    svc.feed_from_batch(bid, 30, ts=NOW)

    batch = _get(repo, bid)
    assert batch.state is BatchState.OPENED, "a partially-used batch stays live, not USED"
    assert batch.volume_ml == 70


def test_second_feed_on_opened_batch_does_not_restamp_opened_at() -> None:
    repo, svc = _build()
    bid = svc.store_expression(
        MilkStore.FRIDGE, BottleColour.YELLOW, 100, expressed_at=NOW - timedelta(hours=1)
    )
    first_ts = NOW
    second_ts = NOW + timedelta(hours=1)

    svc.feed_from_batch(bid, 30, ts=first_ts)
    svc.feed_from_batch(bid, 20, ts=second_ts)

    batch = _get(repo, bid)
    assert batch.state is BatchState.OPENED
    assert batch.opened_at == first_ts, "opened_at is stamped once, on the first feed only"
    assert batch.volume_ml == 50


def test_feed_exceeding_remaining_volume_raises_without_mutating_batch() -> None:
    repo, svc = _build()
    bid = svc.store_expression(
        MilkStore.FRIDGE, BottleColour.ORANGE, 100, expressed_at=NOW - timedelta(hours=1)
    )
    svc.feed_from_batch(bid, 60, ts=NOW)  # remaining volume now 40

    try:
        svc.feed_from_batch(bid, 41, ts=NOW + timedelta(minutes=10))
    except InvalidBatchTransitionError:
        pass
    else:
        raise AssertionError("feeding more than the remaining volume should raise")

    batch = _get(repo, bid)
    assert batch.volume_ml == 40, "a rejected over-request must not mutate the stored remainder"
