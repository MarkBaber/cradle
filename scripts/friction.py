#!/usr/bin/env python3
"""Friction-signal capture and the deterministic Stop-hook trigger (DX-13).

Implements the taxonomy fixed by
docs/adr/0014-friction-signals-gate-the-retrospective.md: an append-only
event log at .cockpit/friction.jsonl, and a threshold check with no model in
the loop. Counted signals (gate_failed, land_retried) need a
second occurrence to be friction -- the first is normal iteration.
Single-occurrence signals (turn_cap_reached, edit_outside_touches,
ci_failed_after_local_pass) are friction on their own.

    python3 scripts/friction.py record <signal> [--session-id ID] \
        [--task-id ID] [--payload JSON] [--repo PATH]
    python3 scripts/friction.py stop [--repo PATH]   # reads hook JSON on stdin

Stdlib only. `record` always exits 0: a friction-recording failure must
never break the hook or script that calls it. `stop` exits 0 when nothing
tripped and 2 -- with the summary on stderr -- when something did: a Stop
hook's stdout on exit 0 is transcript-only and never reaches the model, so
exit 2 is the channel that actually surfaces a tripped ladder to the session.
"""
from __future__ import annotations

import argparse
import importlib.util as importutil
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: `<type>/<id>-<slug>` (CLAUDE.md, Working rules) -- mirrors land.py's
#: BRANCH_RE. Duplicated, not imported: every scripts/ tool here is
#: standalone (see module docstring, "Stdlib only").
_BRANCH_TASK_ID_RE = re.compile(r"^[a-z]+/(?P<id>[A-Z][A-Z0-9]*-\d+)-")

COUNTED_SIGNALS = frozenset({
    "gate_failed", "land_retried", "ci_status_still_running",
})
SINGLE_OCCURRENCE_SIGNALS = frozenset({
    "turn_cap_reached", "edit_outside_touches", "ci_failed_after_local_pass",
})
#: DX-149: recorded for triage (cockpit FRICTION pane, retro.py) but never
#: counted toward a trip: a PreToolUse refusal already interrupts the turn
#: and says why, so counting it again only blocks turn-end for no new info.
LOGGED_ONLY_SIGNALS = frozenset({"hook_blocked"})
SIGNALS = COUNTED_SIGNALS | SINGLE_OCCURRENCE_SIGNALS | LOGGED_ONLY_SIGNALS

#: DX-85: `stop_reported` is a high-water-mark event the Stop hook writes to
#: its own log after reporting a trip, so a later Stop can tell whether any
#: relevant event has arrived since. It is deliberately not in SIGNALS:
#: tripped_signals()'s existing `signal not in SIGNALS: continue` filter
#: already keeps it out of every threshold count for free.
META_SIGNALS = frozenset({"stop_reported"})

THRESHOLDS: dict[str, int] = {
    **{signal: 2 for signal in COUNTED_SIGNALS},
    **{signal: 1 for signal in SINGLE_OCCURRENCE_SIGNALS},
}

#: DX-46: one-line human-readable explanation + remediation hint per signal,
#: covering every member of SIGNALS. Keep this table in sync with SIGNALS --
#: the test_every_signal_has_a_hint unit test enforces the invariant.
SIGNAL_HINTS: dict[str, str] = {
    "gate_failed": (
        "gate_failed: a local test/lint gate failed twice; "
        "check `scripts/ci_status.py`, not a raw log (ADR 0028)."
    ),
    "hook_blocked": (
        "hook_blocked: a PreToolUse guard (split_guard, worktree_guard, "
        "discovery_guard, or the commit/push wrapper hooks) refused a "
        "command twice; resolve the underlying issue before resuming "
        "(ADR 0014)."
    ),
    "land_retried": (
        "land_retried: `scripts/land.py` needed more than one attempt twice; "
        "check for merge conflicts or a failing CI gate (ADR 0014)."
    ),
    "turn_cap_reached": (
        "turn_cap_reached: the session hit its turn cap without finishing; "
        "break the task into smaller steps or raise the cap in CLAUDE.md."
    ),
    "edit_outside_touches": (
        "edit_outside_touches: the session edited a file outside its declared "
        "touches list; update `touches` in tasks.toml or revert the stray edit."
    ),
    "ci_failed_after_local_pass": (
        "ci_failed_after_local_pass: CI failed after a clean local gate; "
        "check `scripts/ci_status.py` for environment differences (ADR 0028)."
    ),
    "ci_status_still_running": (
        "ci_status_still_running: `scripts/ci_status.py` was re-invoked twice "
        "on the same branch while the pipeline was still unresolved; use a "
        "`Monitor` until-loop instead of polling from a fresh turn (DX-62)."
    ),
}

