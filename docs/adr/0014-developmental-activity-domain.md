# 0014. Developmental-activity domain

- **Status:** Accepted
- **Date:** 2026-08-22

## Context

Task M2 (`tasks.toml`) adds a new logging domain — developmental activity, in
four categories (tummy time, reading & talking, sensory play, foreign
language) — measured in minutes rather than volume or count. It needs a closed vocabulary, an event
model, a migration, repo plumbing, and per-category best-practice guidance
text for the Log page, without turning that guidance into a clinical claim
the alerts engine acts on.

## Decision

- **`[activity_targets]` lives in `rules_config.toml` and stays inside
  CLAUDE.md's architect-only carve-out.** The file already holds two
  non-clinical tables, `[entry_defaults]` (U22) and `[projections]`
  (V3/U24), both explicitly carved *out* of the architect-only rule because
  they are UX convenience defaults. `[activity_targets]` is the inverse: the
  values *are* the health-guidance claims themselves, not a convenience
  default, so unlike those two tables it stays inside the carve-out — a
  future settings page must not make them runtime-editable.
- **The targets are display copy, not a rule.** No severity, no `Finding`,
  no `alerts/messages.py` entry, and no alert rule reads
  `activity.category`. Alerting on a missed activity (e.g. no tummy time
  today) would be a new `alerts/` rule with its own severity and copy. This
  mirrors the reasoning ADR 0012 gave for `StoolConsistency` being
  descriptive-only.
- **The `foreign_language` target is UNVERIFIED**, flagged as such in
  `rules_config.toml`. Unlike the other three targets and unlike every row
  of SPEC §2's domain table, it is not a cited NHS/NICE value — NICE
  publishes no newborn foreign-language-exposure guideline. The value shown
  is general early-language-acquisition reasoning (consistent short daily
  exposure matters more than session length at this age), marked
  `UNVERIFIED - awaiting architect confirmation` rather than accepted
  silently, and left as an open point for the architect to accept, edit or
  replace.
- **`ActivityEvent.duration_min` is nullable.** The session is logged as it
  starts and the minutes are filled in afterwards, the same two-tap shape as
  `ExpressionEvent` (task M1).
- **`activity.category` carries a `CHECK (category IN (...))` constraint**
  in `migrations/0005_activity.sql`, precedented by `milk_batch.store`/
  `milk_batch.state` in `0003_milk.sql` for freshly-created tables.
  `expression.side` has none, and U17 could not add one to
  `nappy.consistency` because SQLite's `ALTER TABLE` cannot add a CHECK
  constraint to an existing table. A fifth `ActivityCategory` member later
  therefore needs a migration, not just an enum edit.
- **Migration numbered 0005, nothing renumbered.** 0002 is reserved by
  unlanded task P3; 0003 is M1 and 0004 is U17. `Db.migrate()`
  (`src/cradle/repos/db.py`) tracks applied migrations by filename, not
  ordinal, so a later-landing 0002 still applies exactly once.
- **Column order in the `CREATE TABLE` is load-bearing.**
  `services/export_service.py` derives `DOMAINS = tuple(sorted(EDITABLE))`
  and builds its CSV header as `id, baby_id, ts, logged_by,
  *sorted(EDITABLE[domain] - {"ts"}), created_at, edited_at, deleted_at`. The
  `activity` table declares `category, duration_min, note` in sorted order
  to match, so the new domain gets CSV/JSON export for free with no
  service-layer edit.
- **`ActivityCategory`/`ActivityEvent` are imported from their submodules
  (`cradle.models.enums`, `cradle.models.events`) in `events_repo.py`, not
  from the `cradle.models` package root**, because `models/__init__.py` is
  outside M2's `touches` and CLAUDE.md says to append a task rather than
  grow the diff. Follow-up task M3 has been appended to `tasks.toml` to fold
  them into `models/__init__.py`'s `__all__`. This is exactly the situation
  ADR 0012 recorded for `StoolConsistency`, which task U21 later cleared.

## Rejected alternatives

- **A separate file for the activity-target display copy**, outside
  `rules_config.toml` — rejected because every other config table already
  lives here and the dependency set is closed (CLAUDE.md).
- **`[activity_targets]` treated like `[entry_defaults]`/`[projections]`**
  and carved out of the architect-only rule — rejected because the values
  are the health-guidance claims themselves, not UX convenience defaults.
- **NOT NULL `duration_min`** with a required duration on entry — rejected
  because it would make starting tummy time a two-field form instead of the
  two-tap shape `ExpressionEvent` established.
- **No CHECK constraint on `category`**, matching `expression.side`'s
  precedent — not the path taken; `milk_batch.store`/`state` in a
  freshly-created table is the closer precedent, and a CHECK is cheap on a
  table created fresh (unlike `nappy.consistency`, where SQLite's `ALTER
  TABLE` ruled it out).
- **Migration numbered 0002** — rejected because 0002 is reserved by task
  P3.

## Consequences

`services/history_service.py` has a hardcoded `DOMAINS` tuple —
`("feed", "nappy", "sleep", "growth", "temperature", "milestone", "note")` —
that does not include `activity` (it also excludes `expression` and
`milk_batch`, which M1 left in the same state). The new domain therefore
will not appear on `/history` until a task addresses that.

`ActivityCategory`/`ActivityEvent` are not re-exported from
`cradle.models/__init__.py`; task M3 is appended to `tasks.toml` to fold
them in.

`rules_config.toml`'s `[activity_targets].foreign_language` value is
UNVERIFIED and needs architect sign-off, edit, or replacement before it can
be treated as settled guidance copy.

Source: tasks.toml, task M2.
