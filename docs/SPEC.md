# CRADLE — Newborn Tracking & Development Monitor

**Version:** 1.0-draft · **Status:** AWAITING ARCHITECT APPROVAL · **Target:** Raspberry Pi / home server, LAN-only

---

## 1. Mission & scope

A mobile-first PWA for ≤4 household users to log a single newborn's care events and
measurements with near-zero friction, compare growth against UK-WHO reference standards,
surface out-of-range/missing-data alerts via ntfy push, and preserve a complete,
exportable history (timeseries + animated growth playback + milestones).

**Quality targets (the razor for every decision):**

| Target | Value |
|---|---|
| Quick-entry interaction cost | ≤2 taps to log a feed/nappy event at "now" with defaults accepted — tap tile, tap Save (§5.4); sleep start/end stays a single confirm-free tap |
| Page weight (quick-entry screen) | ≤150 KB transferred, no build step (vendored date/time-picker assets on the entry-panel and `/history` edit controls are exempt — see §7.1, 2026-08-21) |
| Server | Runs on Pi 4/5, ≤150 MB RSS, SQLite only, zero runtime network deps except ntfy POST |
| Centile accuracy | z-score/centile matches published UK-WHO LMS reference to ±0.01 z (oracle-tested) |
| Data safety | WAL + nightly `VACUUM INTO` backup; export to CSV/JSON at any time |
| Alert latency | Missing-data / out-of-range checks evaluated ≤5 min after trigger condition |

**v1 IS:** single baby; feeds, nappies, sleep, growth, temperature, milestones, notes;
expressed-milk sessions and colour-coded bottle stock (phase 6, §5.3);
UK-WHO centiles with gestational-age correction; rule-based alerts + reminders via ntfy;
timeseries dashboards + animated centile playback; CSV/JSON export; no auth (LAN trust),
device-name attribution.

**v1 IS NOT:** multi-child, accounts/auth, photo storage, internet exposure, native app,
medical-device functionality (see §1.1), cloud sync.

### 1.1 Safety framing (non-negotiable)

CRADLE is a logging and awareness tool, **not** a medical device and never diagnoses.
Every out-of-range alert links to its NHS/NICE source and uses the fixed phrasing
*"discuss with your midwife/health visitor, or call 111"* (fever ≥38 °C under 3 months:
*"seek medical advice now — call 111 or GP"*). Alert copy lives in one reviewed module
(`alerts/messages.py`); agents must not author new medical claims.

---

## 2. What is tracked, and why (best-practice basis)

Grounded in NHS newborn guidance, NICE CG37 (postnatal care) / NG194, and RCPCH UK-WHO
growth chart practice. Each domain has an evidence-based *expected pattern* that powers
the alert rules in §6.

| Domain | Fields | Why it's key |
|---|---|---|
| **Feeds** | ts_start, method (breast-L/breast-R/bottle-expressed/bottle-formula), duration_min (breast), volume_ml (bottle), note | Feed frequency/intake is the primary early-days health signal: 8–12 feeds/24 h expected in weeks 0–4 |
| **Nappies** | ts, kind (wet/dirty/mixed), stool_colour (enum incl. amber-flag colours) | Output is the proxy for intake: day-of-life-dependent wet/dirty counts are the standard NHS hydration check |
| **Sleep** | ts_start, ts_end (nullable while running), location (cot/pram/arms/other) | Sleep-cycle consolidation trend; total/24 h context for feeding and development |
| **Growth** | ts, weight_g, length_mm (optional), head_circ_mm (optional), source (midwife/home) | Centile tracking; weight-loss %, regain-by-day-14, centile-channel crossing are the core NICE escalation signals |
| **Temperature** | ts, temp_c, site | ≥38 °C under 3 months is a red-flag threshold (NICE NG143 traffic-light) |
| **Milestones** | ts, category (motor/social/communication/first), title, note | The "look back in future" record: first smile, rolls, sits, etc.; keyed to typical age windows for context, never scored |
| **Notes/Observations** | ts, text, tags | Jaundice observations, vitamin-D drops given, medication, anything unstructured |
| **Activities** | ts, category (tummy_time/reading_talking/sensory_play/foreign_language), duration_min, note | Developmental time, logged in minutes rather than volume or count. Per-category best-practice guidance is display copy in `rules_config.toml`'s `[activity_targets]` (task M2) — shown to a parent for context, never scored and read by no alert rule |

