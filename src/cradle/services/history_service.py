"""Unified event history across domains (task U4)."""

from dataclasses import dataclass
from datetime import date, datetime

from cradle.models import to_local
from cradle.repos.events_repo import EventsRepo

DOMAINS = (
    "feed",
    "nappy",
    "sleep",
    "growth",
    "temperature",
    "milestone",
    "note",
    "expression",
    "milk_batch",
    "activity",
    "journal",
)


@dataclass(frozen=True, slots=True)
class HistoryRow:
    table: str
    event_id: int
    ts: datetime
    detail: str
    logged_by: str
    method: str | None = None
    kind: str | None = None
    volume_ml: int | None = None
    duration_min: int | None = None
    stool_colour: str | None = None
    consistency: str | None = None
    # Everything below carries the rest of EventsRepo.EDITABLE's per-table
    # allow-list, so the history page's Edit/Clone panel (task U43) can
    # pre-fill from a HistoryRow alone - no by-id repo lookup was added
    # (events_repo.py is outside this task's touches); these values are
    # already sitting on the domain objects rows() iterates below.
    note: str | None = None
    ts_end: datetime | None = None
    location: str | None = None
    measure: str | None = None
    value: int | None = None
    source: str | None = None
    temp_c: float | None = None
    site: str | None = None
    category: str | None = None
    title: str | None = None
    text: str | None = None
    tags: tuple[str, ...] | None = None
    # expression/milk_batch (task U46/M1): EDITABLE columns those two tables
    # have that don't map onto any field above.
    side: str | None = None
    store: str | None = None
    colour: str | None = None
    state: str | None = None

    @property
    def activity(self) -> str:
        return self.table.replace("_", " ").title()

    @property
    def activity_kind(self) -> str:
        if self.table == "feed" and self.method:
            return self.method.replace("_", " ").title()
        if self.table == "nappy" and self.kind:
            return self.kind.replace("_", " ").title()
        if self.table == "growth":
            parts = self.detail.split(" ", 1)
            return parts[0].title()
        if self.table == "milestone" and ":" in self.detail:
            return self.detail.split(":", 1)[0].strip().title()
        if self.table == "activity" and self.category:
            return self.category.replace("_", " ").title()
        if self.table == "expression" and self.side:
            return self.side.title()
        if self.table == "milk_batch" and self.state:
            return self.state.title()
        return ""

    @property
    def short_detail(self) -> str:
        if self.table == "feed":
            bits = []
            if self.duration_min:
                bits.append(f"{self.duration_min} min")
            if self.volume_ml:
                bits.append(f"{self.volume_ml} ml")
            return ", ".join(bits)
        if self.table == "nappy":
            bits = []
            if self.stool_colour and self.stool_colour != "unset":
                bits.append(self.stool_colour.replace("_", " "))
            if self.consistency and self.consistency != "unset":
                bits.append(self.consistency.replace("_", " "))
            return ", ".join(bits)
        if self.table == "growth":
            parts = self.detail.split(" ", 1)
            return parts[1] if len(parts) > 1 else ""
        if self.table == "milestone" and ":" in self.detail:
            return self.detail.split(":", 1)[1].strip()
        return self.detail


