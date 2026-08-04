-- CRADLE initial schema (SPEC 5.3). Greenfield: full v1 schema in one migration.
-- All event tables share: baby_id FK, ts (UTC ISO-8601 text), logged_by,
-- created_at, edited_at, deleted_at (soft delete, D8).

CREATE TABLE baby (
  baby_id        INTEGER PRIMARY KEY CHECK (baby_id = 1),  -- single baby (D11: FK kept)
  name           TEXT NOT NULL,
  sex            TEXT NOT NULL CHECK (sex IN ('male','female')),
  dob            TEXT NOT NULL,
  due_date       TEXT NOT NULL,
  birth_weight_g INTEGER NOT NULL
);

CREATE TABLE feed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  method TEXT NOT NULL,
  duration_min INTEGER, volume_ml INTEGER, note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_feed_ts ON feed(ts);

CREATE TABLE nappy (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL, stool_colour TEXT NOT NULL DEFAULT 'unset',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_nappy_ts ON nappy(ts);

CREATE TABLE sleep (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, ts_end TEXT, logged_by TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT 'cot',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_sleep_ts ON sleep(ts);

CREATE TABLE growth (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  measure TEXT NOT NULL, value INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'home',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);
CREATE INDEX idx_growth_ts ON growth(measure, ts);

CREATE TABLE temperature (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  temp_c REAL NOT NULL, site TEXT NOT NULL DEFAULT 'axilla',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);

CREATE TABLE milestone (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL, title TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);

CREATE TABLE note (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  ts TEXT NOT NULL, logged_by TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, edited_at TEXT, deleted_at TEXT
);

CREATE TABLE alert_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL UNIQUE,
  rule_id TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL,
  ts TEXT NOT NULL, acknowledged_at TEXT
);
