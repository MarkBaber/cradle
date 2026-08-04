"""Unified event history across domains (task U4)."""

from dataclasses import dataclass
from datetime import datetime

from cradle.repos.events_repo import EventsRepo

DOMAINS = ("feed", "nappy", "sleep", "growth", "temperature", "milestone", "note")


@dataclass(frozen=True, slots=True)
class HistoryRow:
    table: str
    event_id: int
    ts: datetime
    detail: str
    logged_by: str


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
            table: str, event_id: int | None, ts: datetime, detail: str, logged_by: str
        ) -> None:
            if event_id is not None:
                out.append(HistoryRow(table, event_id, ts, detail, logged_by))

        if "feed" in wanted:
            for e in self._repo.list_feeds(limit, since, until):
                bits = [e.method.value.replace("_", " ")]
                if e.duration_min:
                    bits.append(f"{e.duration_min} min")
                if e.volume_ml:
                    bits.append(f"{e.volume_ml} ml")
                add("feed", e.event_id, e.ts, ", ".join(bits), e.logged_by)
        if "nappy" in wanted:
            for n in self._repo.list_nappies(limit, since, until):
                detail = n.kind.value
                if n.stool_colour.value != "unset":
                    detail += f" ({n.stool_colour.value.replace('_', ' ')})"
                add("nappy", n.event_id, n.ts, detail, n.logged_by)
        if "sleep" in wanted:
            for s in self._repo.list_sleeps(limit, since, until):
                if s.ts_end is None:
                    detail = f"asleep in {s.location} (running)"
                else:
                    mins = int((s.ts_end - s.ts).total_seconds() // 60)
                    detail = f"slept {mins} min in {s.location}"
                add("sleep", s.event_id, s.ts, detail, s.logged_by)
        if "growth" in wanted:
            for g in self._repo.list_growth(limit=limit):
                unit = "g" if g.measure.value == "weight" else "mm"
                add(
                    "growth",
                    g.event_id,
                    g.ts,
                    f"{g.measure.value} {g.value}{unit} ({g.source})",
                    g.logged_by,
                )
        if "temperature" in wanted:
            for t in self._repo.list_temperatures(limit, since, until):
                add("temperature", t.event_id, t.ts, f"{t.temp_c:.1f} C ({t.site})", t.logged_by)
        if "milestone" in wanted:
            for m in self._repo.list_milestones(limit):
                add("milestone", m.event_id, m.ts, f"{m.category}: {m.title}", m.logged_by)
        if "note" in wanted:
            for nt in self._repo.list_notes(limit):
                add("note", nt.event_id, nt.ts, nt.text[:80], nt.logged_by)

        # Domain repos apply since/until only where indexed; enforce uniformly here.
        if since is not None:
            out = [r for r in out if r.ts >= since]
        if until is not None:
            out = [r for r in out if r.ts < until]

        out.sort(key=lambda r: r.ts, reverse=True)
        return out[:limit]
