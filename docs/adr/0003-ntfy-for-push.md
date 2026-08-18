# 0003. ntfy for push

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Notifications need to reach the household with minimal setup and no external
bot infrastructure.

## Decision

Use **ntfy** for push notifications.

## Rejected alternatives

- **Telegram bot** — rejected because: ntfy needs no bot token/registration;
  it's one HTTP POST; it's self-hostable later; the household subscribes to
  one private topic.

## Consequences

Trade-off accepted: the topic name is the only secret — acceptable on the
stated threat model (LAN + ntfy.sh topic entropy).

Source: docs/SPEC.md §4, decision D3.
