#!/usr/bin/env python3
"""Signal-triggered retrospective and its output ladder (DX-14).

The token-spending half of DX-10/ADR 0014: invoked only when DX-13's Stop
hook trips a friction threshold, or when asked for directly. Given a
session's captured friction events, produces the ladder Mark Baber
specified verbatim (docs/adr/0014-friction-signals-gate-the-retrospective.md):

  1. Unconditional: append one task to tasks.toml, in house shape, with a
     notes line naming the tripped signal(s) and the session that produced
     it -- the same provenance convention DX-01/DX-02/DX-06/DX-07 already
     follow by hand. The append is surgical: only new bytes are added after
     the file's current end, so every existing byte is left untouched
     (ADR 0009's discipline, extended here from update_task's in-place
     rewrite to an append).
  2. Offered, not taken: a problem summary written to
     handoff/retro-<date>-<session>.md, for an Opus review session. Never
     dispatched automatically -- this module never invokes `claude`
     (CLAUDE.md non-negotiable #3) or any subprocess at all.
  3. On an approved review: replacement task(s), rewritten in place under
     the proposal's own id. No new status value is ever introduced --
     STATUSES stays the closed vocabulary scripts/backlog.py and
     scripts/validate_tasks.py already assert against.
  4. Any proposed CLAUDE.md or build_prompt() amendment is written into the
     summary as a diff to read and approve, never applied. Enforced in
     code, not just by convention: every write in this module funnels
     through the single guarded chokepoint below, which refuses to write
     CLAUDE.md or scripts/backlog.py by name -- so no call site, however it
     derives its path, can ever reach either file.

Steps 2-4 require explicit approval; step 1 is unconditional.

    python3 scripts/retro.py propose --session-id ID --signal NAME [...]
        [--repo PATH] [--task-id ID] [--title TEXT] [--description TEXT]
        [--exit-criterion TEXT ...] [--phase N] [--routing impl]
        [--depends ID ...] [--touches PATH ...]
    python3 scripts/retro.py escalate --summary PATH --approve

Stdlib only, matching every other scripts/ tool's self-contained convention.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

REQUIRED = {"id", "phase", "title", "status", "routing", "depends", "touches",
            "description", "exit_criteria"}
STATUSES = {"todo", "in_progress", "review", "done", "blocked"}
ROUTINGS = {"architect", "impl", "tester", "reviewer"}

_FIELD_ORDER = ["id", "phase", "title", "status", "routing", "depends", "touches",
                "description", "exit_criteria", "notes", "value", "value_category",
                "value_rationale"]

# The two files this module must never be able to write, by construction
# (task DX-14's notes: a structural guarantee, not a prompt instruction).
_PROTECTED_WRITE_NAMES = {"CLAUDE.md", "backlog.py"}


class RetroError(Exception):
    pass


class ApprovalRequired(RetroError):
    pass


class TaskNotFound(RetroError):
    pass


def _write_text(path: Path, content: str) -> None:
    """The single point in this module where a write reaches disk.

    Every public write goes through here, and here alone refuses the two
    protected filenames -- so the DX-14 no-write-path rule holds regardless
    of what path a caller (or a confused prompt) passes in, not merely
    because nothing in this file happens to call it that way today.
    """
    if path.name in _PROTECTED_WRITE_NAMES:
        raise RetroError(
            f"refusing to write {path}: retro.py has no write path to the "
            "constitution or the dispatch script")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".retro-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# -- tasks.toml block rendering ---------------------------------------------

def _toml_str(s: str) -> str:
    if "\n" in s:
        return '"""' + s.replace('"""', '\\"\\"\\"') + '"""'
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_inline_list(items: list[str]) -> str:
    return "[" + ", ".join(_toml_str(i) for i in items) + "]"


