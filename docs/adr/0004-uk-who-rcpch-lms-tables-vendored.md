# 0004. UK-WHO (RCPCH) LMS tables, vendored

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Growth centile calculations need a reference table that matches what UK
clinicians compare against, and must work offline and reproducibly.

## Decision

Vendor the UK-WHO (RCPCH) LMS tables.

## Rejected alternatives

- **WHO-only tables** — rejected because: UK clinical practice (Red Book
  charts) is what the midwife/HV compares against.
- **API lookup** — rejected because: vendoring gives offline operation and
  reproducibility; the table version is pinned in `reference/VERSION`.

## Consequences

Table version is pinned in `reference/VERSION`; the growth engine has no
runtime dependency on an external reference-data API.

Source: docs/SPEC.md §4, decision D4.