**Baby profile** (single row): name, sex, dob, due_date (→ gestational age; centiles use
*corrected* age when born <37 weeks), birth_weight_g.

---

## 3. Architecture

```
                    browser (PWA, HTMX + Alpine, Plotly)
                                  │
  ┌───────────────────────────────▼───────────────────────────────┐
  │ routers/      thin HTTP: pages + HTMX fragments + JSON export │
  ├───────────────────────────────────────────────────────────────┤
  │ services/     use-cases: log_event, growth_assessment,        │
  │               dashboard_series, alert_evaluation, export      │
  ├──────────────┬───────────────────────────┬────────────────────┤
  │ reference/   │ alerts/                   │ repos/             │
  │ UK-WHO LMS   │ pure rules engine         │ SQLite (WAL)       │
  │ tables +     │ (facts in → findings out, │ per-domain repos   │
  │ expected-    │ no I/O, no clock reads)   │                    │
  │ pattern data │                           │                    │
  ├──────────────┴───────────────────────────┴────────────────────┤
  │ ports/        Clock, Notifier (ntfy | console), Scheduler     │
  ├───────────────────────────────────────────────────────────────┤
  │ models/       frozen dataclasses / enums; zero deps upward    │
  └───────────────────────────────────────────────────────────────┘
```

**Layering rule (machine-enforced):**
`routers → services → (reference | alerts | repos | ports) → models`.
`reference` and `alerts` are pure (import only `models` + stdlib). `repos` and port
adapters may not import `services` or `routers`. Enforced by import-linter contract +
AST fallback test.

**Key contracts:**

- `alerts.evaluate(facts: FactSet, rules: RuleSet, now: datetime) -> list[Finding]` —
  deterministic pure function. `FactSet` is assembled by the service layer from repos;
  the engine never touches DB or wall clock. This is what makes every alert rule unit-
  testable with synthetic timelines.
- `reference.zscore(measure, sex, age_days, value, *, corrected: bool) -> ZResult`
  (z, centile, LMS row used) — LMS method: `z = ((x/M)^L − 1)/(L·S)`, `L≠0`.
- `Notifier.send(finding: Finding) -> None` — ntfy adapter POSTs to a topic;
  `ConsoleNotifier` fallback keeps dev/test offline.
- Scheduler (APScheduler, in-process): every 5 min → `alert_evaluation` service
  (evaluates condition alerts and reminder rules in sweep).
- Alert de-duplication: `Finding.fingerprint` (rule id + subject + period bucket);
  a finding is notified once per fingerprint, recorded in `alert_log`.

---

## 4. Decisions & rejected alternatives (do not re-litigate)

| # | Decision | Rejected | Why |
|---|---|---|---|
| D1 | FastAPI + HTMX + Alpine, no build step | React/Vite SPA | House pattern (ATRIUM/PRISM); Pi-friendly; tired-parent UI is server-rendered fragments, not app state |
| D2 | SQLite WAL, single file | Postgres | ≤4 users, one writer effectively; trivially backed up with `VACUUM INTO` |
| D3 | **ntfy** for push | Telegram bot | No bot token/registration; one HTTP POST; self-hostable later; household subscribes to one private topic. Trade-off: topic name is the only secret — acceptable on the stated threat model (LAN + ntfy.sh topic entropy) |
| D4 | UK-WHO (RCPCH) LMS tables, vendored | WHO-only; API lookup | UK clinical practice (Red Book charts) = what the midwife/HV compares against; vendored → offline + reproducible; table version pinned in `reference/VERSION` |
| D5 | Corrected age for preterm (<37 w) in centile calcs | Chronological always | RCPCH standard practice; toggle shown in UI when applicable |
| D6 | Pure rules engine over event "facts" | Checks inline in scheduler job | Determinism + testability; synthetic-timeline tests are the acceptance mechanism for every medical-adjacent rule |
| D7 | No auth; `logged_by` device cookie | PIN/accounts | LAN-only threat model; attribution matters, security theatre doesn't. Revisit only if exposed |
| D8 | Soft-delete + `edited_at` on all events | Hard delete / append-only event-sourcing | Tired-parent typo correction is a first-class flow; full event-sourcing is over-engineering here (YAGNI) |
| D9 | Plotly.js (vendored) for charts; centile playback via Plotly frames | Custom canvas/SVG; matplotlib server-side | Interactive timeseries + built-in animation frames; consistent with PRISM |
| D10 | APScheduler in-process | systemd timers / cron | Single deployable; scheduler state visible in-app; Pi service stays one unit |
| D11 | Single baby, but all event tables carry `baby_id` FK to the one profile row | Bare tables | Costs nothing now; makes a future sibling a migration-free feature, without building multi-child UI |

