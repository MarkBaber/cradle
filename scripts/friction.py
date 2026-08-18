#!/usr/bin/env python3
"""Friction-signal capture and the deterministic Stop-hook trigger (DX-13).

Implements the taxonomy fixed by
docs/adr/0014-friction-signals-gate-the-retrospective.md: an append-only
event log at .cockpit/friction.jsonl, and a threshold check with no model in
the loop. Counted signals (gate_failed, hook_blocked, land_retried) need a
second occurrence to be friction -- the first is normal iteration.
Single-occurrence signals (turn_cap_reached, edit_outside_touches,
ci_failed_after_local_pass) are friction on their own.

    python3 scripts/friction.py record <signal> [--session-id ID] \
        [--task-id ID] [--payload JSON] [--repo PATH]
    python3 scripts/friction.py stop [--repo PATH]   # reads hook JSON on stdin

Stdlib only. `record` and `stop` always exit 0: a friction-recording failure
must never break the hook that calls it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COUNTED_SIGNALS = frozenset({"gate_failed", "hook_blocked", "land_retried"})
SINGLE_OCCURRENCE_SIGNALS = frozenset({
    "turn_cap_reached", "edit_outside_touches", "ci_failed_after_local_pass",
})
SIGNALS = COUNTED_SIGNALS | SINGLE_OCCURRENCE_SIGNALS

THRESHOLDS: dict[str, int] = {
    **{signal: 2 for signal in COUNTED_SIGNALS},
    **{signal: 1 for signal in SINGLE_OCCURRENCE_SIGNALS},
}


def friction_log_path(repo: Path) -> Path:
    return repo / ".cockpit" / "friction.jsonl"


def record_event(
    log_path: Path,
    signal: str,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    if signal not in SIGNALS:
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


def tripped_signals(events: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for event in events:
        signal = event.get("signal")
        if signal in SIGNALS:
            counts[signal] = counts.get(signal, 0) + 1
    return sorted(
        signal for signal, count in counts.items()
        if count >= THRESHOLDS[signal]
    )


def stop_summary(
    events: list[dict[str, Any]], session_id: str | None,
) -> str | None:
    relevant = [e for e in events if e.get("session_id") == session_id]
    tripped = tripped_signals(relevant)
    if not tripped:
        return None
    return "friction signals tripped: " + ", ".join(tripped)


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo = Path(args.repo) if args.repo else Path.cwd()

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
    session_id = hook_input.get("session_id") if isinstance(hook_input, dict) else None
    try:
        events = load_events(friction_log_path(repo))
        summary = stop_summary(events, session_id)
    except OSError:
        summary = None
    if summary:
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
