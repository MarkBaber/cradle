-- WhatsApp echo audit trail (task N5): one row per message the app tried to
-- send, whether or not delivery actually succeeded. The runner tracks
-- applied migrations by filename, not ordinal (0007's header) - do not
-- renumber.

-- Recording every attempt, not only successful deliveries, is what turns
-- this table into something you can debug a failed send against (task N5
-- notes) - a row with success=0 is exactly the evidence you'd want. It also
-- backs the date-header logic: the most recent row's local_date is "the
-- last local day a message was filed under", read via last_local_date()
-- rather than a separate boolean flag.
CREATE TABLE chat_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  ts TEXT NOT NULL,           -- UTC ISO-8601 timestamp of the source event
  local_date TEXT NOT NULL,   -- YYYY-MM-DD the message was filed under (display-zone day)
  text TEXT NOT NULL,         -- the composed WhatsApp message body actually sent
  sent_at TEXT NOT NULL,      -- UTC ISO-8601 timestamp the send was attempted
  success INTEGER NOT NULL    -- 1 delivered clean (2xx), 0 failed (network error or non-2xx)
);

-- Runtime-editable WhatsApp destination (task N5's chat-id design point).
-- This task's own touches list has no room for rules_config.toml, so unlike
-- N3's ntfy topic, chat_id is not TOML-backed - it is a one-row settings
-- table, edited from /settings via app.py the same way baby_repo's one-row
-- table already is. The access token and phone_number_id needed to actually
-- send are deliberately NOT stored here or anywhere on disk (read from the
-- CRADLE_WHATSAPP_TOKEN / CRADLE_WHATSAPP_PHONE_ID environment variables
-- instead) - a WhatsApp access token is materially more sensitive than a
-- shared ntfy topic, and this app has no git-ignored secrets-file
-- convention to extend for it.
CREATE TABLE whatsapp_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  chat_id TEXT NOT NULL DEFAULT ''
);
INSERT INTO whatsapp_settings (id, chat_id) VALUES (1, '');