def render_task_block(task: dict[str, Any]) -> str:
    """Serialize one task dict as a `[[task]]` block, house-schema shaped.

    Validates against the same closed vocabulary scripts/validate_tasks.py
    enforces, so a block this function accepts is one that gate will accept
    too.
    """
    missing = REQUIRED - task.keys()
    if missing:
        raise RetroError(f"task {task.get('id', '<no id>')}: missing fields {sorted(missing)}")
    if task["status"] not in STATUSES:
        raise RetroError(
            f"{task['id']}: bad status {task['status']!r} (allowed: {sorted(STATUSES)})")
    if task["routing"] not in ROUTINGS:
        raise RetroError(
            f"{task['id']}: bad routing {task['routing']!r} (allowed: {sorted(ROUTINGS)})")
    if not task["exit_criteria"]:
        raise RetroError(f"{task['id']}: empty exit_criteria")

    lines = ["[[task]]"]
    for key in _FIELD_ORDER:
        if key not in task:
            continue
        value = task[key]
        if key == "phase":
            lines.append(f"phase = {int(value)}")
        elif key in ("depends", "touches"):
            lines.append(f"{key} = {_toml_inline_list(list(value))}")
        elif key == "exit_criteria":
            lines.append("exit_criteria = [")
            lines.extend(f"  {_toml_str(item)}," for item in value)
            lines.append("]")
        elif key == "value":
            lines.append(f"value = {int(value)}")
        elif key == "notes":
            if value:
                lines.append(f"notes = {_toml_str(value)}")
        else:
            lines.append(f"{key} = {_toml_str(str(value))}")
    return "\n".join(lines)


def _split_task_blocks(text: str) -> list[tuple[int, int]]:
    """Byte spans of each `[[task]]` block, ending at the next top-level table.

    Mirrors nightshift.backlog.manifest's scan exactly (this module stays
    stdlib-only and does not import it, per the scripts/ convention every
    sibling tool -- backlog.py, friction.py, validate_tasks.py -- follows).
    """
    bounds: list[tuple[int, int]] = []
    for m in re.finditer(r"^\[\[task\]\]\s*$", text, re.M):
        start, after_header = m.start(), m.end()
        nxt = re.search(r"^\[", text[after_header:], re.M)
        bounds.append((start, after_header + nxt.start() if nxt else len(text)))
    return bounds


def _existing_ids(text: str) -> set[str]:
    return set(re.findall(r'^\s*id\s*=\s*"([^"]+)"\s*$', text, re.M))


def _manifest_target(repo: Path) -> tuple[bool, Path]:
    """ADR 0065(b): `tasks/_meta.toml` present -> directory mode (returns
    the `tasks/` dir); otherwise the monolithic `tasks.toml` path. Both
    containers present at once is a hard error, named."""
    tasks_dir = repo / "tasks"
    meta_path = tasks_dir / "_meta.toml"
    mono_path = repo / "tasks.toml"
    conflicts = [p for p in (mono_path, repo / "tasks.archive.toml") if p.exists()]
    if meta_path.exists() and conflicts:
        raise RetroError(
            f"both {meta_path} and {' and '.join(str(p) for p in conflicts)} exist")
    if meta_path.exists():
        return True, tasks_dir
    return False, mono_path


def _manifest_texts(repo: Path) -> list[str]:
    """Every live task file's text: one blob (`tasks.toml`) in monolithic
    mode, one per `tasks/<id>.toml` in directory mode (ADR 0065) --
    unreadable/missing files contribute nothing, same as the single-file
    `.exists()`/`OSError` guards this replaces."""
    dir_mode, target = _manifest_target(repo)
    if dir_mode:
        if not target.is_dir():
            return []
        texts = []
        for p in sorted(target.glob("*.toml")):
            if p.name.startswith("_"):
                continue
            try:
                texts.append(p.read_text(encoding="utf-8"))
            except OSError:
                continue
        return texts
    if not target.exists():
        return []
    try:
        return [target.read_text(encoding="utf-8")]
    except OSError:
        return []


def _append_task_directory(tasks_dir: Path, task: dict[str, Any]) -> Path:
    """Directory-mode counterpart of `append_task()`: one `[[task]]` entry
    per `tasks/<id>.toml`, created exclusively (ADR 0065(c)/(d)) -- never
    touches an existing file's bytes, the same guarantee the monolithic
    surgical append gives the shared file."""
    task_path = tasks_dir / f"{task['id']}.toml"
    if task_path.exists():
        raise RetroError(f"task id {task['id']!r} already exists in {task_path}")
    _write_text(task_path, render_task_block(task) + "\n")
    return task_path


