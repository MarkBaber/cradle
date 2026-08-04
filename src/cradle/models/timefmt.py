"""Timezone edges (tasks U2, C1).

Lives in models because both routers (parsing datetime-local input) and
services (bucketing events into local days) need it, and services may not
import upward.

The DB stores UTC (SPEC). Browsers submit `datetime-local` values with no
offset, and a tired parent typing 09:30 means 09:30 *their* time. So naive
input is interpreted in the server's local zone and converted to UTC on the
way in, and UTC is converted back on the way out for display.
"""

from datetime import UTC, datetime


def to_utc(value: datetime) -> datetime:
    """Naive input is server-local wall time; aware input is converted as-is."""
    if value.tzinfo is None:
        return value.astimezone().astimezone(UTC)
    return value.astimezone(UTC)


def to_local(value: datetime) -> datetime:
    """UTC (or naive-assumed-UTC) -> server-local, for rendering."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).astimezone()
    return value.astimezone()
