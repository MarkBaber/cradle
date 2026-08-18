#!/usr/bin/env python3
"""OpenAPI + SSE catalogue export [AP-03] -- docs/api.md is the UI handoff contract.

    python3 scripts/export_api_docs.py            # (re)generate docs/api.md
    python3 scripts/export_api_docs.py --check    # verify docs/api.md is current, exit 1 if stale

The generated file must be sufficient on its own for a UI model that never
reads this server tree: every REST route (with request/response examples),
every SSE event type, and the auth rule. Route/schema data is pulled live
from `nightshift.api.app.create_app(...).openapi()` -- no server process, no
real secrets or config file needed (see `create_app`'s signature; `Config()`
default-constructs and the bearer token is only read per-request, never at
app-construction time). ROUTE_NOTES/EVENT_NOTES below carry the hand-written
prose (auth class, error codes, description) for each route/event; both are
checked for exact coverage against the live route table / events.py's event
list so this script fails loudly instead of silently drifting.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from nightshift.api.app import API_TOKEN_ENV, create_app  # noqa: E402
from nightshift.backlog.manager import BacklogManager  # noqa: E402
from nightshift.config import Config  # noqa: E402
from nightshift.db import Database  # noqa: E402
from nightshift.notify.base import Notifier  # noqa: E402

OUT_PATH = ROOT / "docs" / "api.md"
EVENTS_PATH = ROOT / "src" / "nightshift" / "api" / "events.py"

BEARER = "Bearer token (`Authorization: Bearer <token>`)"
LOOPBACK = "Loopback client only (no token)"

# (method, path) -> (description, auth, errors beyond the standard ones)
ROUTE_NOTES: dict[tuple[str, str], dict[str, Any]] = {
    ("get", "/api/backlog"): {
        "description": "List every task across configured projects, optionally filtered to one.",
        "auth": BEARER,
        "errors": [],
    },
    ("patch", "/api/tasks/{task_id}"): {
        "description": "Reprioritise, block, or unblock one task.",
        "auth": BEARER,
        "errors": [
            "404 -- unknown task id",
            "409 -- illegal status transition",
        ],
    },
    ("get", "/api/runs"): {
        "description": "List every run, most recent first.",
        "auth": BEARER,
        "errors": [],
    },
    ("get", "/api/runs/{run_id}"): {
        "description": "One run plus every attempt made within it.",
        "auth": BEARER,
        "errors": ["404 -- unknown run id"],
    },
    ("get", "/api/runs/{run_id}/attempts/{attempt_id}/log"): {
        "description": "Archived stderr log for one attempt, as plain text. Capped at 64KiB "
        "with a head/tail elision marker if the recorded log exceeds that size.",
        "auth": BEARER,
        "errors": [
            "404 -- no log recorded for this attempt",
            "404 -- log file missing on disk",
        ],
    },
    ("get", "/api/metrics/summary"): {
        "description": "Nightly summary (tokens, cost, outcomes) for one run.",
        "auth": BEARER,
        "errors": ["404 -- run has no recorded metrics"],
        "response_example": {
            "run_id": "a1b2c3d4e5f6", "window_id": "2026-07-29T00:05",
            "started_at": 1732882800.0, "ended_at": 1732897200.0, "halted_reason": "",
            "tasks_attempted": 4, "tasks_review": 3, "tasks_blocked": 1, "attempts": 5,
            "input_tokens": 205000, "output_tokens": 16000, "cache_read_tokens": 90000,
            "total_tokens": 221000, "cost_usd": 2.10, "duration_ms": 480000,
        },
    },
    ("get", "/api/metrics/tokens"): {
        "description": "Token usage grouped by dimension (default: model), since a unix timestamp.",
        "auth": BEARER,
        "errors": ["400 -- unknown `group` value"],
        "response_example": [
            {"model": "sonnet", "attempts": 5, "input_tokens": 205000,
             "output_tokens": 16000, "cost_usd": 2.10},
        ],
    },
    ("get", "/api/metrics/burndown"): {
        "description": "Backlog burn-down series (tasks remaining over time).",
        "auth": BEARER,
        "errors": [],
        "response_example": {
            "pending": 12, "trailing_completions": [2, 3, 3],
            "avg_completed_per_night": 2.67, "eta_nights": 4.49,
        },
    },
    ("get", "/api/metrics/efficiency"): {
        "description": "Cost/token efficiency rollup across runs.",
        "auth": BEARER,
        "errors": [],
        "response_example": {
            "total_tokens": 221000, "completed_tasks": 3, "tokens_per_completed_task": 73666.7,
            "repair_success_rate": 0.5, "window_utilisation": 0.62,
        },
    },
    ("get", "/api/events"): {
        "description": "Server-Sent Events stream of the live run (see SSE events below).",
        "auth": BEARER,
        "errors": [],
    },
    ("get", "/api/config"): {
        "description": "Read-only view of the loaded `nightshift.toml`. No write-back route.",
        "auth": BEARER,
        "errors": [],
    },
    ("post", "/api/control/{action}"): {
        "description": (
            "Control the scheduler. `action` is one of `pause`, `resume`, `abort`. "
            "`resume` wakes the scheduler; `pause`/`abort` both request shutdown at the "
            "next task boundary (the run loop never interrupts mid-verify/commit/push)."
        ),
        "auth": BEARER,
        "errors": ["404 -- unknown action (not one of pause/resume/abort)"],
    },
    ("post", "/internal/wake"): {
        "description": (
            "Wake the scheduler immediately (systemd timer target). Bearer auth does not "
            "apply here -- a local timer has no way to hold a secret -- so this route is "
            "restricted to loopback client addresses instead."
        ),
        "auth": LOOPBACK,
        "errors": ["403 -- request did not come from a loopback address"],
    },
}

# event_type -> (description, example payload)
EVENT_NOTES: dict[str, dict[str, Any]] = {
    "run_started": {
        "description": "A scheduled window's run loop began.",
        "payload": {
            "id": "a1b2c3d4e5f6", "started_at": 1732882800.0, "ended_at": None,
            "window_id": "2026-07-29T00:05", "tasks_attempted": 0,
            "tasks_review": 0, "halted_reason": "",
        },
    },
    "task_state": {
        "description": "A task's execution state changed (claimed, executing, verifying, ...).",
        "payload": {"task_id": "AP-03", "state": "executing"},
    },
    "attempt_finished": {
        "description": "One `claude -p` invocation against one task completed.",
        "payload": {
            "id": "f6e5d4c3b2a1", "run_id": "a1b2c3d4e5f6", "task_id": "AP-03",
            "model": "sonnet", "session_id": "sess-123", "turns": 9,
            "input_tokens": 41000, "output_tokens": 3200, "cache_read_tokens": 18000,
            "cost_usd": 0.42, "duration_ms": 96000, "outcome": "success", "detail": "",
        },
    },
    "run_summary": {
        "description": "A run ended (window closed, budget exhausted, or halted).",
        "payload": {
            "id": "a1b2c3d4e5f6", "started_at": 1732882800.0, "ended_at": 1732897200.0,
            "window_id": "2026-07-29T00:05", "tasks_attempted": 4,
            "tasks_review": 3, "halted_reason": "",
        },
    },
    "budget_update": {
        "description": "The window budget governor's learned ceiling or usage changed.",
        "payload": {
            "window_id": "2026-07-29T00:05", "tokens_used": 620000,
            "learned_ceiling": 900000.0, "budget_fraction": 0.8,
        },
    },
}

# Per-(model, field) example overrides -- keyed by model first because field
# names like "id"/"status" mean different things on TaskOut vs RunOut vs
# WakeResult; a plain field-name lookup would bleed "AP-03" into a run id or
# "review" (a task status) into a control-action result.
_FIELD_EXAMPLES: dict[tuple[str, str], Any] = {
    ("TaskOut", "id"): "AP-03", ("TaskOut", "status"): "review",
    ("TaskOut", "title"): "Export OpenAPI and SSE catalogue to docs/api.md",
    ("TaskOut", "description"): "Generate the endpoint table, request/response examples...",
    ("TaskOut", "depends"): ["AP-01"],
    ("TaskOut", "touches"): ["docs/api.md", "scripts/export_api_docs.py"],
    ("TaskOut", "exit_criteria"): [
        "tests/unit/test_api.py::test_auth_required_on_api_routes green"],
    ("RunOut", "id"): "a1b2c3d4e5f6", ("RunOut", "halted_reason"): "",
    ("AttemptOut", "id"): "f6e5d4c3b2a1", ("AttemptOut", "run_id"): "a1b2c3d4e5f6",
    ("AttemptOut", "detail"): "", ("AttemptOut", "outcome"): "success",
    ("ProjectOut", "name"): "nightshift", ("ProjectOut", "path"): "/srv/projects/nightshift",
    ("ProjectOut", "manifest"): "tasks.toml",
    ("ConfigOut", "windows"): ["00:05-05:00"], ("ConfigOut", "sandbox"): "worktree",
    ("ConfigOut", "db_path"): "nightshift.db", ("ConfigOut", "api_host"): "127.0.0.1",
    ("ControlResult", "status"): "ok", ("ControlResult", "action"): "resume",
    ("WakeResult", "status"): "ok",
}
# Field-name-only fallback for names that mean the same thing everywhere they occur.
_STRING_EXAMPLES: dict[str, Any] = {
    "task_id": "AP-03", "session_id": "sess-123", "routing": "impl", "notes": "",
    "project": "nightshift", "window_id": "2026-07-29T00:05", "model": "sonnet",
    "block": "waiting on design review", "verify": "./scripts/test",
    "branch_prefix": "task", "state": "executing",
}


def _example(schema_obj: dict[str, Any], components: dict[str, Any],
             field_name: str = "", model: str = "") -> Any:
    if "$ref" in schema_obj:
        model = schema_obj["$ref"].rsplit("/", 1)[-1]
        return _example(components[model], components, field_name, model)
    if "anyOf" in schema_obj:
        branch = next((o for o in schema_obj["anyOf"] if o.get("type") != "null"),
                       schema_obj["anyOf"][0])
        return _example(branch, components, field_name, model)
    if "enum" in schema_obj:
        return schema_obj["enum"][0]
    kind = schema_obj.get("type")
    if kind == "object" or "properties" in schema_obj:
        return {k: _example(v, components, k, model)
                for k, v in schema_obj.get("properties", {}).items()}
    if kind == "array":
        if (model, field_name) in _FIELD_EXAMPLES:
            return _FIELD_EXAMPLES[(model, field_name)]
        return [_example(schema_obj.get("items", {}), components, field_name, model)]
    if kind == "string":
        if (model, field_name) in _FIELD_EXAMPLES:
            return _FIELD_EXAMPLES[(model, field_name)]
        return _STRING_EXAMPLES.get(field_name, "string")
    if kind == "integer":
        return {"phase": 3, "order": 10, "turns": 9, "input_tokens": 41000,
                "output_tokens": 3200, "cache_read_tokens": 18000, "duration_ms": 96000,
                "tasks_attempted": 4, "tasks_review": 3, "safety_margin_min": 20,
                "max_consecutive_failures": 3, "backlog_low_watermark": 3,
                "api_port": 8787, "gitlab_project": 42204, "priority": 1}.get(field_name, 0)
    if kind == "number":
        return {"started_at": 1732882800.0, "ended_at": 1732897200.0, "cost_usd": 0.42,
                "budget_fraction": 0.8, "since_ts": 0.0}.get(field_name, 0.0)
    if kind == "boolean":
        return True
    return None


def _fmt_json(value: Any) -> str:
    import json
    return json.dumps(value, indent=2)


def _event_types() -> list[str]:
    text = EVENTS_PATH.read_text(encoding="utf-8")
    m = re.search(r"Events:\s*(.+?)\s*-\s*payloads", text, re.S)
    if not m:
        raise AssertionError(f"{EVENTS_PATH}: could not find 'Events: ...' list in docstring")
    return [e.strip() for e in m.group(1).replace("\n", " ").split(",")]


def _build_app() -> Any:
    class _StubScheduler:
        async def wake(self) -> None: ...
        async def shutdown(self, *, graceful: bool = True) -> None: ...

    db = Database(":memory:")
    backlog = BacklogManager(db, [])
    return create_app(Config(), db, backlog, _StubScheduler(), Notifier([]))


def generate() -> str:
    app = _build_app()
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    paths: dict[str, dict[str, Any]] = schema["paths"]

    live_routes = {(method, path) for path, ops in paths.items() for method in ops}
    if live_routes != set(ROUTE_NOTES):
        missing = live_routes - set(ROUTE_NOTES)
        extra = set(ROUTE_NOTES) - live_routes
        raise AssertionError(
            "scripts/export_api_docs.py's ROUTE_NOTES is out of sync with the live app: "
            f"missing={sorted(missing)} extra={sorted(extra)}")

    event_types = _event_types()
    if set(event_types) != set(EVENT_NOTES):
        missing = set(event_types) - set(EVENT_NOTES)
        extra = set(EVENT_NOTES) - set(event_types)
        raise AssertionError(
            "scripts/export_api_docs.py's EVENT_NOTES is out of sync with events.py: "
            f"missing={sorted(missing)} extra={sorted(extra)}")

    lines: list[str] = []
    lines.append("# NightShift API — UI handoff contract")
    lines.append("")
    lines.append(
        "Generated by `scripts/export_api_docs.py` from the live FastAPI app "
        "(`nightshift.api.app.create_app(...).openapi()`) plus the SSE event catalogue "
        "declared in `nightshift/api/events.py`. Do not hand-edit -- run "
        "`python3 scripts/export_api_docs.py` to refresh, `--check` to verify it's current. "
        "This file is the complete contract for the UI model: it never needs to read the "
        "server tree.")
    lines.append("")

    lines.append("## Auth")
    lines.append("")
    lines.append(
        f"Every `/api/*` route requires `{BEARER}`, checked with a constant-time compare "
        f"against the server's `{API_TOKEN_ENV}` environment variable. A missing or empty "
        "server token fails closed -- every request is rejected, never treated as \"no auth "
        "needed\". A missing/invalid token returns `401` with a `WWW-Authenticate: Bearer` "
        "header.")
    lines.append("")
    lines.append(
        "`POST /internal/wake` is the one exception: it takes no token at all and instead "
        "requires the request to come from a loopback client address "
        "(`127.0.0.1` / `::1` / `localhost`) -- it exists so a local systemd timer, which has "
        "no way to hold a secret, can nudge the scheduler. Non-loopback callers get `403`.")
    lines.append("")
    lines.append(
        "Every route (loopback route included) may also return `422` for a malformed query, "
        "path, or body parameter -- standard FastAPI request validation.")
    lines.append("")

    lines.append("## Endpoints")
    lines.append("")
    lines.append("| Method | Path | Auth | Description |")
    lines.append("|---|---|---|---|")
    for path, ops in paths.items():
        for method in ops:
            note = ROUTE_NOTES[(method, path)]
            lines.append(
                f"| {method.upper()} | `{path}` | {note['auth']} | {note['description']} |")
    lines.append("")

    lines.append("## Requests and responses")
    lines.append("")
    for path, ops in paths.items():
        for method, op in ops.items():
            note = ROUTE_NOTES[(method, path)]
            lines.append(f"### {method.upper()} `{path}`")
            lines.append("")
            lines.append(note["description"])
            lines.append("")
            lines.append(f"**Auth:** {note['auth']}")
            lines.append("")

            if path == "/api/events":
                lines.append(
                    "**Response:** `text/event-stream` (Server-Sent Events, via "
                    "`sse_starlette`). Each frame is one event from the catalogue below:")
                lines.append("")
                lines.append("```")
                lines.append("event: task_state")
                lines.append('data: {"task_id": "AP-03", "state": "executing"}')
                lines.append("```")
                lines.append("")
                lines.append(
                    "A new connection first replays a short backlog of recent events (so a "
                    "client that connects a moment late doesn't miss them), then streams live. "
                    "See [SSE events](#sse-events) below for the full catalogue.")
                lines.append("")
                continue

            params = op.get("parameters", [])
            if params:
                lines.append("**Parameters:**")
                lines.append("")
                lines.append("| Name | In | Required | Type |")
                lines.append("|---|---|---|---|")
                for p in params:
                    p_schema = p.get("schema", {})
                    p_type = p_schema.get("type") or (
                        "|".join(o.get("type", "?") for o in p_schema.get("anyOf", [])))
                    required = "yes" if p.get("required") else "no"
                    lines.append(f"| `{p['name']}` | {p['in']} | {required} | {p_type} |")
                lines.append("")

            body = op.get("requestBody")
            if body and (method, path) == ("patch", "/api/tasks/{task_id}"):
                # TaskPatch's three fields are three independent operations, not one
                # combined write (see patch_task in app.py) -- one combined example
                # would wrongly imply block+unblock are applied together.
                lines.append("**Request body examples** (fields are independent -- send only "
                              "the one(s) for the operation you want):")
                lines.append("")
                for label, payload in (
                    ("Reprioritise", {"priority": 5}),
                    ("Block", {"block": "waiting on design review"}),
                    ("Unblock", {"unblock": True}),
                ):
                    lines.append(f"_{label}:_")
                    lines.append("")
                    lines.append("```json")
                    lines.append(_fmt_json(payload))
                    lines.append("```")
                    lines.append("")
            elif body:
                body_schema = body["content"]["application/json"]["schema"]
                example = _example(body_schema, components)
                lines.append("**Request body example:**")
                lines.append("")
                lines.append("```json")
                lines.append(_fmt_json(example))
                lines.append("```")
                lines.append("")

            ok = op["responses"].get("200")
            if (method, path) == ("get", "/api/runs/{run_id}/attempts/{attempt_id}/log"):
                # PlainTextResponse's real content-type doesn't survive FastAPI's OpenAPI
                # export (no response_class= on the route), so the schema-driven branch
                # below would render a `null` example -- hand-write this one instead, same
                # convention as the /api/events special-case above.
                lines.append(
                    "**Response example (200):** `text/plain`, not JSON -- illustrative, "
                    "not schema-checked.")
                lines.append("")
                lines.append("```")
                lines.append("demo agent stderr output")
                lines.append("...")
                lines.append("```")
                lines.append("")
            elif ok and "content" in ok:
                if "response_example" in note:
                    lines.append(
                        "**Response example (200):** shape is not statically declared in "
                        "the API layer (plain `dict`/`list[dict]` from `nightshift.metrics`); "
                        "this example is illustrative, not schema-checked.")
                    example = note["response_example"]
                else:
                    resp_schema = ok["content"]["application/json"]["schema"]
                    example = _example(resp_schema, components)
                    lines.append("**Response example (200):**")
                lines.append("")
                lines.append("```json")
                lines.append(_fmt_json(example))
                lines.append("```")
                lines.append("")

            errors = note["errors"] + ["422 -- malformed query/path/body"]
            if (method, path) != ("post", "/internal/wake"):
                errors = ["401 -- missing/invalid bearer token", *errors]
            lines.append("**Errors:**")
            lines.append("")
            for e in errors:
                lines.append(f"- {e}")
            lines.append("")

    lines.append("## SSE events")
    lines.append("")
    lines.append(
        "`GET /api/events` streams every event below on one connection; `event:` names the "
        "type, `data:` is the JSON payload (no per-type schema is enforced in code -- payloads "
        "mirror the REST resource they describe).")
    lines.append("")
    for event_type in event_types:
        info = EVENT_NOTES[event_type]
        lines.append(f"### `{event_type}`")
        lines.append("")
        lines.append(info["description"])
        lines.append("")
        lines.append("```json")
        lines.append(_fmt_json(info["payload"]))
        lines.append("```")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify docs/api.md is current")
    args = ap.parse_args()

    try:
        content = generate()
    except AssertionError as e:
        print(f"export_api_docs INVALID:\n  - {e}")
        return 1

    if args.check:
        if not OUT_PATH.exists():
            print(f"{OUT_PATH} does not exist (run: python3 scripts/export_api_docs.py)")
            return 1
        current = OUT_PATH.read_text(encoding="utf-8")
        if current != content:
            print(f"{OUT_PATH} is stale (run: python3 scripts/export_api_docs.py)")
            return 1
        print(f"OK: {OUT_PATH} matches the live schema.")
        return 0

    OUT_PATH.write_text(content, encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
