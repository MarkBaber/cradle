-- Stool consistency vocabulary (task U17): how the stool looked, alongside the
-- colour 0001 already stores. Descriptive only - no alert rule reads it.
-- 0002 belongs to P3 and is unrelated; the runner tracks applied migrations by
-- filename rather than by ordinal, so a later-landing 0002 still applies once.
-- Do not renumber.

-- One statement on purpose: executescript runs in autocommit, so a multi-
-- statement file that fails part-way leaves the earlier DDL applied with no
-- schema_version row. NOT NULL DEFAULT backfills every pre-existing nappy row
-- with 'unset', which is what StoolConsistency.UNSET reads back as. No CHECK:
-- ALTER TABLE cannot add one in SQLite, and nappy.stool_colour has none either.
ALTER TABLE nappy ADD COLUMN consistency TEXT NOT NULL DEFAULT 'unset';
