# 0009. Plotly.js (vendored) for charts

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Growth charts need interactive timeseries display and centile playback via
animation frames.

## Decision

Use Plotly.js (vendored) for charts; centile playback via Plotly frames.

## Rejected alternatives

- **Custom canvas/SVG** — rejected because: Plotly gives interactive
  timeseries and built-in animation frames, and is consistent with PRISM.
- **matplotlib server-side** — rejected because: interactive timeseries and
  built-in animation frames are needed; consistent with PRISM.

## Consequences

Plotly.js is vendored rather than fetched, and centile playback is
implemented using Plotly frames.

Source: docs/SPEC.md §4, decision D9.
