# CRADLE

Newborn tracking and development monitor for a household of up to four people,
running on a Raspberry Pi on your own network. Two-tap logging of feeds,
nappies and sleep; growth measurements compared against UK-WHO centiles;
reminders and out-of-range prompts pushed to phones via ntfy; a complete,
exportable history with a milestone timeline.

## Not a medical device

CRADLE logs what you tell it and points out when the log looks unusual. It does
not diagnose anything. Every alert says what the *record* shows and directs you
to your midwife, health visitor or 111 — see `docs/SPEC.md` §1.1.

Two things must happen before relying on it (both tracked in `tasks.toml`):

- **A8** — no clinician has reviewed the alert thresholds in `rules_config.toml`
  or the wording in `src/cradle/alerts/messages.py`. Have a midwife, health
  visitor or GP read both.
- **R2** — the UK-WHO reference tables are not vendored, so centiles are
  unavailable. Raw weights, weight-loss percentages and every other feature
  work regardless; the growth chart shows measurements without centile curves
  and says so.

## Install on the Pi

```bash
sudo useradd --system --home /opt/cradle --shell /usr/sbin/nologin cradle
sudo mkdir -p /opt/cradle && sudo chown cradle:cradle /opt/cradle
sudo -u cradle git clone <your-repo> /opt/cradle
cd /opt/cradle
sudo -u cradle python3 -m venv .venv
sudo -u cradle .venv/bin/pip install -e .
sudo -u cradle mkdir -p data backups

sudo cp scripts/deploy/cradle.service /etc/systemd/system/
sudo cp scripts/deploy/cradle-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cradle.service cradle-backup.timer
```

Open `http://<pi-hostname>:8134` on a phone, fill in the baby's details, then
use the browser's "Add to Home Screen" to install it as an app.

Check it came up:

```bash
systemctl status cradle
systemctl list-timers cradle-backup.timer
curl -s localhost:8134/ -o /dev/null -w '%{http_code}\n'
```

## Notifications

Set `[ntfy] topic` in `rules_config.toml` to a long random string, install the
ntfy app on each phone, and subscribe to that topic. Use **Send test
notification** in Settings to confirm.

The topic name is the only secret protecting your notifications — treat it like
a password, and prefer something like `cradle-` plus 24 random characters. To
keep everything on your own hardware, self-host ntfy and point `server` at it.

Note task **N3**: the notifier currently defaults to console output, so wire the
config through before expecting anything on a phone.

## Backups

`scripts/backup.py` writes `backups/cradle-<timestamp>.db` using `VACUUM INTO`
(a consistent copy that does not stop writers) and keeps the newest 30. The
timer runs it nightly at 03:30.

Copy backups off the Pi periodically — an SD card is not a backup strategy.
`/export/cradle.json` is the format to keep long-term: it is plain text,
complete, and restores exactly.

## Security model

No accounts and no authentication: anyone on your network who reaches the port
can read and write the log. That is a deliberate choice for a LAN-only app used
by exhausted people at 3am (decision D7). Do not port-forward it, and if you
need remote access use a VPN or Tailscale rather than exposing it.

## Development

```bash
pip install -e ".[dev]"
scripts/dev.sh            # http://localhost:8134
scripts/test.sh           # pytest, or a stdlib runner when pytest is absent
scripts/lint.sh
scripts/check_layers.sh   # architecture boundaries
```

No extra setup is needed to see htmx, Plotly or the iOS-style AnyPicker date/
time picker locally — they're committed under `src/cradle/routers/static/
vendor/` (see below) and ship with the checkout.

`docs/SPEC.md` holds the architecture and the settled decisions, `tasks.toml`
the work queue and its Definition of Done, `CLAUDE.md` the rules for agents
working in this repo.

## Vendored front-end assets

`src/cradle/routers/static/vendor/` holds `htmx.min.js`, `plotly.min.js`,
`jquery.min.js`, `anypicker.min.js` and `anypicker-all.min.css`, committed to
the repo so a fresh clone works offline with no fetch step. Every page
degrades gracefully without them (plain HTML forms, native `<input type=time>`
/ `<input type=datetime-local>`), but with them present quick-entry gets htmx
and the AnyPicker combined date+time wheel, and `/charts` gets Plotly.

To bump a pinned version, edit the version/URL/SHA-256 in
`scripts/vendor_assets.sh`, then:

```bash
scripts/vendor_assets.sh   # re-fetches and checksum-verifies each file
git add src/cradle/routers/static/vendor/
git commit -m "vendor: bump <library> to <version>"
```
