"""Notifier port: ntfy adapter (D3) plus a console fallback for dev and tests."""

import logging
from typing import Protocol

from cradle.models import AlertSeverity, Finding

log = logging.getLogger(__name__)

NTFY_PRIORITY = {
    AlertSeverity.INFO: "2",
    AlertSeverity.REMINDER: "3",
    AlertSeverity.AMBER: "4",
    AlertSeverity.RED: "5",
}
NTFY_TITLE = {
    AlertSeverity.INFO: "CRADLE",
    AlertSeverity.REMINDER: "CRADLE reminder",
    AlertSeverity.AMBER: "CRADLE - worth a look",
    AlertSeverity.RED: "CRADLE - please check now",
}
TIMEOUT_SECONDS = 10.0


class Notifier(Protocol):
    def send(self, finding: Finding) -> None: ...


class ConsoleNotifier:
    def __init__(self) -> None:
        self.sent: list[Finding] = []

    def send(self, finding: Finding) -> None:
        self.sent.append(finding)
        log.info("[%s] %s: %s", finding.severity.value.upper(),
                 finding.rule_id, finding.message)


class NullNotifier:
    def send(self, finding: Finding) -> None:
        return None


class NtfyNotifier:
    """POST a finding to ntfy (task N1).

    Never raises: a push failure must not abort the sweep or lose the finding,
    which is already persisted in alert_log by the time this is called.
    """

    def __init__(self, server: str, topic: str, poster: object | None = None) -> None:
        self._server = server.rstrip("/")
        self._topic = topic.strip()
        self._poster = poster

    @property
    def configured(self) -> bool:
        return bool(self._server and self._topic)

    @property
    def url(self) -> str:
        return f"{self._server}/{self._topic}"

    def headers(self, finding: Finding) -> dict[str, str]:
        return {
            "Title": NTFY_TITLE[finding.severity],
            "Priority": NTFY_PRIORITY[finding.severity],
            "Tags": f"baby,{finding.rule_id.lower()}",
        }

    def send(self, finding: Finding) -> None:
        if not self.configured:
            log.warning("ntfy topic not configured; dropping %s", finding.rule_id)
            return
        try:
            post = self._poster
            if post is None:
                import httpx  # noqa: PLC0415 - keep import cost off the hot path

                post = httpx.post
            post(self.url, content=finding.message.encode("utf-8"),
                 headers=self.headers(finding), timeout=TIMEOUT_SECONDS)
        except Exception:
            log.exception("ntfy delivery failed for %s", finding.rule_id)
