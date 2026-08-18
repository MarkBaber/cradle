# 0012. Stool consistency vocabulary

- **Status:** Accepted
- **Date:** 2026-08-18

## Context

Task U17 (`tasks.toml`) is routed to the architect rather than implementation
because picking a nappy "consistency" vocabulary is a clinical claim: what a
tired parent can select at 3am, and what a future alert rule could act on.

## Decision

Add a closed `StoolConsistency` enum and wire it through storage as a plain
descriptive field, alongside the existing `stool_colour`.

- **Vocabulary.** `StoolConsistency` (`src/cradle/models/enums.py`) has 8
  members — UNSET, STICKY, SEEDY, SOFT, FORMED, RUNNY, HARD, MUCOUSY — plain
  words a sleep-deprived parent can pick and a midwife will recognise,
  ordered common-first like the sibling `StoolColour` in the same file, and
  sized to match `StoolColour` (8 including UNSET) so the U18 Dirty panel
  stays one tappable list rather than a scrolling one. MUCOUSY is included
  even though it describes a property rather than a consistency, because
  `NappyEvent` (`src/cradle/models/events.py`) has no free-text note field:
  anything absent from this enum is simply unreportable.
- **Descriptive only, no clinical flags.** Unlike `StoolColour`, whose
  red-flag members carry a `# red flag` comment, no `StoolConsistency` member
  is annotated as a flag, and no alert rule reads the field. Alert-firing
  copy lives in `alerts/messages.py`, which CLAUDE.md makes architect-only,
  so a consistency that becomes an alert condition must go through task A8.
  The member comments describe appearance only.
- **Field name `consistency`**, not `stool_consistency`. U17's
  `exit_criteria` and U18's description both name the field "consistency",
  and the downstream task (U18) was written against that signature.
- **Backfill via column default, not repo-side coalescing.**
  `src/cradle/repos/migrations/0004_stool_consistency.sql` is a single
  statement: `ALTER TABLE nappy ADD COLUMN consistency TEXT NOT NULL DEFAULT
  'unset'`.
- **Migration numbered 0004, not 0002.** 0002 is reserved by task P3 (its
  `exit_criteria` names "migration 0002" directly). `Db.migrate()`
  (`src/cradle/repos/db.py`) keys the `schema_version` table on the full
  migration filename rather than the ordinal, so a later-landing 0002 still
  applies exactly once.

## Rejected alternatives

- **`stool_consistency` field name**, for symmetry with the sibling field
  `stool_colour` — rejected because U17's `exit_criteria` and U18's
  description name the field "consistency", and public signatures are
  contracts the downstream task was written against.
- **Nullable `consistency` column plus repo-side coalescing**
  (`StoolConsistency(r["consistency"] or "unset")` in `events_repo.py`) —
  rejected because it pushes a migration concern into every read path, and
  `StoolColour`/`nappy.stool_colour` already set the precedent of a NOT NULL
  DEFAULT column (`0001_init.sql`).
- **CHECK constraint** listing the values, as `milk_batch.store`/`state` do
  in `0003_milk.sql` — rejected because SQLite's `ALTER TABLE` cannot add
  one, and `nappy.stool_colour` has none either.
- **Multi-statement migration file** — rejected because `Db.migrate()` runs
  each file with `executescript`, which executes in autocommit; a
  multi-statement file failing part-way would leave DDL applied with no
  `schema_version` row.
- **Migration numbered 0002** — rejected because 0002 is reserved by task
  P3.

## Consequences

The migration is a single-statement, NOT NULL DEFAULT `ALTER TABLE` with no
CHECK constraint, matching the precedent `stool_colour` already set. No
alert rule may read `consistency` without also adding `alerts/messages.py`
copy through task A8.

Three follow-ups are known, sit outside U17's `touches`, and are being
appended to `tasks.toml` as task U21 rather than folded into this diff:

- `StoolConsistency` is not re-exported from `cradle.models/__init__.py`, so
  `events_repo.py` imports it from `cradle.models.enums` directly, unlike
  every other enum.
- `EDITABLE["nappy"]` in `events_repo.py` does not include `consistency`, so
  the U10 post-hoc edit path cannot change it. Adding it would also
  desynchronise `_csv_header()` in `services/export_service.py` (which sorts
  the `EDITABLE` set) from the physical column order an `ALTER TABLE`
  produces (appended last).
- `LoggingService.log_nappy` takes no `consistency` argument, so `POST
  /api/nappy` cannot write one yet; task U18 needs that pass-through.

Source: tasks.toml, task U17.
