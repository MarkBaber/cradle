"""Production totals, stock on hand, and batch lifecycle over M1 (task V2).

Reports ages, never verdicts: whether an age is too long is a threshold
question owned by A11, which runs the pure rules engine over these facts
(D6). This service only measures.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from cradle.models import (
    LIVE_BATCH_STATES,
    BatchState,
    BottleColour,
    BreastSide,
    FeedEvent,
    FeedMethod,
    MilkBatch,
    MilkStore,
)
from cradle.ports.clock import Clock
from cradle.repos.events_repo import EventsRepo

BABY_ID = 1  # single-baby v1 (D11), matches LoggingService


class InvalidBatchTransitionError(ValueError):
    """A lifecycle transition is not legal for a batch's current state/store."""


class UnknownBatchError(ValueError):
    """A batch id does not refer to any known batch."""


# Legal state moves. THAWED->OPENED and STORED->OPENED additionally require
# the batch to already be in the fridge (see _require_thawed_or_fridge).
_ALLOWED_TRANSITIONS: dict[BatchState, frozenset[BatchState]] = {
    BatchState.STORED: frozenset({BatchState.THAWED, BatchState.OPENED, BatchState.DISCARDED}),
    BatchState.THAWED: frozenset({BatchState.OPENED, BatchState.DISCARDED}),
    BatchState.OPENED: frozenset({BatchState.USED, BatchState.DISCARDED}),
    BatchState.USED: frozenset(),
    BatchState.DISCARDED: frozenset(),
}


def _effective_clock(batch: MilkBatch) -> datetime:
    """The timestamp a batch's age is measured from: its latest lifecycle event."""
    return batch.opened_at or batch.thawed_at or batch.stored_at


@dataclass(frozen=True, slots=True)
class ProductionWindow:
    total_ml: int
    by_side: dict[BreastSide, int]


@dataclass(frozen=True, slots=True)
class ProductionSummary:
    trailing_24h: ProductionWindow
    trailing_7d: ProductionWindow


@dataclass(frozen=True, slots=True)
class BatchAge:
    batch: MilkBatch
    age: timedelta


@dataclass(frozen=True, slots=True)
class StoreStock:
    store: MilkStore
    total_ml: int
    batches: tuple[BatchAge, ...]  # oldest first