def append_task(manifest_path: Path, task: dict[str, Any]) -> None:
    """Append one `[[task]]` block to the end of tasks.toml, surgically.

    Only bytes after the file's current end are added; nothing already
    written is re-parsed or reflowed, so the operator's own comments and
    formatting survive exactly (ADR 0009). Step 1 of the ladder --
    unconditional.
    """
    text = manifest_path.read_text(encoding="utf-8")
    if task["id"] in _existing_ids(text):
        raise RetroError(f"task id {task['id']!r} already exists in {manifest_path}")
    block = render_task_block(task)
    if text == "" or text.endswith("\n\n"):
        sep = ""
    elif text.endswith("\n"):
        sep = "\n"
    else:
        sep = "\n\n"
    _write_text(manifest_path, text + sep + block + "\n")


def rewrite_task_in_place(manifest_path: Path, task_id: str, task: dict[str, Any], *,
                           approved: bool) -> None:
    """Replace one `[[task]]` block's fields in place, under the same id.

    DX-10 step 3: an approved Opus review's task replaces the auto-appended
    proposal rather than sitting beside it. Requires explicit approval --
    there is no default that lets this proceed silently. Only the fields of
    the target block change; every other byte of the file, including the
    block's position, is untouched.
    """
    if not approved:
        raise ApprovalRequired("replacing a task requires explicit human approval")
    if task["id"] != task_id:
        raise RetroError("replacement task id must match the id being replaced")
    text = manifest_path.read_text(encoding="utf-8")
    for start, end in _split_task_blocks(text):
        block = text[start:end]
        if re.search(rf'^\s*id\s*=\s*"{re.escape(task_id)}"\s*$', block, re.M):
            tail = block[len(block.rstrip("\n")):]
            new_block = render_task_block(task) + tail
            _write_text(manifest_path, text[:start] + new_block + text[end:])
            return
    raise TaskNotFound(f"task {task_id!r} not found in {manifest_path}")


def apply_review(manifest_path: Path, proposal_task_id: str, tasks: list[dict[str, Any]], *,
                  approved: bool) -> None:
    """Land an approved Opus review's task(s) (DX-10 step 3).

    The first task replaces the auto-appended proposal in place, under its
    own id; any further tasks are appended as new entries -- the
    one-to-many case ADR 0014 leaves as an inference, not a verbatim
    instruction. Requires explicit approval, same as rewrite_task_in_place.
    """
    if not tasks:
        raise RetroError("a review must produce at least one task")
    first, rest = tasks[0], tasks[1:]
    rewrite_task_in_place(manifest_path, proposal_task_id, first, approved=approved)
    for extra in rest:
        append_task(manifest_path, extra)


# -- provenance + the auto-appended proposal ---------------------------------

def _signal_breakdown(events: list[dict[str, Any]]) -> list[str]:
    """One descriptive line per tripped signal (DX-109): a per-detail
    occurrence count drawn from each event's payload when it carries one --
    today, only `hook_blocked`'s `{"hook": ..., "rule": ...}` does -- e.g.
    'hook_blocked: 3x discovery_guard (unbounded_sweep)'; falls back to a
    bare occurrence count for signals whose payload carries no such detail
    (gate_failed, land_retried, ...). Every line still contains its bare
    signal name as a substring, which proposed_signals_for_session()'s and
    find_proposal_task_id()'s dedup scans depend on."""
    by_signal: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        signal = event.get("signal")
        if not signal:
            continue
        by_signal.setdefault(signal, []).append(event)

    lines: list[str] = []
    for signal in sorted(by_signal):
        occurrences = by_signal[signal]
        detail_counts: dict[str, int] = {}
        undetailed = 0
        for event in occurrences:
            payload = event.get("payload")
            hook = payload.get("hook") if isinstance(payload, dict) else None
            rule = payload.get("rule") if isinstance(payload, dict) else None
            if hook and rule:
                detail = f"{hook} ({rule})"
            elif hook:
                detail = str(hook)
            else:
                undetailed += 1
                continue
            detail_counts[detail] = detail_counts.get(detail, 0) + 1
        parts = [f"{count}x {detail}" for detail, count in sorted(detail_counts.items())]
        if undetailed:
            if detail_counts:
                # A mix of detailed and undetailed occurrences for the same
                # signal -- account for the undetailed ones too, rather than
                # silently dropping them and understating the trip count.
                parts.append(f"{undetailed}x (unspecified)")
            elif undetailed > 1:
                parts = [f"{undetailed}x"]
        if parts:
            lines.append(f"{signal}: {', '.join(parts)}")
        else:
            lines.append(signal)
    return lines