class HistoryService:
    def __init__(self, repo: EventsRepo) -> None:
        self._repo = repo

    def rows(
        self,
        domains: tuple[str, ...] = DOMAINS,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[HistoryRow]:
        out: list[HistoryRow] = []
        wanted = set(domains) & set(DOMAINS)

        def add(
            table: str,
            event_id: int | None,
            ts: datetime,
            detail: str,
            logged_by: str,
            method: str | None = None,
            kind: str | None = None,
            volume_ml: int | None = None,
            duration_min: int | None = None,
            stool_colour: str | None = None,
            consistency: str | None = None,
            note: str | None = None,
            ts_end: datetime | None = None,
            location: str | None = None,
            measure: str | None = None,
            value: int | None = None,
            source: str | None = None,
            temp_c: float | None = None,
            site: str | None = None,
            category: str | None = None,
            title: str | None = None,
            text: str | None = None,
            tags: tuple[str, ...] | None = None,
            side: str | None = None,
            store: str | None = None,
            colour: str | None = None,
            state: str | None = None,
        ) -> None:
            if event_id is not None:
                out.append(
                    HistoryRow(
                        table=table,
                        event_id=event_id,
                        ts=ts,
                        detail=detail,
                        logged_by=logged_by,
                        method=method,
                        kind=kind,
                        volume_ml=volume_ml,
                        duration_min=duration_min,
                        stool_colour=stool_colour,
                        consistency=consistency,
                        note=note,
                        ts_end=ts_end,
                        location=location,
                        measure=measure,
                        value=value,
                        source=source,
                        temp_c=temp_c,
                        site=site,
                        category=category,
                        title=title,
                        text=text,
                        tags=tags,
                        side=side,
                        store=store,
                        colour=colour,
                        state=state,
                    )
                )

        # growth/milestone/note/journal/milk_batch have no ts-indexed since/
        # until filter in events_repo.py (it's outside this task's touches),
        # so they only take a limit and are filtered in Python below. When a
        # window is requested, widen what's fetched so an older week's rows
        # aren't cut off by only ever seeing the newest `limit` rows overall.
        wide_limit = max(limit, 1000) if since is not None or until is not None else limit

        if "feed" in wanted:
            for e in self._repo.list_feeds(limit, since, until):
                bits = [e.method.value.replace("_", " ")]
                if e.duration_min:
                    bits.append(f"{e.duration_min} min")
                if e.volume_ml:
                    bits.append(f"{e.volume_ml} ml")
                add(
                    "feed",
                    e.event_id,
                    e.ts,
                    ", ".join(bits),
                    e.logged_by,
                    method=e.method.value,
                    volume_ml=e.volume_ml,
                    duration_min=e.duration_min,
                    note=e.note,
                )
        if "nappy" in wanted:
            for n in self._repo.list_nappies(limit, since, until):
                detail = n.kind.value
                if n.stool_colour.value != "unset":
                    detail += f" ({n.stool_colour.value.replace('_', ' ')})"
                add(
                    "nappy",
                    n.event_id,
                    n.ts,
                    detail,
                    n.logged_by,
                    kind=n.kind.value,
                    stool_colour=n.stool_colour.value,
                    consistency=n.consistency.value,
                )
        if "sleep" in wanted:
            for s in self._repo.list_sleeps(limit, since, until):
                if s.ts_end is None:
                    detail = f"asleep in {s.location} (running)"
                else:
                    mins = int((s.ts_end - s.ts).total_seconds() // 60)
                    detail = f"slept {mins} min in {s.location}"
                add(
                    "sleep",
                    s.event_id,
                    s.ts,
                    detail,
                    s.logged_by,
                    ts_end=s.ts_end,
                    location=s.location,
                )
        if "growth" in wanted:
            for g in self._repo.list_growth(limit=wide_limit):
                unit = "g" if g.measure.value == "weight" else "mm"
                add(
                    "growth",
                    g.event_id,
                    g.ts,
                    f"{g.measure.value} {g.value}{unit} ({g.source})",
                    g.logged_by,
                    measure=g.measure.value,
                    value=g.value,
                    source=g.source,
                )
        if "temperature" in wanted:
            for t in self._repo.list_temperatures(limit, since, until):
                add(
                    "temperature",
                    t.event_id,
                    t.ts,
                    f"{t.temp_c:.1f} C ({t.site})",
                    t.logged_by,
                    temp_c=t.temp_c,
                    site=t.site,
                )
        if "milestone" in wanted:
            for m in self._repo.list_milestones(wide_limit):
                add(
                    "milestone",
                    m.event_id,
                    m.ts,
                    f"{m.category}: {m.title}",
                    m.logged_by,
                    category=m.category,
                    title=m.title,
                    note=m.note,
                )
        if "note" in wanted:
            for nt in self._repo.list_notes(wide_limit):
                add(
                    "note",
                    nt.event_id,
                    nt.ts,
                    nt.text[:80],
                    nt.logged_by,
                    text=nt.text,
                    tags=nt.tags,
                )
        if "expression" in wanted:
            for x in self._repo.list_expressions(limit, since, until):
                bits = [x.side.value]
                if x.duration_min:
                    bits.append(f"{x.duration_min} min")
                if x.volume_ml:
                    bits.append(f"{x.volume_ml} ml")
                add(
                    "expression",
                    x.event_id,
                    x.ts,
                    ", ".join(bits),
                    x.logged_by,
                    side=x.side.value,
                    volume_ml=x.volume_ml,
                    duration_min=x.duration_min,
                    note=x.note,
                )
        if "milk_batch" in wanted:
            # No ts field (EDITABLE lists expressed_at/stored_at/thawed_at/
            # opened_at/used_at): anchored to stored_at, "when it entered
            # storage" - the day-group a parent would look for a bottle
            # under (task U46 notes).
            for b in self._repo.list_milk_batches(limit=wide_limit):
                add(
                    "milk_batch",
                    b.batch_id,
                    b.stored_at,
                    f"{b.colour.value} bottle, {b.volume_ml} ml ({b.store.value}, {b.state.value})",
                    b.logged_by,
                    volume_ml=b.volume_ml,
                    store=b.store.value,
                    colour=b.colour.value,
                    state=b.state.value,
                )
        if "activity" in wanted:
            for a in self._repo.list_activities(limit, since, until):
                bits = [a.category.value.replace("_", " ")]
                if a.duration_min:
                    bits.append(f"{a.duration_min} min")
                add(
                    "activity",
                    a.event_id,
                    a.ts,
                    ", ".join(bits),
                    a.logged_by,
                    category=a.category.value,
                    duration_min=a.duration_min,
                    note=a.note,
                )
        if "journal" in wanted:
            for j in self._repo.list_journal_entries(wide_limit):
                add(
                    "journal",
                    j.event_id,
                    j.ts,
                    j.title,
                    j.logged_by,
                    title=j.title,
                    text=j.story,
                    tags=j.temperament,
                )

        # Domain repos apply since/until only where indexed; enforce uniformly here.
        if since is not None:
            out = [r for r in out if r.ts >= since]
        if until is not None:
            out = [r for r in out if r.ts < until]

        out.sort(key=lambda r: r.ts, reverse=True)
        return out[:limit]

    def get_row(self, table: str, event_id: int) -> HistoryRow | None:
        """One event's full row, for the history page's Edit/Clone pre-fill (U43).

        Not indexed by id - a plain scan of that table's rows with a high
        limit, which is fine at this app's single-baby/Pi scale and avoids a
        new repo lookup method (events_repo.py is outside this task's
        touches). Soft-deleted rows never appear in rows(), so a deleted
        event's stale Edit/Clone link degrades to "not found" rather than
        resurrecting it.
        """
        if table not in DOMAINS:
            return None
        for r in self.rows(domains=(table,), limit=100_000):
            if r.event_id == event_id:
                return r
        return None

    def day_grouped_rows(
        self,
        domains: tuple[str, ...] = DOMAINS,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[tuple[date, list[HistoryRow]]]:
        flat = self.rows(domains=domains, since=since, until=until, limit=limit)
        groups: dict[date, list[HistoryRow]] = {}
        for r in flat:
            d = to_local(r.ts).date()
            groups.setdefault(d, []).append(r)

        result: list[tuple[date, list[HistoryRow]]] = []
        for d in sorted(groups.keys(), reverse=True):
            day_rows = sorted(groups[d], key=lambda r: r.ts)
            result.append((d, day_rows))
        return result

