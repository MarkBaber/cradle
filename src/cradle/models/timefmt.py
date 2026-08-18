"""Timezone edges (tasks U2, C1, U9).

Lives in models because both routers (parsing datetime-local input) and
services (bucketing events into local days) need it, and services may not
import upward.

The DB stores UTC (SPEC). Browsers submit `datetime-local` values with no
offset, and a tired parent typing 09:30 means 09:30 *their* time. So naive
input is interpreted in the configured display zone and converted to UTC on
the way in, and UTC is converted back on the way out for display.

The zone is *configured*, not inferred from the server's OS setting (task
U9): a household phone in a different timezone than the Pi must not
silently shift timestamps. It is read from `[display].timezone` in
rules_config.toml (an IANA zone name), falling back to UTC if the key or
file is absent. An unrecognised zone name raises `ZoneInfoNotFoundError`
rather than falling back silently -- a typo in the config must not masquerade
as a working, if wrong, setting (SPEC R1: fail loudly rather than
approximate).
"""

import tomllib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH = Path("rules_config.toml")
DEFAULT_ZONE = "UTC"

#: (path, mtime, zone) of the last resolved config. to_utc/to_local sit on
#: series_service's per-event local-day bucketing -- a hot path -- so this
#: avoids re-opening and re-parsing rules_config.toml on every call. Keyed
#: on mtime rather than cached forever so an edited file is picked up
#: without a process restart.
_cache: tuple[str, float | None, ZoneInfo] | None = None


def _display_zone() -> ZoneInfo:
    """Resolve the configured display zone, defaulting to UTC if unset."""
    global _cache
    mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
    if _cache is not None and _cache[0] == str(CONFIG_PATH) and _cache[1] == mtime:
        return _cache[2]
    name = DEFAULT_ZONE
    if mtime is not None:
        with CONFIG_PATH.open("rb") as fh:
            config = tomllib.load(fh)
        name = config.get("display", {}).get("timezone", DEFAULT_ZONE)
    zone = ZoneInfo(name)
    _cache = (str(CONFIG_PATH), mtime, zone)
    return zone


def to_utc(value: datetime) -> datetime:
    """Naive input is display-zone wall time; aware input is converted as-is."""
    if value.tzinfo is None:
        return value.replace(tzinfo=_display_zone()).astimezone(UTC)
    return value.astimezone(UTC)


def to_local(value: datetime) -> datetime:
    """UTC (or naive-assumed-UTC) -> configured display zone, for rendering."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).astimezone(_display_zone())
    return value.astimezone(_display_zone())