---

## 5. Per-system handoff specs

### 5.1 `reference/` — UK-WHO growth engine
- Data: `reference/data/ukwho_lms.csv` — columns `measure,sex,age_days,L,M,S`, covering
  weight/length/head-circ from birth to ≥2 y (UK-WHO composition: British-1990 birth
  section for term birth values, WHO standard from 2 weeks). Provenance + version in
  `reference/VERSION`. **Task R2 vendors this data; its exit criterion includes a
  provenance note citing the RCPCH/WHO source files used.**
- Interpolation: linear in L, M, S between bracketing age rows (standard practice).
- `ZResult = (z, centile, corrected_age_days, table_version)`.
- **Oracle:** `tests/oracle/ukwho_vectors.csv` — ≥40 hand-checked vectors (published
  centile-chart spot values, both sexes, incl. preterm-corrected cases). Parity test
  asserts ±0.01 z. Oracle is permanent regression collateral.

### 5.2 `alerts/` — rules engine
Rule = frozen dataclass: `id, severity (info|reminder|amber|red), window, predicate`.
v1 rule set (each with named test in `tests/unit/test_rules_*.py`):

| Rule id | Trigger (defaults; thresholds in `rules_config.toml`) | Severity |
|---|---|---|
| `FEED_GAP` | No feed logged for >4 h (age <28 d, daytime-independent) | reminder |
| `FEED_COUNT_LOW` | <8 feeds in trailing 24 h, age <28 d | amber |
| `WET_NAPPY_LOW` | Wet count below NHS day-of-life table (d1:1, d2:2, d3:3, d4:4, d5+:6) over trailing 24 h | amber |
| `STOOL_ABSENT` | No dirty nappy >24 h in days 3–28 | amber |
| `STOOL_COLOUR` | Amber-flag colour logged (pale/chalky, red, black after d5) | red |
| `WEIGHT_LOSS_10PC` | Weight <90 % of birth weight | red |
| `WEIGHT_NOT_REGAINED` | Below birth weight at ≥14 d | amber |
| `CENTILE_CROSS` | Weight z drops ≥ (2 channel-widths ≈ 1.33 z) from established baseline | amber |
| `FEVER_U3M` | temp ≥38.0 °C and age <90 d | red |
| `WEIGH_IN_DUE` | No weight for >14 d (age <6 m) — cadence configurable | reminder |
| `MEASUREMENT_GAP` | No events of any kind for >12 h (data-entry lapse, not health) | info |

Severity → ntfy priority mapping; red findings also pinned on dashboard until
acknowledged. All thresholds live in `rules_config.toml` — code never hard-codes them.

### 5.3 `repos/` + schema
Tables: `baby`, `feed`, `nappy`, `sleep`, `growth`, `temperature`, `milestone`, `note`,
`expression`, `milk_batch`, `activity`, `alert_log`, `schema_version`. All event tables:
`id, baby_id, ts (UTC ISO-8601), logged_by, created_at, edited_at, deleted_at`. Migrations =
ordered SQL files in `repos/migrations/`, applied at startup, tracked in `schema_version`
**by filename, not ordinal** — a lower-numbered file that lands later still applies exactly
once.

`milk_batch` is one row per physical bottle rather than an event: it carries `expressed_at`,
`stored_at`, `store`, `colour`, `volume_ml`, `state`, `thawed_at`, `opened_at`, `used_at`
and an optional `expression_id` FK alongside the shared columns. `stored_at` is deliberately
separate from `expressed_at` — the storage clock the expiry rules run on starts when the
bottle goes in to cool, not when it was expressed at the cot side an hour earlier — and
`state` is stored rather than derived, because a bottle can be discarded for reasons no
timestamp shows. Colour identifies the bottle physically, so a **partial unique index**
allows at most one live batch (`stored|thawed|opened`, not soft-deleted) per colour;
otherwise an alert naming the blue bottle points at two different bottles.

