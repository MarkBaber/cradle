-- Baby journal (task U44): freeform stories and temperament observations
-- ("giggly", "clingy", "curious" ... ordinary UX text, not a clinical
-- vocabulary - CLAUDE.md's medical-copy carve-out does not apply here), with
-- photos attached. journal follows the same event shape as every other
-- domain table (0001_init.sql: id, baby_id, ts, logged_by, <columns>,
-- created_at, edited_at, deleted_at) so it slots into EventsRepo.EDITABLE
-- and X1's export/backup machinery unchanged.
CREATE TABLE journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '', story TEXT NOT NULL DEFAULT '',
  temperament TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_journal_ts ON journal(ts);

-- journal_photo is a child table, not an event: it has no baby_id of its
-- own, only a reference to the entry it illustrates. It is deliberately
-- never added to EventsRepo.EDITABLE (task U44 notes) - embedding image
-- bytes in the JSON/CSV export would break X1's pinned CSV-header-stability
-- and round-trip contracts for every other domain sharing that code path.
-- Photos are covered for backup purposes by scripts/backup.py's whole-file
-- VACUUM INTO regardless, same as every other table.
CREATE TABLE journal_photo (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  journal_entry_id INTEGER NOT NULL REFERENCES journal(id),
  ts TEXT NOT NULL,
  content_type TEXT NOT NULL,
  caption TEXT NOT NULL DEFAULT '',
  image BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_journal_photo_entry ON journal_photo(journal_entry_id);
