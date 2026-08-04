"""N1: ntfy adapter contract and failure containment."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.models import AlertSeverity, Finding  # noqa: E402
from cradle.ports.notifier import ConsoleNotifier, NtfyNotifier  # noqa: E402


def _finding(severity: AlertSeverity = AlertSeverity.RED) -> Finding:
    return Finding(rule_id="FEVER_U3M", severity=severity, message="Temperature 38.6 C",
                   fingerprint="FEVER_U3M:7", ts=datetime(2026, 7, 15, tzinfo=UTC))


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, content: bytes = b"", headers=None, timeout=None):
        self.calls.append((url, content, headers or {}))
        return None


def test_posts_to_server_and_topic() -> None:
    rec = Recorder()
    NtfyNotifier("https://ntfy.sh/", "secret-topic", rec).send(_finding())
    (url, content, headers) = rec.calls[0]
    assert url == "https://ntfy.sh/secret-topic"
    assert content == b"Temperature 38.6 C"
    assert headers["Priority"] == "5"
    assert "please check now" in headers["Title"]


def test_priority_scales_with_severity() -> None:
    expected = {AlertSeverity.INFO: "2", AlertSeverity.REMINDER: "3",
                AlertSeverity.AMBER: "4", AlertSeverity.RED: "5"}
    for severity, priority in expected.items():
        rec = Recorder()
        NtfyNotifier("https://ntfy.sh", "t", rec).send(_finding(severity))
        assert rec.calls[0][2]["Priority"] == priority


def test_unconfigured_topic_does_not_post() -> None:
    rec = Recorder()
    n = NtfyNotifier("https://ntfy.sh", "  ", rec)
    assert not n.configured
    n.send(_finding())
    assert rec.calls == []


def test_delivery_failure_is_swallowed() -> None:
    def boom(*args, **kwargs):
        raise ConnectionError("pi is offline")

    NtfyNotifier("https://ntfy.sh", "t", boom).send(_finding())  # must not raise


def test_console_notifier_records_for_tests() -> None:
    c = ConsoleNotifier()
    c.send(_finding())
    assert len(c.sent) == 1
