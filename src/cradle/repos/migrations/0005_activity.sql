-- Developmental-activity domain (task M2): tummy time, reading & talking,
-- sensory play, foreign language - logged in minutes rather than volume or
-- count. Applies on top of 0001_init.sql. 0002 belongs to P3 and is unrelated;
-- the runner tracks applied migrations by filename rather than by ordinal, so a
-- later-landing 0002 still applies exactly once. Do not renumber.

-- Best-practice guidance per category is display copy in rules_config.toml's
-- [activity_targets] table, not a threshold: no alert rule reads this table.
-- duration_min is nullable so the session can be logged as it starts and the
-- minutes filled in afterwards, the same two-tap shape as expression.
CREATE TABLE activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL
    CHECK (category IN ('tummy_time','reading_talking','sensory_play','foreign_language')),
  duration_min INTEGER, note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_activity_ts ON activity(ts);
