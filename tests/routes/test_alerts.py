"""U6: pinned red findings and acknowledgement at the HTTP boundary."""

import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.ports.clock import FixedClock  # noqa: E402
from cradle.ports.notifier import ConsoleNotifier  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}


def _client() -> tuple[TestClient, ConsoleNotifier]:
    notifier = ConsoleNotifier()
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "a.db",
        clock=FixedClock(NOW),
        notifier=notifier,
        config_path=ROOT / "rules_config.toml",
        reference=(None, "task R2 outstanding"),
        start_scheduler=False,
    )
    client = TestClient(app, follow_redirects=False)
    client.post("/api/settings/profile", data=PROFILE)
    return client, notifier


def _sweep(client: TestClient) -> int:
    return client.app.state.services.alerts.sweep()


def test_red_finding_is_pinned_then_acknowledged() -> None:
    client, notifier = _client()
    client.post("/api/temperature", data={"temp_c": "39.0"})
    raised = client.app.state.services.alerts.sweep()
    assert raised >= 1
    assert any(f.rule_id == "FEVER_U3M" for f in notifier.sent)

    page = client.get("/").text
    assert "seek medical advice now" in page
    assert "Acknowledge" in page

    pinned = client.app.state.services.alerts.pinned()
    r = client.post(
        "/api/alerts/acknowledge",
        data={"fingerprint": pinned[0].fingerprint},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "seek medical advice now" not in client.get("/").text


def test_no_alerts_means_no_banner() -> None:
    client, _ = _client()
    for _ in range(10):
        client.post("/api/feed", data={"method": "breast_left"})
        client.post("/api/nappy", data={"kind": "wet"})
    client.app.state.services.alerts.sweep()
    assert "Acknowledge" not in client.get("/").text


def test_amber_findings_shown_without_acknowledge_button() -> None:
    client, _ = _client()
    old = (NOW - timedelta(hours=9)).isoformat()
    client.post("/api/feed", data={"method": "breast_left"})
    client.post("/api/adjust-time", data={"table": "feed", "event_id": 1, "ts": old})
    client.app.state.services.alerts.sweep()
    page = client.get("/today").text
    assert "wet nappies recorded" in page or "feeds recorded" in page


def test_non_red_findings_can_be_dismissed() -> None:
    client, _ = _client()
    old = (NOW - timedelta(hours=9)).isoformat()
    client.post("/api/feed", data={"method": "breast_left"})
    client.post("/api/adjust-time", data={"table": "feed", "event_id": 1, "ts": old})
    client.app.state.services.alerts.sweep()

    page = client.get("/today").text
    assert "Dismiss" in page
    assert "alert-time" in page

    outstanding = client.app.state.services.alerts.outstanding()
    assert len(outstanding) >= 1
    fp = outstanding[0].fingerprint

    r = client.post(
        "/api/alerts/acknowledge",
        data={"fingerprint": fp},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    page_after = client.get("/today").text
    assert "alert-nonred" not in page_after or fp not in page_after


def test_old_notifications_are_automatically_dismissed_on_pages() -> None:
    client, _ = _client()
    # Insert a finding from 7 days ago
    seven_days_ago = NOW - timedelta(days=7)
    from cradle.models import AlertSeverity, Finding

    old_finding = Finding(
        rule_id="FEED_GAP",
        severity=AlertSeverity.REMINDER,
        message="no feeds logged for 8 hours",
        fingerprint="FEED_GAP:old_test",
        ts=seven_days_ago,
    )
    client.app.state.services.alerts._alert_log.record_if_new(old_finding)

    # When visiting / or /today, the 7-day-old notification should NOT be rendered
    home_page = client.get("/").text
    assert "no feeds logged for 8 hours" not in home_page

    today_page = client.get("/today").text
    assert "no feeds logged for 8 hours" not in today_page


def test_messages_page_shows_dismissed_and_active_messages_with_timestamps() -> None:
    client, _ = _client()
    client.post("/api/temperature", data={"temp_c": "39.0"})
    client.app.state.services.alerts.sweep()

    # Active message on /messages
    page = client.get("/messages").text
    assert page.startswith("<!doctype html>")
    assert "Messages" in page
    assert "seek medical advice now" in page
    assert "msg-time" in page
    assert "15 Jul" in page
    assert "Active" in page
    assert "Dismiss" in page

    # Dismiss it
    pinned = client.app.state.services.alerts.pinned()
    client.post("/api/alerts/acknowledge", data={"fingerprint": pinned[0].fingerprint})

    # Now it is dismissed on /messages
    page_after = client.get("/messages").text
    assert "seek medical advice now" in page_after
    assert "Dismissed at" in page_after


def test_messages_redirect_before_profile() -> None:
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "p_msg.db",
        clock=FixedClock(NOW),
        config_path=ROOT / "rules_config.toml",
        reference=(None, "x"),
        start_scheduler=False,
    )
    r = TestClient(app, follow_redirects=False).get("/messages")
    assert r.status_code == 303