`activity` is one row per developmental-activity session alongside the shared columns:
`category` (a closed `ActivityCategory` — `tummy_time`, `reading_talking`, `sensory_play`,
`foreign_language`, stored by `.value` and constrained by a `CHECK`), `duration_min` and
`note`. `duration_min` is nullable on purpose: the session is logged as it starts and the
minutes are filled in afterwards, the same two-tap shape as `expression`. The best-practice
target text each category displays lives in `rules_config.toml`'s `[activity_targets]`, one
entry per enum member. That table is **display copy, not a threshold** — it gates nothing,
carries no severity and has no `messages.py` entry — but because the values are the
health-guidance claims themselves rather than a UX convenience default, they stay inside
CLAUDE.md's architect-only carve-out for that file, unlike `[entry_defaults]` and
`[projections]`. Alerting on a missed activity would be a new `alerts/` rule with its own
severity and copy.

### 5.4 `routers/` + UI
- `/` **Quick-entry**: six oversized tiles (Feed L / Feed R / Bottle / Wet / Dirty /
  Sleep toggle). Feed and Mess tiles (Feed L / Feed R / Bottle / Wet / Dirty) open an
  htmx-swapped panel below the grid, pre-populated with sensible defaults (timestamp
  `now`, side/method matching the tile tapped, plus volume, duration, stool colour and
  consistency where relevant) — nothing is written until **Save**, which posts the
  whole event in one request. Every panel field is optional; Save with all defaults
  accepted never 4xx's. The Sleep toggle is unchanged: a single confirm-free POST at
  `now` starts or ends the active sleep — a running timer has no optional fields to
  gate on. HTMX swap on Save shows the undo-toast (10 s) and "adjust time" link, as
  before.
  **Abandoned panel:** a tile tap alone writes nothing. If the panel is opened and the
  user navigates away, backgrounds the app, or taps another tile without tapping Save,
  no row exists for that tap and none is recoverable — the event must be logged again
  from scratch. This is the deliberate cost of the panel-then-Save flow above: today a
  tap is a row; after this change a tap can be nothing, so a parent interrupted
  mid-feed can lose the entry entirely rather than leave an under-specified one.
  **Rejected alternative:** write the row on tap (the flow this section replaces) and
  refine it in place afterward via `/history` (U10). Rejected because a volume/duration
  left unset this way isn't recorded as *unset* — it is stored as *zero*, so a `bottle_ml`
  series with no data entered reads as a baby drinking nothing rather than as data not
  yet captured; and because the post-hoc edit path is the one nobody uses at 3am, so
  "refine it later" doesn't hold up in practice. U10 (soft-delete + inline edit) stays
  valid regardless — it is still how the taps that were never refined get corrected.
- `/today`: last-24 h strip (counts vs expected, active sleep timer, time-since-last-feed).
- `/charts`: Plotly timeseries per domain; growth chart draws 0.4/2/9/25/50/75/91/98/99.6
  UK-WHO centile curves with baby's trajectory overlaid; **Play** button animates the
  trajectory via Plotly frames (the "watch them grow" view).
- `/history`: filterable event table, inline edit/soft-delete.
- `/milestones`: timeline cards.
- `/export`: CSV per domain + one full JSON.
- `/settings`: baby profile, rules thresholds, ntfy topic test-send.
- PWA: manifest + minimal service worker (static-asset cache only; no offline writes in
  v1 — deliberate, see §8).

### 5.5 Operations
`scripts/`: `dev.sh` (uvicorn reload), `test.sh` (offline fallback runner), `fmt.sh`,
`lint.sh`, `check_layers.sh`, `backup.sh` (`VACUUM INTO` timestamped copy, retain 30),
`deploy/cradle.service` (systemd unit for the Pi). CI: GitLab, stages
validate → test → quality per house pattern.

---

## 6. Project structure

```
cradle/
├── docs/SPEC.md
├── CLAUDE.md  tasks.toml  rules_config.toml  .gitlab-ci.yml  pyproject.toml
├── src/cradle/
│   ├── models/        # enums + frozen dataclasses (no deps)
│   ├── reference/     # LMS engine + vendored data/ + VERSION
│   ├── alerts/        # rules.py, engine.py, messages.py (reviewed copy)
│   ├── repos/         # sqlite repos + migrations/
│   ├── ports/         # clock.py, notifier.py (ntfy/console), scheduler.py
│   ├── services/      # use-cases
│   ├── routers/       # fastapi routers + templates/ + static/ (vendored plotly, htmx, alpine)
│   └── app.py         # composition root
├── scripts/           # dev, test, fmt, lint, check_layers, backup, deploy/
└── tests/
    ├── unit/          # per-layer, incl. test_layers.py (AST fallback)
    ├── oracle/        # ukwho_vectors.csv + parity test
    └── smoke/         # walking-skeleton test
```

