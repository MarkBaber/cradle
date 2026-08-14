# 0002. SQLite WAL, single file

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

CRADLE serves at most a handful of household users with effectively one writer.

## Decision

Use SQLite in WAL mode, single file.

## Rejected alternatives

- **Postgres** — rejected because: ≤4 users, one writer effectively; SQLite is
  trivially backed up with `VACUUM INTO`.

## Consequences

Backups are a `VACUUM INTO` operation; no separate database server process is
run or operated.

Source: docs/SPEC.md §4, decision D2.
