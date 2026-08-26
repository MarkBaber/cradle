-- Achievement/badge catalog + awards (task U42): trophy grid, rarity tiers,
-- repeatable counters, custom authoring (rule-based + manual). Applies on
-- top of 0001_init.sql; the runner tracks applied migrations by filename,
-- not ordinal (0005_activity.sql's header), so this lands as the next
-- unused number regardless of what lands between now and when it merges.
-- Do not renumber.

-- achievement_definition holds both predefined (seeded by the app at
-- startup, not user-editable) and custom (user-authored via /achievements'
-- builder form) entries in one table, distinguished by `source`. Rule
-- params (domain/field/match_value/threshold) are plain columns rather than
-- one table per rule type - the catalog stays a flat, data-driven table
-- (task notes), the same latitude U31/U34/U36's wheel catalogs already had.
CREATE TABLE achievement_definition (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  rarity TEXT NOT NULL DEFAULT 'common',
  rule_type TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT '',
  field TEXT NOT NULL DEFAULT '',
  match_value TEXT NOT NULL DEFAULT '',
  threshold INTEGER NOT NULL DEFAULT 1,
  repeatable INTEGER NOT NULL DEFAULT 0,
  icon TEXT NOT NULL DEFAULT '🏆',
  source TEXT NOT NULL DEFAULT 'predefined',
  celebrate_every TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- One row per (baby_id, badge_key), additive-only: a repeat qualifying
-- event updates count/last_awarded_at on the existing row rather than
-- inserting a second one - the same UNIQUE shape every other per-baby
-- singleton in this app uses (baby itself is one row; this is one row per
-- badge instead). Nothing here is ever deleted, decremented, or marked
-- overdue/expired (task U42 constraint, carried over unchanged from the
-- original scope).
CREATE TABLE achievement_award (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  baby_id INTEGER NOT NULL REFERENCES baby(baby_id),
  badge_key TEXT NOT NULL REFERENCES achievement_definition(key),
  count INTEGER NOT NULL DEFAULT 0,
  first_awarded_at TEXT NOT NULL,
  last_awarded_at TEXT NOT NULL,
  UNIQUE(baby_id, badge_key)
);
CREATE INDEX idx_achievement_award_baby ON achievement_award(baby_id);
