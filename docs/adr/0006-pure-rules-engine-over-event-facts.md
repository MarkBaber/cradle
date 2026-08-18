# 0006. Pure rules engine over event "facts"

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Medical-adjacent alerting rules need to be deterministic and testable against
synthetic timelines.

## Decision

Implement alerting as a pure rules engine operating over event "facts".

## Rejected alternatives

- **Checks inline in the scheduler job** — rejected because: a pure rules
  engine gives determinism and testability; synthetic-timeline tests are the
  acceptance mechanism for every medical-adjacent rule.

## Consequences

Every medical-adjacent rule must have synthetic-timeline tests as its
acceptance mechanism.

Source: docs/SPEC.md §4, decision D6.
