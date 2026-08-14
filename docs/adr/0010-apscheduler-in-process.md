# 0010. APScheduler in-process

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Scheduled jobs (alert evaluation, reminder cron entries) need to run as part
of the deployed service.

## Decision

Use APScheduler, in-process.

## Rejected alternatives

- **systemd timers / cron** — rejected because: a single deployable is
  preferred; scheduler state is visible in-app; the Pi service stays one
  unit.

## Consequences

Scheduler state is visible in-app; the deployment remains a single unit
rather than splitting scheduling into separate OS-level jobs.

Source: docs/SPEC.md §4, decision D10.
