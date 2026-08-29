"""N5: WhatsApp Cloud API adapter contract and failure containment."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.ports.whatsapp import WhatsAppCloudNotifier  # noqa: E402


class Recorder:
    def __init__(self, status_code: int = 200) -> None:
        self.calls: list[tuple[str, dict[str, object], dict[str, str]]] = []
        self._status_code = status_code

    def __call__(self, url: str, json=None, headers=None, timeout=None):
        self.calls.append((url, json or {}, headers or {}))
        return _Response(self._status_code)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_posts_to_phone_number_id_and_chat_id() -> None:
    rec = Recorder()
    ok = WhatsAppCloudNotifier("secret-token", "1234567890", "447700900000", rec).send(
        "22:00 30ml bottle expressed milk"
    )
    assert ok is True
    (url, body, headers) = rec.calls[0]
    assert url == "https://graph.facebook.com/v20.0/1234567890/messages"
    assert body["to"] == "447700900000"
    assert body["text"] == {"body": "22:00 30ml bottle expressed milk"}
    assert headers["Authorization"] == "Bearer secret-token"


def test_unconfigured_destination_does_not_post() -> None:
    rec = Recorder()
    n = WhatsAppCloudNotifier("", "1234567890", "447700900000", rec)
    assert not n.configured
    assert n.send("hello") is False
    assert rec.calls == []


def test_unconfigured_chat_id_does_not_post() -> None:
    rec = Recorder()
    n = WhatsAppCloudNotifier("token", "1234567890", "  ", rec)
    assert not n.configured
    assert n.send("hello") is False
    assert rec.calls == []


def test_delivery_failure_is_swallowed() -> None:
    def boom(*args, **kwargs):
        raise ConnectionError("pi is offline")

    ok = WhatsAppCloudNotifier("token", "1234567890", "447700900000", boom).send("hello")
    assert ok is False  # must not raise


def test_non_2xx_response_is_treated_as_failure() -> None:
    rec = Recorder(status_code=401)
    ok = WhatsAppCloudNotifier("bad-token", "1234567890", "447700900000", rec).send("hello")
    assert ok is False
    assert len(rec.calls) == 1  # the call was made; only the outcome is a failure