def compose_provenance_notes(
    signals: list[str], session_id: str, events: list[dict[str, Any]] | None = None,
) -> str:
    breakdown = "; ".join(_signal_breakdown(events)) if events else ", ".join(sorted(signals))
    return (f"Auto-appended by scripts/retro.py (DX-14) after session {session_id} "
            f"tripped: {breakdown}.")


def proposed_signals_for_session(repo: Path, session_id: str) -> set[str]:
    """Find friction signals for session_id already recorded in proposal
    tasks -- across every live task file, mono or directory mode
    (ADR 0065; `repo`, not a single pre-resolved manifest path)."""
    proposed: set[str] = set()
    for text in _manifest_texts(repo):
        for start, end in _split_task_blocks(text):
            block = text[start:end]
            notes_match = re.search(
                r'^\s*notes\s*=\s*"""(.*?)"""|^\s*notes\s*=\s*"(.*?)"',
                block,
                re.M | re.S,
            )
            notes_text = ""
            if notes_match:
                notes_text = notes_match.group(1) or notes_match.group(2) or ""
            if session_id in notes_text or f"RETRO-{session_id}" in block:
                for sig in (
                    "gate_failed", "hook_blocked", "land_retried", "ci_status_still_running",
                    "turn_cap_reached", "edit_outside_touches", "ci_failed_after_local_pass",
                ):
                    if sig in notes_text or sig in block:
                        proposed.add(sig)
    return proposed


def find_proposal_task_id(repo: Path, session_id: str) -> str | None:
    """Find the proposal task ID for session_id, across every live task
    file, mono or directory mode (ADR 0065)."""
    for text in _manifest_texts(repo):
        for start, end in _split_task_blocks(text):
            block = text[start:end]
            notes_match = re.search(
                r'^\s*notes\s*=\s*"""(.*?)"""|^\s*notes\s*=\s*"(.*?)"',
                block,
                re.M | re.S,
            )
            notes_text = (
                notes_match.group(1) or notes_match.group(2) or "" if notes_match else ""
            )
            if session_id in notes_text or f"RETRO-{session_id}" in block:
                id_match = re.search(r'^\s*id\s*=\s*"([^"]+)"\s*$', block, re.M)
                if id_match:
                    return id_match.group(1)
    return None


def _generate_unique_task_id(repo: Path, session_id: str) -> str:
    base = f"RETRO-{session_id}"
    existing: set[str] = set()
    for text in _manifest_texts(repo):
        existing |= _existing_ids(text)
    if base not in existing:
        return base
    counter = 2
    while f"{base}-{counter}" in existing:
        counter += 1
    return f"{base}-{counter}"


def propose(
    repo: Path,
    *,
    session_id: str,
    signals: list[str] | None = None,
    events: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    exit_criteria: list[str] | None = None,
    phase: int = 6,
    routing: str = "impl",
    depends: list[str] | None = None,
    touches: list[str] | None = None,
) -> RetroResult:
    """Step 1 (unconditional append) + Step 2 (offered summary) for a set of
    tripped signals -- or, given full `events` (DX-109: session_id/task_id/
    payload preserved), the richer provenance those carry instead of just
    their bare signal names. At least one of `signals`/`events` must be
    given; when both are, `events` takes precedence and `signals` is
    ignored."""
    tid = task_id or _generate_unique_task_id(repo, session_id)
    if events is None:
        if signals is None:
            raise RetroError("propose() needs signals or events")
        events = [{"signal": s, "session_id": session_id} for s in signals]
    return run(
        repo,
        session_id=session_id,
        events=events,
        task_id=tid,
        title=title,
        description=description,
        exit_criteria=exit_criteria,
        phase=phase,
        routing=routing,
        depends=depends,
        touches=touches,
    )


