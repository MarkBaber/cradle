-- Expressed-milk domain (task M1): pumping sessions and colour-coded bottles.
-- Applies on top of 0001_init.sql. 0002 belongs to P3 and is independent of
-- this file; the runner tracks applied migrations by filename, not by ordinal,
-- so a later-landing 0002 still applies exactly once. Do not renumber either.

CREATE TABLE expression (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL,
  volume_ml INTEGER, duration_min INTEGER, note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_expression_ts ON expression(ts);

-- One row per physical bottle. stored_at is deliberately separate from
-- expressed_at: the storage clock the expiry rules run on starts when the
-- bottle goes in to cool, not when it was expressed at the cot side.
CREATE TABLE milk_batch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  expressed_at TEXT NOT NULL, stored_at TEXT NOT NULL,
  store TEXT NOT NULL CHECK (store IN ('fridge','freezer','room')),
  colour TEXT NOT NULL,
  volume_ml INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'stored'
    CHECK (state IN ('stored','thawed','opened','used','discarded')),
  thawed_at TEXT, opened_at TEXT, used_at TEXT,
  expression_id INTEGER REFERENCES expression(id),
  logged_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_milk_batch_stored_at ON milk_batch(store, stored_at);

-- Colour identifies the bottle physically, so at most one batch of a colour
-- may be live at a time - otherwise an alert naming the blue bottle points at
-- two different bottles. Live = LIVE_BATCH_STATES in models/enums.py.
CREATE UNIQUE INDEX idx_milk_batch_live_colour ON milk_batch(colour)
  WHERE deleted_at IS NULL AND state IN ('stored','thawed','opened');
