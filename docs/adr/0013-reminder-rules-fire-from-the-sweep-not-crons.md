# 0013. Reminder rules fire from the 5-minute sweep, not separate cron jobs

- **Status:** Accepted
- **Date:** 2026-08-19

## Context

ADR 0010 accepted APScheduler in-process for "scheduled jobs (alert
evaluation, reminder cron entries)", and docs/SPEC.md §5.2/§7 described the
scheduler as running the 5-minute alert-evaluation sweep *and* separate cron
entries for reminder rules (e.g. `WEIGH_IN_DUE`). Task N4 found that
`ports/scheduler.build_scheduler` had only ever registered the interval
sweep job — no reminder crons exist in the code, and none had been added by
the time N4 picked this up. In practice `AlertsService.sweep()` already
evaluates every rule, including the reminder ones, on each 5-minute tick,
with `Finding.fingerprint` de-duplication (ADR/D2 area, `alert_log` UNIQUE)
already preventing repeat notifications within a bucket. The SPEC was
therefore describing a second scheduling path that both never existed in
the code and would have been redundant with the first.

## Decision

Do not add reminder cron jobs. Keep the single 5-minute interval sweep as
the only scheduled job APScheduler runs; reminder rules are evaluated as
part of that sweep like every other rule. `docs/SPEC.md` §5.2's scheduler
bullet and the phase-3 exit criteria in §7 are reworded to describe this,
and this ADR plus the §7.1 amendments-table row (task N4) record the change
per the ADR log's staleness check (`scripts/check_adr.py`).

This does not revisit ADR 0010: APScheduler in-process is unchanged, only
the number of jobs it runs.

## Rejected alternatives

- **Add the reminder cron jobs as originally specified** — rejected because
  a reminder that fires on its own cron and a reminder that fires from the
  sweep produce the same notification once fingerprint de-dup is in play;
  running two scheduling paths to reach one outcome is harder to reason
  about at 3am than running one, for no behavioural gain.

## Consequences

`build_scheduler` stays a single `add_job(..., "interval", ...)` call.
Reminder cadence is bounded by the 5-minute sweep interval rather than an
independent cron schedule — acceptable since every reminder rule already
tolerates being evaluated on a coarser tick than its own threshold.

Source: docs/SPEC.md §5.2, §7, §7.1; task N4 (`tasks.toml`).
