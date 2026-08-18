# 0001. FastAPI + HTMX + Alpine, no build step

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

CRADLE's UI stack must fit the house pattern and target hardware (Pi-friendly),
and the target interaction is a tired-parent UI, not a rich app-state client.

## Decision

Use FastAPI + HTMX + Alpine, with no build step.

## Rejected alternatives

- **React/Vite SPA** — rejected because: house pattern (ATRIUM/PRISM) favours
  server-rendered fragments; Pi-friendly; the tired-parent UI is server-rendered
  fragments, not app state.

## Consequences

Server-rendered fragments become the default UI unit; no frontend build tooling
is introduced or maintained.

Source: docs/SPEC.md §4, decision D1.
