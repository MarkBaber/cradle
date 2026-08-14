# 0011. Single baby, but all event tables carry `baby_id` FK to the one profile row

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

CRADLE currently supports a single baby profile, but the schema shape has
future implications for multi-child support.

## Decision

Model a single baby, but have all event tables carry a `baby_id` FK to the
one profile row.

## Rejected alternatives

- **Bare tables** (no `baby_id` FK) — rejected because: carrying the FK costs
  nothing now; it makes a future sibling a migration-free feature, without
  building multi-child UI.

## Consequences

A future sibling/second baby becomes a migration-free feature; no
multi-child UI is built now.

Source: docs/SPEC.md §4, decision D11.
