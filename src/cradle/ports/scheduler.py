"""Background scheduling (task N2, decision D10).

APScheduler runs in-process so the Pi has a single systemd unit to manage. The
sweep is wrapped so a failure logs and the schedule survives: a crashed job
must not leave the household with silent alerts.
"""

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

SWEEP_MINUTES = 5


def guarded(job: Callable[[], object], name: str) -> Callable[[], None]:
    def run() -> None:
        try:
            job()
        except Exception:
            log.exception("scheduled job %s failed", name)

    return run


def build_scheduler(alert_sweep: Callable[[], object], interval_minutes: int = SWEEP_MINUTES):  # noqa: ANN201
    """Return a started BackgroundScheduler running the alert sweep."""
    from apscheduler.schedulers.background import BackgroundScheduler  # noqa: PLC0415

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        guarded(alert_sweep, "alert_sweep"), "interval",
        minutes=interval_minutes, id="alert_sweep",
        max_instances=1, coalesce=True,
    )
    scheduler.start()
    return scheduler
