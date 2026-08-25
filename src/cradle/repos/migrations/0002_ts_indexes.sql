-- ts indexes for temperature, milestone and note (task P3): feed, nappy,
-- sleep and growth already have one from 0001_init.sql; these three were
-- left out. Newborn data volumes make this a nicety, not urgent - see P3
-- notes in tasks.toml.

CREATE INDEX idx_temperature_ts ON temperature(ts);
CREATE INDEX idx_milestone_ts ON milestone(ts);
CREATE INDEX idx_note_ts ON note(ts);
