# 0005. Corrected age for preterm (<37 w) in centile calcs

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Centile calculations for babies born preterm (<37 weeks) need an age basis
consistent with clinical practice.

## Decision

Use corrected age for preterm (<37 w) infants in centile calculations.

## Rejected alternatives

- **Chronological age always** — rejected because: RCPCH standard practice
  uses corrected age for preterm infants; a toggle is shown in the UI when
  applicable.

## Consequences

The UI shows a toggle when corrected age applies.

Source: docs/SPEC.md §4, decision D5.