#: DX-45: a lifetime tally with no decay means one old trip blocks every
#: future Stop check forever, for any session that later resumes the same
#: task branch. An event only counts toward a fresh trip if it's within this
#: many seconds of "now" -- long enough to cover a single working session,
#: short enough that a stale trip clears on the next day's resume. Events
#: with a missing or unparseable timestamp still count (older log lines, and
#: hand-built test fixtures, predate this field's use in trip decisions).
DECAY_WINDOW_SECONDS = 4 * 60 * 60


def _load_retro_module(repo: Path) -> Any:
    """Load retro.py module by path (ADR 0053/OB-10 by-path pattern)."""
    path = repo / "scripts" / "retro.py"
    if not path.exists():
        path = Path(__file__).resolve().parent / "retro.py"
    spec = importutil.spec_from_file_location("_friction_retro", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load retro.py from {path}")
    module = importutil.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_common_dir(repo: Path) -> Path | None:
    """The shared `.git` dir a linked worktree and its main checkout both
    resolve to -- best-effort, never raises (DX-38 finding 2)."""
    git_file_or_dir = repo / ".git"
    if git_file_or_dir.is_dir():
        return git_file_or_dir.resolve()
    if git_file_or_dir.is_file():
        try:
            content = git_file_or_dir.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                gitdir_path = Path(content[len("gitdir:"):].strip())
                if not gitdir_path.is_absolute():
                    gitdir_path = (repo / gitdir_path).resolve()
                else:
                    gitdir_path = gitdir_path.resolve()
                if gitdir_path.parent.name == "worktrees":
                    return gitdir_path.parent.parent
                return gitdir_path
        except OSError:
            pass
    if not git_file_or_dir.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    common_dir = result.stdout.strip()
    if not common_dir:
        return None
    path = Path(common_dir)
    if not path.is_absolute():
        path = repo / path
    return path.resolve()


def _resolve_task_id_for_session(repo: Path, session_id: str | None) -> str | None:
    """Best-effort session_id -> task_id resolution against recent dispatch
    sidecars (DX-109), for hook-emitted events (`hook_blocked`) that carry a
    session_id but no task_id of their own -- unlike current_task_id()'s
    branch-name resolution, this covers a session whose branch has since
    moved on or was never task-named. Duplicated, not imported, from
    cockpit.py's/session_brief.py's `.cockpit/logs/dispatch-*.json` sidecar
    scan (module docstring, "Stdlib only"). Never raises."""
    if not session_id:
        return None
    common_dir = _git_common_dir(repo)
    root = common_dir.parent if common_dir is not None else repo
    logs_dir = root / ".cockpit" / "logs"
    if not logs_dir.is_dir():
        return None
    try:
        candidates = sorted(logs_dir.glob("dispatch-*.json"))
    except OSError:
        return None
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("session_id") == session_id:
            task_id = data.get("task_id")
            if isinstance(task_id, str) and task_id:
                return task_id
    return None


def current_task_id(repo: Path) -> str | None:
    """The task id named by `repo`'s current branch, `<type>/<id>-<slug>`
    (CLAUDE.md) -- best-effort, never raises (DX-38 finding 1)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _BRANCH_TASK_ID_RE.match(result.stdout.strip())
    return match["id"] if match else None


def friction_log_path(repo: Path) -> Path:
    """One log shared by every linked worktree of the same repository, not
    one per worktree (DX-38 finding 2): resolved through the common `.git`
    dir's parent, falling back to `repo` itself when that isn't available
    (not a git repo at all, e.g. under a bare `tmp_path` in tests).

    `NIGHTSHIFT_FRICTION_LOG`, when set, overrides this resolution entirely
    (DX-73): some callers (split_guard.py, worktree_guard.py) hardcode the
    real checkout as `repo` and have no way to thread a test's `tmp_path`
    through, so a test isolates by setting this env var instead."""
    override = os.environ.get("NIGHTSHIFT_FRICTION_LOG")
    if override:
        path = Path(override)
    else:
        common_dir = _git_common_dir(repo)
        root = common_dir.parent if common_dir is not None else repo
        path = root / ".cockpit" / "friction.jsonl"
    # DX-112: a test must never write the operator's own log. The autouse
    # fixture in tests/conftest.py isolates via the env var; a test that
    # clears it fails loudly here rather than polluting silently.
    if os.environ.get("PYTEST_CURRENT_TEST") and path.resolve() == repo_friction_log_path():
        raise RuntimeError(
            f"refusing to use the repo's own friction log {path} under pytest "
            "(set NIGHTSHIFT_FRICTION_LOG to a tmp path)")
    return path


def repo_friction_log_path() -> Path:
    """The `.cockpit/friction.jsonl` of the checkout this script lives in,
    or NIGHTSHIFT_REPO_FRICTION_LOG when set: tests copy this script into
    tmp repos, where `__file__` would name the tmp repo's own log."""
    pinned = os.environ.get("NIGHTSHIFT_REPO_FRICTION_LOG")
    if pinned:
        return Path(pinned).resolve()
    here = Path(__file__).resolve().parent.parent
    common_dir = _git_common_dir(here)
    root = common_dir.parent if common_dir is not None else here
    return (root / ".cockpit" / "friction.jsonl").resolve()


def record_event(
    log_path: Path,
    signal: str,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    if signal not in SIGNALS and signal not in META_SIGNALS:
        raise ValueError(f"unknown friction signal: {signal!r}")
    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "task_id": task_id,
        "signal": signal,
        "payload": payload or {},
    }
    # Single write() call to an O_APPEND fd is atomic on a local POSIX
    # filesystem, so concurrent writers (parallel worktrees/sessions) can
    # never interleave or truncate each other's lines -- no lock file needed.
    line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def load_events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for raw_line in log_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    """`event`'s parsed, tz-aware timestamp, or None if it has none or an
    unparseable one."""
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp


def _event_age_seconds(event: dict[str, Any], now: datetime) -> float | None:
    """Seconds between `event`'s timestamp and `now`, or None if the event
    has no parseable timestamp -- such an event is never decayed (see
    DECAY_WINDOW_SECONDS)."""
    stamp = _event_timestamp(event)
    if stamp is None:
        return None
    return (now - stamp).total_seconds()


def _newest_signal_event_timestamp(
    relevant: list[dict[str, Any]],
) -> tuple[datetime | None, bool]:
    """The newest timestamp among `relevant`'s SIGNALS-taxonomy events, and
    whether any of them lacked a parseable timestamp -- in which case no
    high-water mark can be trusted (DX-85): a mark that undercounts would
    silently swallow a real trip, so the caller must treat this case as
    "always report"."""
    newest: datetime | None = None
    saw_unparseable = False
    for event in relevant:
        signal = event.get("signal")
        if signal not in SIGNALS or signal in LOGGED_ONLY_SIGNALS:
            continue
        stamp = _event_timestamp(event)
        if stamp is None:
            saw_unparseable = True
            continue
        if newest is None or stamp > newest:
            newest = stamp
    return newest, saw_unparseable


def _last_reported_through(
    relevant: list[dict[str, Any]], session_id: str | None,
) -> datetime | None:
    """The newest `summarised_through` high-water mark among this session's
    prior `stop_reported` events in `relevant`, or None if it has never
    reported (or its marks are unparseable)."""
    last: datetime | None = None
    for event in relevant:
        if event.get("signal") != "stop_reported":
            continue
        if event.get("session_id") != session_id:
            continue
        payload = event.get("payload")
        raw = payload.get("summarised_through") if isinstance(payload, dict) else None
        if not isinstance(raw, str):
            continue
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if last is None or stamp > last:
            last = stamp
    return last


def tripped_signals(
    events: list[dict[str, Any]], *, now: datetime | None = None,
) -> list[str]:
    now = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    for event in events:
        signal = event.get("signal")
        if signal not in SIGNALS or signal in LOGGED_ONLY_SIGNALS:
            continue
        if signal == "gate_failed":
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("scope") == "scoped":
                continue
        age = _event_age_seconds(event, now)
        if age is not None and age > DECAY_WINDOW_SECONDS:
            continue
        counts[signal] = counts.get(signal, 0) + 1
    return sorted(
        signal for signal, count in counts.items()
        if count >= THRESHOLDS[signal]
    )


def stop_summary(
    events: list[dict[str, Any]], session_id: str | None,
    task_id: str | None = None, *, now: datetime | None = None,
) -> str | None:
    """An event is relevant to this Stop if it carries this session's id --
    the two settings.json-native emitters (hook_blocked) always do -- or
    this repo's current task id -- what every script-invoked emitter
    (gate_failed, land_retried, edit_outside_touches,
    ci_failed_after_local_pass) carries instead, having no session id of
    their own to write (DX-38 finding 1)."""
    relevant = [
        e for e in events
        if e.get("session_id") == session_id
        or (task_id is not None and e.get("task_id") == task_id)
    ]
    tripped = tripped_signals(relevant, now=now)
    if not tripped:
        return None

    newest_ts, saw_unparseable = _newest_signal_event_timestamp(relevant)
    if not saw_unparseable and newest_ts is not None:
        last_reported = _last_reported_through(relevant, session_id)
        if last_reported is not None and newest_ts <= last_reported:
            # DX-85: this trip has already been reported to the session and
            # nothing relevant has arrived since -- stay silent so a
            # resolved refusal doesn't keep blocking every subsequent
            # turn-end for the rest of the decay window.
            return None

    prefix = "friction signals tripped: " + ", ".join(tripped)
    hints = [SIGNAL_HINTS[s] for s in tripped if s in SIGNAL_HINTS]
    if hints:
        return prefix + "\n" + "\n".join(hints)
    return prefix


def _transcript_session_ids() -> set[str]:
    """Session ids of every transcript under the Claude projects dir."""
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    projects = home / "projects"
    return {
        p.stem for p in
        list(projects.glob("*/*.jsonl")) + list(projects.glob("*/*/subagents/*.jsonl"))
    }


def purge_synthetic(log_path: Path, *, yes: bool) -> list[dict[str, Any]]:
    """Rows whose session_id matches no transcript file (DX-112); removed
    from the log only when `yes`. Rows with no session id are kept -- they
    are not provably synthetic."""
    known = _transcript_session_ids()
    events = load_events(log_path)
    synthetic = [
        e for e in events
        if isinstance(e.get("session_id"), str) and e["session_id"] not in known
    ]
    if yes and synthetic:
        drop = {id(e) for e in synthetic}
        kept = "".join(
            json.dumps(e, sort_keys=True) + "\n" for e in events if id(e) not in drop)
        tmp = log_path.with_suffix(".jsonl.tmp")
        tmp.write_text(kept, encoding="utf-8")
        os.replace(tmp, log_path)
    return synthetic


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="friction.py")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="append one friction event")
    record.add_argument("signal", choices=sorted(SIGNALS))
    record.add_argument("--session-id", default=None)
    record.add_argument("--task-id", default=None)
    record.add_argument("--payload", default=None, help="JSON object")
    record.add_argument("--repo", default=None, help="repo root (default: cwd)")

    stop = sub.add_parser("stop", help="Stop-hook: evaluate and report")
    stop.add_argument("--repo", default=None, help="repo root (default: cwd)")

    purge = sub.add_parser(
        "purge", help="report (or with --yes remove) rows from non-transcript sessions")
    purge.add_argument("--synthetic", action="store_true", required=True)
    purge.add_argument("--yes", action="store_true", help="remove the reported rows")
    purge.add_argument("--repo", default=None, help="repo root (default: cwd)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo = Path(args.repo) if args.repo else Path.cwd()

    if args.command == "purge":
        rows = purge_synthetic(friction_log_path(repo), yes=args.yes)
        by_session: dict[str, int] = {}
        for e in rows:
            by_session[e["session_id"]] = by_session.get(e["session_id"], 0) + 1
        verb = "removed" if args.yes else "would remove"
        print(f"{verb} {len(rows)} synthetic row(s)")
        for sid, n in sorted(by_session.items(), key=lambda kv: -kv[1]):
            print(f"  {sid}: {n}")
        if rows and not args.yes:
            print("re-run with --yes to remove them")
        return 0

    if args.command == "record":
        try:
            payload = json.loads(args.payload) if args.payload else {}
            record_event(
                friction_log_path(repo), args.signal,
                task_id=args.task_id, session_id=args.session_id, payload=payload,
            )
        except (OSError, ValueError):
            pass
        return 0

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_input = {}
    if isinstance(hook_input, dict) and hook_input.get("stop_hook_active"):
        # DX-59: this turn's Stop was already blocked once and the model
        # already saw the summary -- re-evaluating would just re-trip the
        # same still-undecayed events on every subsequent Stop attempt,
        # trapping the session until DECAY_WINDOW_SECONDS elapses.
        return 0
    session_id = hook_input.get("session_id") if isinstance(hook_input, dict) else None
    t_id = current_task_id(repo) or _resolve_task_id_for_session(repo, session_id)
    try:
        events = load_events(friction_log_path(repo))
        summary = stop_summary(events, session_id, t_id)
    except OSError:
        summary = None
    if summary:
        eff_session_id = session_id or t_id or "unknown"
        relevant = [
            e for e in events
            if e.get("session_id") == session_id
            or (t_id is not None and e.get("task_id") == t_id)
        ]
        try:
            tripped = tripped_signals(relevant)
            if tripped:
                retro = _load_retro_module(repo)
                already_proposed = retro.proposed_signals_for_session(repo, eff_session_id)
                unproposed_signals = [s for s in tripped if s not in already_proposed]
                if unproposed_signals:
                    # DX-109: forward the full event objects (payload dict --
                    # hook, rule -- and whatever task_id we could resolve)
                    # for the tripped-but-unproposed signals, not just their
                    # bare names, so retro.py can render a richer provenance
                    # breakdown than "gate_failed, hook_blocked".
                    unproposed_events = [
                        e if e.get("task_id") or not t_id else {**e, "task_id": t_id}
                        for e in relevant
                        if e.get("signal") in unproposed_signals
                    ]
                    res = retro.propose(repo, session_id=eff_session_id, events=unproposed_events)
                    proposal_id = res.task_id
                else:
                    proposal_id = retro.find_proposal_task_id(repo, eff_session_id)
                if proposal_id:
                    summary = f"{summary}\nAuto-appended retro proposal task: {proposal_id}"
        except Exception:
            pass

        try:
            newest_ts, saw_unparseable = _newest_signal_event_timestamp(relevant)
            mark_payload = {
                "summarised_through": (
                    None if saw_unparseable or newest_ts is None
                    else newest_ts.isoformat()
                ),
            }
            record_event(
                friction_log_path(repo), "stop_reported",
                task_id=t_id, session_id=session_id, payload=mark_payload,
            )
        except (OSError, ValueError):
            pass

        # Plain stdout on exit 0 from a Stop hook is transcript/debug-log
        # only and never reaches the model; exit 2's stderr is the
        # documented channel that does (DX-38 finding 3).
        print(summary, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