def propose_task(*, task_id: str, title: str, signals: list[str], session_id: str,
                  phase: int, description: str, exit_criteria: list[str],
                  routing: str = "impl", depends: list[str] | None = None,
                  touches: list[str] | None = None,
                  value: int = 3, value_category: str = "stability",
                  value_rationale: str | None = None,
                  events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not signals:
        raise RetroError("a retro proposal needs at least one tripped signal")
    return {
        "id": task_id,
        "phase": phase,
        "title": title,
        "status": "todo",
        "routing": routing,
        "depends": list(depends or []),
        "touches": list(touches or []),
        "description": description,
        "exit_criteria": list(exit_criteria),
        "notes": compose_provenance_notes(signals, session_id, events=events),
        "value": value,
        "value_category": value_category,
        "value_rationale": (
            value_rationale or f"Auto-appended retro proposal for session {session_id}."
        ),
    }


def signals_from_events(events: list[dict[str, Any]]) -> list[str]:
    return sorted({e["signal"] for e in events if "signal" in e})


# -- the problem summary (offered, never dispatched) -------------------------

def retro_summary_path(repo: Path, session_id: str, *, date: str | None = None) -> Path:
    d = date or datetime.now(UTC).strftime("%Y-%m-%d")
    return repo / "handoff" / f"retro-{d}-{session_id}.md"


def _event_table_row(event: dict[str, Any]) -> str:
    """One `| Timestamp | Signal | Hook | Rule | Context |` row (DX-109),
    replacing the prior synthetic `f"- {event}"` raw-dict-repr bullet.
    `Context` is whichever of task_id/session_id the event carries, in that
    preference order -- a resolved task_id is the more useful anchor when
    both are present."""
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    timestamp = event.get("timestamp") or ""
    signal = event.get("signal") or ""
    hook = payload.get("hook") or ""
    rule = payload.get("rule") or ""
    context = event.get("task_id") or event.get("session_id") or ""
    return f"| {timestamp} | {signal} | {hook} | {rule} | {context} |"


def write_retro_summary(
    repo: Path, *, session_id: str, signals: list[str], proposal_task_id: str,
    events: list[dict[str, Any]] | None = None,
    claude_md_diff: str | None = None, backlog_py_diff: str | None = None,
    date: str | None = None,
) -> Path:
    """Write the problem summary for an offered (not dispatched) Opus review.

    Any proposed constitution or dispatch-script amendment is embedded here
    as prose/diff text to read and approve -- this function, like every
    other write in this module, goes through _write_text, which cannot
    target either file, so there is no path by which this could apply one
    itself.
    """
    path = retro_summary_path(repo, session_id, date=date)
    lines = [
        f"# Retro: session {session_id}",
        "",
        f"Signals tripped: {', '.join(sorted(signals))}",
        f"Auto-appended proposal: {proposal_task_id}",
        "",
        "This summary is offered for an Opus review session. It is not "
        "dispatched automatically -- escalation needs explicit approval "
        "(see `retro.offer_opus_review`).",
    ]
    if events:
        lines += ["", "## Events", "", "| Timestamp | Signal | Hook | Rule | Context |",
                   "|---|---|---|---|---|"]
        lines.extend(_event_table_row(event) for event in events)
    if claude_md_diff:
        lines += ["", "## Proposed CLAUDE.md amendment (NOT applied -- approve to apply)",
                   "", "```diff", claude_md_diff, "```"]
    if backlog_py_diff:
        lines += ["", "## Proposed scripts/backlog.py amendment (NOT applied -- approve to apply)",
                   "", "```diff", backlog_py_diff, "```"]
    _write_text(path, "\n".join(lines) + "\n")
    return path


def offer_opus_review(summary_path: Path, *, approved: bool) -> str | None:
    """Offer, never take (DX-10 step 2).

    Returns a dispatch-ready description of the review only when the
    caller passes `approved=True` explicitly -- there is no default, so a
    call site cannot silently proceed. Never invokes `claude` itself
    (CLAUDE.md non-negotiable #3): the return value is text for a human or
    an external dispatcher to act on, nothing more.
    """
    if not approved:
        raise ApprovalRequired("the Opus review needs explicit approval before it runs")
    return f"opus review requested for {summary_path}"


# -- orchestration: steps 1-2 for one session's captured events -------------

class RetroResult(NamedTuple):
    task_id: str
    manifest_path: Path
    summary_path: Path
    signals: list[str]


def _default_task_id(session_id: str) -> str:
    return f"RETRO-{session_id}"


def _default_title(
    signals: list[str], session_id: str, events: list[dict[str, Any]] | None = None,
) -> str:
    breakdown = "; ".join(_signal_breakdown(events)) if events else ", ".join(signals)
    return f"Retro follow-up: {breakdown} (session {session_id})"


def _default_description() -> str:
    return (
        "Auto-appended by scripts/retro.py (DX-14) -- an unconditional, mid-friction "
        "first pass. Expected to be re-scoped by a human and, on an approved Opus "
        "review, replaced in place (DX-10 step 3) rather than dispatched as written."
    )


def _default_exit_criteria() -> list[str]:
    return ["re-scoped and confirmed by a human before this task is dispatched"]


def run(
    repo: Path, *, session_id: str, events: list[dict[str, Any]],
    task_id: str | None = None, title: str | None = None,
    description: str | None = None, exit_criteria: list[str] | None = None,
    phase: int = 6, routing: str = "impl",
    depends: list[str] | None = None, touches: list[str] | None = None,
) -> RetroResult:
    """Run the unconditional half of the ladder (steps 1-2) for one
    session's captured friction events. Steps 3-4 are separate,
    approval-gated calls: rewrite_task_in_place/apply_review, and
    offer_opus_review.
    """
    signals = signals_from_events(events)
    if not signals:
        raise RetroError("no signals in the given events -- nothing to retro")
    tid = task_id or _default_task_id(session_id)
    task = propose_task(
        task_id=tid, title=title or _default_title(signals, session_id, events=events),
        signals=signals, session_id=session_id, phase=phase,
        description=description or _default_description(),
        exit_criteria=exit_criteria or _default_exit_criteria(),
        routing=routing, depends=depends, touches=touches, events=events,
    )
    dir_mode, target = _manifest_target(repo)
    if dir_mode:
        manifest_path = _append_task_directory(target, task)
    else:
        manifest_path = target
        append_task(manifest_path, task)
    summary_path = write_retro_summary(
        repo, session_id=session_id, signals=signals, proposal_task_id=tid, events=events,
    )
    return RetroResult(task_id=tid, manifest_path=manifest_path,
                        summary_path=summary_path, signals=signals)


# -- CLI ----------------------------------------------------------------------

def cmd_propose(args: argparse.Namespace) -> int:
    repo = Path(args.repo) if args.repo else Path.cwd()
    events: list[dict[str, Any]] = [{"signal": s, "session_id": args.session_id}
                                     for s in args.signal]
    result = run(
        repo, session_id=args.session_id, events=events, task_id=args.task_id,
        title=args.title, description=args.description,
        exit_criteria=args.exit_criterion, phase=args.phase, routing=args.routing,
        depends=args.depends, touches=args.touches,
    )
    print(f"appended {result.task_id} to {result.manifest_path}")
    print(f"summary written to {result.summary_path} -- offered for an Opus "
          "review, not dispatched")
    return 0


def cmd_escalate(args: argparse.Namespace) -> int:
    try:
        result = offer_opus_review(Path(args.summary), approved=args.approve)
    except ApprovalRequired as e:
        print(str(e))
        return 1
    print(result)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retro.py")
    sub = parser.add_subparsers(dest="command", required=True)

    propose = sub.add_parser("propose", help="step 1 (unconditional) + step 2 (offered)")
    propose.add_argument("--session-id", required=True)
    propose.add_argument("--signal", action="append", required=True,
                          help="a tripped signal name; repeatable")
    propose.add_argument("--repo", default=None)
    propose.add_argument("--task-id", default=None)
    propose.add_argument("--title", default=None)
    propose.add_argument("--description", default=None)
    propose.add_argument("--exit-criterion", action="append", default=None)
    propose.add_argument("--phase", type=int, default=6)
    propose.add_argument("--routing", default="impl", choices=sorted(ROUTINGS))
    propose.add_argument("--depends", action="append", default=[])
    propose.add_argument("--touches", action="append", default=[])
    propose.set_defaults(func=cmd_propose)

    escalate = sub.add_parser("escalate", help="step 2: offer, requires --approve")
    escalate.add_argument("--summary", required=True)
    escalate.add_argument("--approve", action="store_true")
    escalate.set_defaults(func=cmd_escalate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