class MilkStockService:
    def __init__(self, repo: EventsRepo, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    def _at(self, ts: datetime | None) -> datetime:
        return ts if ts is not None else self._clock.now()

    def _get_batch(self, batch_id: int) -> MilkBatch:
        for batch in self._repo.list_milk_batches(states=None):
            if batch.batch_id == batch_id:
                return batch
        raise UnknownBatchError(f"no batch with id {batch_id}")

    def _transition(self, batch: MilkBatch, to: BatchState, at: datetime) -> None:
        if to not in _ALLOWED_TRANSITIONS[batch.state]:
            raise InvalidBatchTransitionError(
                f"cannot move batch {batch.batch_id} from {batch.state.value} to {to.value}"
            )
        assert batch.batch_id is not None  # always set once read back from the repo
        self._repo.set_batch_state(batch.batch_id, to, at)

    # ------------------------------------------------------------ production
    def production_summary(self) -> ProductionSummary:
        now = self._clock.now()
        return ProductionSummary(
            trailing_24h=self._production_window(now - timedelta(hours=24)),
            trailing_7d=self._production_window(now - timedelta(days=7)),
        )

    def _production_window(self, since: datetime) -> ProductionWindow:
        events = self._repo.list_expressions(limit=10_000, since=since)
        by_side: dict[BreastSide, int] = {side: 0 for side in BreastSide}
        for ev in events:
            by_side[ev.side] += ev.volume_ml or 0
        return ProductionWindow(total_ml=sum(by_side.values()), by_side=by_side)

    # ----------------------------------------------------------------- stock
    def stock_on_hand(self) -> dict[MilkStore, StoreStock]:
        now = self._clock.now()
        by_store: dict[MilkStore, list[MilkBatch]] = defaultdict(list)
        for batch in self._repo.list_milk_batches(states=LIVE_BATCH_STATES):
            by_store[batch.store].append(batch)
        result: dict[MilkStore, StoreStock] = {}
        for store, batches in by_store.items():
            ordered = sorted(batches, key=lambda b: (_effective_clock(b), b.batch_id or 0))
            result[store] = StoreStock(
                store=store,
                total_ml=sum(b.volume_ml for b in ordered),
                batches=tuple(
                    BatchAge(batch=b, age=now - _effective_clock(b)) for b in ordered
                ),
            )
        return result

    def fifo_next(self, store: MilkStore) -> MilkBatch | None:
        batches = self._repo.list_milk_batches(states=LIVE_BATCH_STATES, store=store)
        if not batches:
            return None
        return min(batches, key=lambda b: (_effective_clock(b), b.batch_id or 0))

    # ------------------------------------------------------------- lifecycle
    def store_expression(
        self,
        store: MilkStore,
        colour: BottleColour,
        volume_ml: int,
        expressed_at: datetime,
        stored_at: datetime | None = None,
        expression_id: int | None = None,
        logged_by: str = "",
    ) -> int:
        if store not in (MilkStore.FRIDGE, MilkStore.FREEZER):
            raise InvalidBatchTransitionError(
                f"cannot store a fresh expression into {store.value}"
            )
        return self._repo.insert_milk_batch(
            MilkBatch(
                batch_id=None,
                baby_id=BABY_ID,
                expressed_at=expressed_at,
                stored_at=stored_at if stored_at is not None else self._clock.now(),
                store=store,
                colour=colour,
                volume_ml=volume_ml,
                state=BatchState.STORED,
                logged_by=logged_by,
                expression_id=expression_id,
            )
        )

    def thaw(self, batch_id: int, at: datetime | None = None) -> None:
        batch = self._get_batch(batch_id)
        if batch.store != MilkStore.FREEZER:
            raise InvalidBatchTransitionError(f"batch {batch_id} is not in the freezer")
        self._transition(batch, BatchState.THAWED, self._at(at))
        self._repo.edit_event("milk_batch", batch_id, {"store": MilkStore.FRIDGE.value})

    def open_batch(self, batch_id: int, at: datetime | None = None) -> None:
        batch = self._get_batch(batch_id)
        if batch.store == MilkStore.FREEZER:
            raise InvalidBatchTransitionError(f"batch {batch_id} must be thawed before opening")
        self._transition(batch, BatchState.OPENED, self._at(at))

    def use(self, batch_id: int, at: datetime | None = None) -> None:
        batch = self._get_batch(batch_id)
        self._transition(batch, BatchState.USED, self._at(at))

    def discard(self, batch_id: int, at: datetime | None = None) -> None:
        batch = self._get_batch(batch_id)
        self._transition(batch, BatchState.DISCARDED, self._at(at))

    # --------------------------------------------------------------- feeding
    def feed_from_batch(
        self,
        batch_id: int,
        volume_ml: int,
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        """Link a BOTTLE_EXPRESSED feed to a batch: decrement its remaining
        volume and open it on first draw. Partial use leaves the remainder
        live; opened_at is stamped once, not on every subsequent feed."""
        batch = self._get_batch(batch_id)
        if batch.store == MilkStore.FREEZER:
            raise InvalidBatchTransitionError(f"batch {batch_id} must be thawed before feeding")
        at = self._at(ts)
        if batch.state in (BatchState.STORED, BatchState.THAWED):
            self._transition(batch, BatchState.OPENED, at)
        elif batch.state != BatchState.OPENED:
            raise InvalidBatchTransitionError(
                f"cannot feed from batch {batch_id} in state {batch.state.value}"
            )
        remaining = batch.volume_ml - volume_ml
        if remaining < 0:
            raise InvalidBatchTransitionError(
                f"batch {batch_id} only has {batch.volume_ml}ml remaining"
            )
        self._repo.edit_event("milk_batch", batch_id, {"volume_ml": remaining})
        return self._repo.insert_feed(
            FeedEvent(
                event_id=None,
                baby_id=BABY_ID,
                ts=at,
                logged_by=logged_by,
                method=FeedMethod.BOTTLE_EXPRESSED,
                volume_ml=volume_ml,
                note=f"batch {batch_id}",
            )
        )
