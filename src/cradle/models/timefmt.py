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
file is absent.
"""

import tomllib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH = Path("rules_config.toml")
DEFAULT_ZONE = "UTC"


def _display_zone() -> ZoneInfo:
    """Resolve the configured display zone, defaulting to UTC if unset."""
    name = DEFAULT_ZONE
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as fh:
            config = tomllib.load(fh)
        name = config.get("display", {}).get("timezone", DEFAULT_ZONE)
    return ZoneInfo(name)


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