Dependency policy (closed set): fastapi, uvicorn, jinja2, apscheduler, httpx,
python-multipart; dev: pytest, ruff, mypy, import-linter. Plotly/HTMX/Alpine vendored
as static files. Adding a dep = architect decision.

---

## 7. Phased plan (DoD-gated)

| Ph | Deliverable | Exit criteria (abridged; full detail in tasks.toml) |
|---|---|---|
| **0** | Runnable skeleton | API importable; smoke test green: POST /api/feed → row in SQLite → appears in /history fragment; CI green at commit zero |
| **1** | Core logging + quick-entry UI | All 7 domains loggable/editable/soft-deletable; ≤2-tap entry verified by route tests; undo works; /today counts correct on synthetic day |
| **2** | Growth engine (riskiest bet) | Oracle parity ±0.01 z on ≥40 vectors incl. preterm; corrected-age logic tested; /charts growth view renders centile curves |
| **3** | Alerts + ntfy + scheduler | Every §5.2 rule has passing synthetic-timeline tests; fingerprint de-dup proven; ntfy adapter integration-tested against mock; reminders fire via 5-min sweep |
| **4** | Dashboards + animation | Timeseries per domain; animated centile playback; sleep/feed pattern views |

| **5** | Milestones, export, backup, PWA polish | Full JSON/CSV export round-trips; backup script + systemd unit verified; PWA installable; Lighthouse-style weight budget met |

Phase 2 precedes alerts because `CENTILE_CROSS`/weight rules depend on the growth
engine, and the LMS/oracle work is the highest-risk correctness bet — retire it early.

---

## 7.1 Amendments made during implementation

Recorded here so the SPEC stays ground truth rather than drifting from the code.

| Ref | Change | Why |
|---|---|---|
| A3 | `alerts.load_rules(config_path)` → `build_rules(config: Mapping)` | The alerts layer was to be pure, but reading a TOML file made that claim nominal. The service now owns the file read. |
| C1 | Timeseries moved from `growth_service` to a new `series_service` | They span every domain and have nothing to do with the LMS reference. |
| C1/U2 | `to_utc`/`to_local` moved from `routers` to `models` | Services bucket events into local days and may not import upward. |
| B1 | `backup.sh` reimplemented as `backup.py` | It shelled out to the `sqlite3` CLI, absent from minimal Raspberry Pi OS images. |
| W1 | Service worker served from `/sw.js`, not `/static/sw.js` | A worker's scope is capped by its own path, so it could only have controlled `/static/*`. |
| R1 | Added `LmsTable`, `value_at_z`, `z_for_centile`, `ReferenceDataMissingError` | Drawing centile curves needs the inverse of the z-score, and missing reference data must fail loudly rather than approximate. |
| R3 | Gestation derived from a 40-week due date, not the 37-week threshold | The original formula conflated the two and would have mis-corrected every baby by three weeks. |
| N4 | Reminder crons dropped in favour of 5-minute sweep | Reminder rules (e.g. `WEIGH_IN_DUE`) are evaluated in the 5-minute sweep with fingerprint de-duplication; separate cron jobs are redundant. |
| U29 | Quick-entry weight budget (§1) exempts the vendored date/time-picker library, `AnyPicker`, including its transitive dependency, jQuery | Mark Baber directed picking the best iOS-style combined date+time picker for the entry-panel and `/history` edit controls over trimming the choice to fit the historical 150 KB ceiling (2026-08-21). The gate itself is not removed — it still covers every other quick-entry asset (`app.css`, `entry.js`, htmx, the core tiles) exactly as before; this is a scoped carve-out of the same shape the table already gives Plotly on `/charts`. |

## 8. Deliberate-not-oversight callouts

- **No offline write queue** in the PWA: sync conflict machinery for a LAN app is poor
  ROI; the Pi is on the same Wi-Fi as the phones. Revisit only on real pain.
- **No photos** in v1: storage/backup scope creep; `note` links to external storage.
- **Alerts advise, never diagnose** (§1.1); thresholds are config, sources cited in UI.
- **No auth** (D7) — deliberate given LAN threat model.
- **ntfy topic = shared secret**: documented in README; self-host note included.
