#!/usr/bin/env python3
"""Canonical house tasks.toml validator (scaffold-toolkit). Copy verbatim.

Checks: required fields, canonical status/routing vocabulary, non-empty
exit_criteria, dependency existence, acyclic graph. CI 'validate' stage gate.
Run from repo root: python3 scripts/validate_tasks.py

The validation contract above is the canonical scaffold-toolkit behaviour and
is unchanged by no-arg invocation (exit 0/1, same messages) so other house
projects can still diff this file against the toolkit copy.

NightShift-local addition, layered on top rather than into the contract:
reporting on the ready/blocked backlog for a follow-up dispatch decision.

    python3 scripts/validate_tasks.py --agent   # JSON for a toolcall
    python3 scripts/validate_tasks.py --human   # coloured terminal report

If tasks.archive.toml exists beside tasks.toml (docs/adr/0049), its ids join
the known-id set a `depends` edge resolves against; a parse error there fails
the gate the same way an invalid tasks.toml does. Absent that file, every
code path and the output are unchanged, so the no-arg contract above still
diffs byte-identical against the toolkit copy.

If tasks/_meta.toml exists (docs/adr/0065), the manifest is directory-mode:
one [[task]] entry per tasks/<id>.toml, tasks/_meta.toml for [meta], and
tasks/archive/<id>.toml in place of tasks.archive.toml. This is detected
purely by that marker file, never by a bare tasks/ directory existing, so a
project that has not opted in is unaffected. Absent tasks/_meta.toml, every
code path above is exactly what runs today.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path

REQUIRED = {"id", "phase", "title", "status", "routing", "depends", "touches",
            "description", "exit_criteria", "value", "value_category", "value_rationale"}
VALUE_FIELDS = {"value", "value_category", "value_rationale"}
BASE_REQUIRED = REQUIRED - VALUE_FIELDS
STATUSES = {"todo", "in_progress", "review", "done", "blocked"}
ROUTINGS = {"architect", "impl", "tester", "reviewer"}
VALUE_CATEGORIES = {"stability", "leverage", "velocity", "efficiency"}

# SPEC 4.2 / ADR 0001: review satisfies downstream depends; only impl/tester
# routing is autonomous-dispatchable, architect/reviewer stay human-gated.
SCHEDULABLE = {"todo"}
SATISFIED = {"done", "review"}
AUTONOMOUS = {"impl", "tester"}


def validate(tasks: list[dict],
            archived_ids: frozenset[str] = frozenset()) -> tuple[list[str], dict[str, dict]]:
    """`archived_ids` (ADR 0049) are ids moved out to tasks.archive.toml: a
    `depends` edge to one resolves as known, but an archived id never enters
    `by_id` -- it contributes only its own existence, nothing else."""
    errors: list[str] = []
    by_id: dict[str, dict] = {}
    known: set[str] = {t["id"] for t in tasks if isinstance(t.get("id"), str)} | set(archived_ids)

    for t in tasks:
        req = REQUIRED if t.get("status") not in ("done", "blocked") else BASE_REQUIRED
        missing = req - t.keys()
        if missing:
            errors.append(f"{t.get('id', '<no id>')}: missing fields {sorted(missing)}")
            continue
        if t["id"] in by_id or t["id"] in archived_ids:
            # First declaration wins -- an unconditional overwrite here would
            # let a later duplicate silently clobber the earlier one, so
            # dependency resolution would resolve against the wrong edges.
            # An id already claimed by the archive is a reuse, reported the
            # same way (ADR 0049 rule 6): an archived id may never return.
            errors.append(f"duplicate task id {t['id']}")
        else:
            by_id[t["id"]] = t
        if t["status"] not in STATUSES:
            errors.append(f"{t['id']}: bad status '{t['status']}' (allowed: {sorted(STATUSES)})")
        if t["routing"] not in ROUTINGS:
            errors.append(f"{t['id']}: bad routing '{t['routing']}' (allowed: {sorted(ROUTINGS)})")
        if not t["exit_criteria"]:
            errors.append(f"{t['id']}: empty exit_criteria")

        if "value" in t:
            v = t["value"]
            if not (type(v) is int and 1 <= v <= 5):
                errors.append(f"{t['id']}: bad value {v!r} (allowed: integer 1-5)")
        if "value_category" in t:
            c = t["value_category"]
            if c not in VALUE_CATEGORIES:
                allowed_cats = sorted(VALUE_CATEGORIES)
                errors.append(f"{t['id']}: bad value_category '{c}' (allowed: {allowed_cats})")
        if "value_rationale" in t:
            r = t["value_rationale"]
            if not isinstance(r, str) or not r.strip():
                errors.append(f"{t['id']}: empty value_rationale")
        if "created_at" in t:
            ts = t["created_at"]
            if not isinstance(ts, str):
                errors.append(f"{t['id']}: bad created_at {ts!r} (allowed: ISO 8601 string)")
            else:
                try:
                    datetime.fromisoformat(ts)
                except ValueError:
                    errors.append(f"{t['id']}: bad created_at {ts!r} (not ISO 8601)")

    for t in by_id.values():
        for dep in t["depends"]:
            # Checked against every declared id, not just complete ones: a
            # task missing an unrelated required field must not cascade a
            # spurious "unknown dependency" into everything that depends on it.
            if dep not in known:
                errors.append(f"{t['id']}: unknown dependency '{dep}'")

    WHITE, GREY, BLACK = 0, 1, 2
    colour = {tid: WHITE for tid in by_id}
    for start in by_id:
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        colour[start] = GREY
        while stack:
            node, i = stack[-1]
            deps = [d for d in by_id[node]["depends"] if d in by_id]
            if i < len(deps):
                stack[-1] = (node, i + 1)
                nxt = deps[i]
                if colour[nxt] == GREY:
                    # One error per back edge. Leave nxt GREY rather than
                    # marking it BLACK here: doing so would mask a second,
                    # different cycle running back through the same node.
                    errors.append(f"dependency cycle through {nxt}")
                elif colour[nxt] == WHITE:
                    colour[nxt] = GREY
                    stack.append((nxt, 0))
            else:
                colour[node] = BLACK
                stack.pop()

    return errors, by_id


#: ADR 0065(a): directory-mode id rule -- a property of the directory
#: container, not the schema; monolithic mode has no charset constraint.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _discover_directory(tasks_dir: Path) -> tuple[list[dict], list[str]]:
    """Parse tasks/*.toml (the live, non-recursive set) independently of
    tasks/_meta.toml, collecting every parse error, filename/id mismatch,
    reserved-name violation and case collision -- each reported by path
    (ADR 0065(c)). Independent implementation from manifest.py's own loader,
    per ADR 0024."""
    errors: list[str] = []
    tasks: list[dict] = []
    seen_ci: dict[str, Path] = {}
    for p in sorted(tasks_dir.glob("*.toml")):
        if p.name.startswith("_"):
            if p.name != "_meta.toml":
                errors.append(f"{p}: reserved filename")
                continue
            try:
                meta_doc = tomllib.loads(p.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as e:
                errors.append(f"{p}: {e}")
                continue
            if "task" in meta_doc:
                errors.append(f"{p}: must not hold a 'task' entry")
            continue
        try:
            doc = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            errors.append(f"{p}: {e}")
            continue
        extra_tables = set(doc) - {"task"}
        if extra_tables:
            errors.append(f"{p}: unexpected top-level table(s) {sorted(extra_tables)}")
        task_list = doc.get("task", [])
        if len(task_list) != 1:
            errors.append(f"{p}: expected exactly one [[task]] entry, found {len(task_list)}")
            continue
        t = task_list[0]
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str) or not _ID_RE.match(tid):
            errors.append(f"{p}: id {tid!r} does not match the directory-mode id pattern")
            continue
        if tid != p.stem:
            errors.append(f"{p}: id {tid!r} does not match filename stem {p.stem!r}")
            continue
        low = tid.lower()
        if low in seen_ci:
            errors.append(f"{p}: id {tid!r} collides case-insensitively with {seen_ci[low]}")
            continue
        seen_ci[low] = p
        tasks.append(t)
    return tasks, errors


def _discover_archive_directory(archive_dir: Path) -> tuple[frozenset[str], list[str]]:
    """Archived ids from tasks/archive/*.toml file stems, each validated
    against its own `id` (ADR 0065(g), amending ADR 0049 rule 3)."""
    if not archive_dir.is_dir():
        return frozenset(), []
    errors: list[str] = []
    ids: set[str] = set()
    for p in sorted(archive_dir.glob("*.toml")):
        try:
            doc = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            errors.append(f"{p}: {e}")
            continue
        task_list = doc.get("task", [])
        if len(task_list) != 1:
            errors.append(f"{p}: expected exactly one [[task]] entry, found {len(task_list)}")
            continue
        t = task_list[0]
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str) or tid != p.stem:
            errors.append(f"{p}: id {tid!r} does not match filename stem {p.stem!r}")
            continue
        ids.add(tid)
    return frozenset(ids), errors


def _slug(title: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-").split("-")
    slug, length = [], 0
    for w in words:
        if slug and length + 1 + len(w) > 40:
            break
        slug.append(w)
        length += len(w) + (1 if len(slug) > 1 else 0)
    return "-".join(slug)


def classify(by_id: dict[str, dict]) -> dict[str, list]:
    """Bucket todo/in_progress tasks into ready / needs_routing / blocked."""
    ready, needs_routing, blocked, in_progress = [], [], [], []

    for t in by_id.values():
        if t["status"] == "in_progress":
            in_progress.append(t)
            continue
        if t["status"] != "todo":
            continue
        missing = [d for d in t["depends"] if by_id.get(d, {}).get("status") not in SATISFIED]
        if missing:
            blocked.append((t, missing))
        elif t["routing"] not in AUTONOMOUS:
            needs_routing.append(t)
        else:
            ready.append(t)

    key = lambda t: (t["phase"], t["id"])  # noqa: E731
    ready.sort(key=key)
    needs_routing.sort(key=key)
    in_progress.sort(key=key)
    blocked.sort(key=lambda pair: key(pair[0]))
    return {"ready": ready, "needs_routing": needs_routing,
             "blocked": blocked, "in_progress": in_progress}


def recommend(ready: list[dict]) -> dict | None:
    """Next-best candidate: earliest phase, then id, among the ready set."""
    if not ready:
        return None
    t = ready[0]
    branch = f"task/{t['id']}-{_slug(t['title'])}"
    prompt = (
        f"Implement task {t['id']} ({t['title']}) on branch {branch}. "
        f"Read CLAUDE.md, docs/SPEC.md and any docs/adr/ records it cites first — "
        f"do not re-litigate locked decisions. Stay strictly inside touches: "
        f"{', '.join(t['touches'])}. CI is the sole arbiter of done; the named "
        f"exit_criteria are the definition of done: {'; '.join(t['exit_criteria'])}. "
        f"Set status to 'review', never 'done', in the same diff."
    )
    return {"id": t["id"], "branch": branch, "prompt": prompt}


def render_agent(report: dict) -> str:
    def brief(t: dict) -> dict:
        return {
            "id": t["id"], "phase": t["phase"], "title": t["title"],
            "routing": t["routing"], "depends": t["depends"],
            "touches": t["touches"], "exit_criteria": t["exit_criteria"],
        }

    payload = {
        "ready": [brief(t) for t in report["ready"]],
        "in_progress": [brief(t) for t in report["in_progress"]],
        "needs_routing": [
            {**brief(t), "reason": f"routing '{t['routing']}' requires human dispatch (ADR 0001)"}
            for t in report["needs_routing"]
        ],
        "blocked": [
            {**brief(t), "blocked_on": missing}
            for t, missing in report["blocked"]
        ],
        "recommendation": recommend(report["ready"]),
    }
    return json.dumps(payload, indent=2)


class _C:
    """ANSI colour codes, disabled when stdout isn't a tty or NO_COLOR is set."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self("1", text)

    def green(self, text: str) -> str:
        return self("32", text)

    def yellow(self, text: str) -> str:
        return self("33", text)

    def blue(self, text: str) -> str:
        return self("34", text)

    def red(self, text: str) -> str:
        return self("31", text)

    def dim(self, text: str) -> str:
        return self("2", text)


def render_human(report: dict, by_id: dict[str, dict]) -> str:
    import os
    c = _C(sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    lines: list[str] = []

    def task_line(t: dict) -> str:
        n_touches, n_criteria = len(t["touches"]), len(t["exit_criteria"])
        return (f"  {c.bold(t['id']):16} phase {t['phase']}  "
                f"{n_touches} touches / {n_criteria} criteria  {t['title']}")

    lines.append(c.bold(f"tasks.toml — {len(by_id)} tasks"))
    lines.append("")

    ready = report["ready"]
    lines.append(c.green(c.bold(f"READY ({len(ready)})"))
                 + c.dim(" — todo, deps satisfied, autonomous routing"))
    if ready:
        lines.extend(task_line(t) for t in ready)
    else:
        lines.append(c.dim("  (none)"))
    lines.append("")

    in_progress = report["in_progress"]
    if in_progress:
        lines.append(c.yellow(c.bold(f"IN PROGRESS ({len(in_progress)})")))
        lines.extend(task_line(t) for t in in_progress)
        lines.append("")

    needs_routing = report["needs_routing"]
    if needs_routing:
        lines.append(c.blue(c.bold(f"NEEDS ROUTING ({len(needs_routing)})"))
                      + c.dim(" — deps satisfied, awaiting human/architect dispatch"))
        for t in needs_routing:
            lines.append(task_line(t) + c.dim(f"  [{t['routing']}]"))
        lines.append("")

    blocked = report["blocked"]
    lines.append(c.red(c.bold(f"BLOCKED ({len(blocked)})"))
                 + c.dim(" — waiting on incomplete depends"))
    if blocked:
        for t, missing in blocked:
            waits = ", ".join(f"{d} [{by_id[d]['status']}]" for d in missing)
            lines.append(task_line(t))
            lines.append(c.dim(f"    waiting on: {waits}"))
    else:
        lines.append(c.dim("  (none)"))
    lines.append("")

    rec = recommend(ready)
    if rec:
        lines.append(c.bold("NEXT BEST CANDIDATE") + f": {c.green(rec['id'])}  ({rec['branch']})")
        lines.append(c.dim("  Suggested prompt:"))
        lines.append(f"  {rec['prompt']}")
    else:
        lines.append(c.dim("No ready tasks — nothing autonomous-dispatchable right now."))

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--agent", action="store_true",
                       help="print ready/blocked backlog as JSON for a toolcall")
    mode.add_argument("--human", action="store_true",
                       help="print a coloured, human-readable backlog report")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    path = root / "tasks.toml"

    # ADR 0065(b): mode is decided purely by tasks/_meta.toml existing, never
    # by a bare tasks/ dir -- a project that hasn't opted in is unaffected.
    # Both containers present at once is a hard error naming both paths.
    tasks_dir = root / "tasks"
    meta_path = tasks_dir / "_meta.toml"
    archive_mono_path = root / "tasks.archive.toml"
    conflicts = [p for p in (path, archive_mono_path) if p.exists()]
    if meta_path.exists() and conflicts:
        print("tasks manifest INVALID: both tasks/_meta.toml and "
              f"{' and '.join(str(p) for p in conflicts)} exist")
        print(f"  - {meta_path}")
        for p in conflicts:
            print(f"  - {p}")
        return 1

    if meta_path.exists():
        tasks, loader_errors = _discover_directory(tasks_dir)
        archived_ids, archive_errors = _discover_archive_directory(tasks_dir / "archive")
        errors, by_id = validate(tasks, archived_ids)
        errors = loader_errors + archive_errors + errors
        if errors:
            print("tasks/ INVALID:")
            for e in errors:
                print("  -", e)
            return 1
    else:
        tasks = tomllib.loads(path.read_text(encoding="utf-8")).get("task", [])

        # ADR 0049: tasks.archive.toml, discovered by convention beside
        # tasks.toml, never by an argv flag -- the no-arg invocation other
        # house projects diff against this file must stay unchanged.
        archive_path = path.parent / "tasks.archive.toml"
        archived_ids = frozenset()
        if archive_path.exists():
            try:
                archived_tasks = tomllib.loads(
                    archive_path.read_text(encoding="utf-8")).get("task", [])
            except tomllib.TOMLDecodeError as exc:
                print("tasks.archive.toml INVALID:")
                print(f"  - {exc}")
                return 1
            archived_ids = frozenset(
                t["id"] for t in archived_tasks
                if isinstance(t, dict) and isinstance(t.get("id"), str)
            )

        errors, by_id = validate(tasks, archived_ids)

        if errors:
            print("tasks.toml INVALID:")
            for e in errors:
                print("  -", e)
            return 1

    if args.agent:
        print(render_agent(classify(by_id)))
    elif args.human:
        print(render_human(classify(by_id), by_id))
    else:
        print(f"OK: {len(by_id)} tasks, graph acyclic, schema complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
