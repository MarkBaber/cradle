# CRADLE — Agent Constitution

## Precedence
1. `tasks.toml` — the work queue; `exit_criteria` are the Definition of Done.
2. `docs/SPEC.md` — decisions D1–D11 are settled; **do not re-litigate**.
3. Source doc-comments.

## Workflow
- One task per branch: `task/<id>-<slug>`; squash-merge; `main` always green.
- Never start a task whose `depends` are not `done`.
- **Stay inside `touches`.** Need to change another file? Append a new task, don't grow the diff.
- Update the task's `status` in `tasks.toml` in the same diff.
- Commit format: `[<AREA>][<TASK-ID>] summary`.

## Definition of Done
Stubs replaced (no remaining `NotImplementedError("task <this-id>")`) + tests added +
`scripts/lint.sh` + `scripts/check_layers.sh` + `scripts/test.sh` green +
`tasks.toml` updated. **CI is the sole arbiter of completion.**

## Hard constraints
- Layering (SPEC §3): `routers → services → (reference|alerts|repos|ports) → models`.
  `reference`/`alerts` import models + stdlib only. Enforced by CI; don't fight it.
- Public signatures are contracts. Changing one = architect sign-off, recorded in the
  task's `notes`.
- **Medical copy** (`alerts/messages.py`) and rule thresholds' *clinical values*
  (`rules_config.toml`) are architect-only. Implement plumbing; never invent or reword
  health claims, thresholds, or escalation text.
- Dependency set is closed (SPEC §6). Adding a dep is an architect decision.
- All timestamps UTC ISO-8601; enums stored by `.value`.

## Token efficiency (first-class rules)
- Minimal diffs: never reformat untouched code; no speculative abstractions (YAGNI).
- Read only the SPEC sections and files your task `touches` — not the whole repo.
- Use `scripts/` (`test.sh`, `lint.sh`, `fmt.sh`, `check_layers.sh`) — no ad-hoc
  command exploration.
- Consume CI as digests (failing test names), never full logs.
- Blocked? Write one concise question into the task's `notes`, set status `review`, stop.
  Do not loop.

## Testing
- Unit tests live beside the layer they test (`tests/unit/`), route tests in
  `tests/routes/`, oracle parity in `tests/oracle/` (permanent — never delete vectors).
- Alert rules: every rule gets fire **and** no-fire synthetic-timeline tests with
  `FixedClock`; boundary values explicitly (37.9/38.0 °C, day 4/5, 10 %, 1.33 z).
- Tests must pass under `scripts/offline_runner.py` (stdlib) where they don't need
  fastapi/httpx; guard optional-dep tests with import-skip.
