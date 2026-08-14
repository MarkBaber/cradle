# 0008. Soft-delete + `edited_at` on all events

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Tired-parent data entry involves frequent typo corrections on logged events.

## Decision

Use soft-delete plus an `edited_at` field on all events.

## Rejected alternatives

- **Hard delete / append-only event-sourcing** — rejected because: tired-parent
  typo correction is a first-class flow; full event-sourcing is
  over-engineering here (YAGNI).

## Consequences

Corrections are modelled as edits/soft-deletes rather than as an
event-sourced append-only log.

Source: docs/SPEC.md §4, decision D8.
