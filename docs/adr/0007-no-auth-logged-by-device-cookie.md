# 0007. No auth; `logged_by` device cookie

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

CRADLE operates on a LAN-only threat model within a household.

## Decision

Use no authentication; attribute entries via a `logged_by` device cookie.

## Rejected alternatives

- **PIN/accounts** — rejected because: LAN-only threat model; attribution
  matters, security theatre doesn't. Revisit only if exposed.

## Consequences

This decision should be revisited only if the deployment is exposed beyond
the LAN.

Source: docs/SPEC.md §4, decision D7.
