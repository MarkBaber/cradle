"""N3: ntfy topic editable from /settings, and test-send reaches it."""

import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.ports.notifier import NtfyNotifier  # noqa: E402


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, content: bytes = b"", headers=None, timeout=None):
        self.calls.append((url, content, headers or {}))
        return None


def _config_copy(tmp_path: Path, topic: str = "") -> Path:
    dest = tmp_path / "rules_config.toml"
    shutil.copy(ROOT / "rules_config.toml", dest)
    text = dest.read_text().replace('topic = ""', f'topic = "{topic}"')
    dest.write_text(text)
    return dest


def _client(config_path: Path, notifier=None) -> TestClient:
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "a.db",
        notifier=notifier,
        config_path=config_path,
        reference=(None, "task R2 outstanding"),
        start_scheduler=False,
    )
    return TestClient(app, follow_redirects=False)


def test_get_ntfy_topic_reflects_config(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path, topic="household-a1b2")
    client = _client(config_path)
    assert client.get("/api/settings/ntfy").json() == {"topic": "household-a1b2"}


def test_post_ntfy_topic_persists_and_is_reflected(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path, topic="")
    client = _client(config_path)

    r = client.post(
        "/api/settings/ntfy", data={"topic": "new-topic"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200

    assert client.get("/api/settings/ntfy").json() == {"topic": "new-topic"}
    parsed = tomllib.loads(config_path.read_text())
    assert parsed["ntfy"]["topic"] == "new-topic"
    assert parsed["weight"]["loss_red_fraction"] == 0.10


def test_post_ntfy_topic_without_htmx_redirects_to_settings(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path, topic="")
    client = _client(config_path)
    r = client.post("/api/settings/ntfy", data={"topic": "new-topic"})
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"


def test_post_ntfy_topic_rejects_invalid_characters(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path, topic="old-topic")
    client = _client(config_path)

    r = client.post(
        "/api/settings/ntfy", data={"topic": "bad topic!"}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 400
    assert client.get("/api/settings/ntfy").json() == {"topic": "old-topic"}


def test_post_ntfy_topic_empty_clears_topic_and_falls_back(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path, topic="old-topic")
    client = _client(config_path)

    r = client.post("/api/settings/ntfy", data={"topic": ""}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "console" in r.text.lower()
    assert client.get("/api/settings/ntfy").json() == {"topic": ""}


def test_test_send_button_reaches_the_configured_topic(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path, topic="household-a1b2")
    recorder = Recorder()
    notifier = NtfyNotifier("https://ntfy.sh", "household-a1b2", poster=recorder)
    client = _client(config_path, notifier=notifier)

    r = client.post("/api/settings/test-notification", headers={"HX-Request": "true"})

    assert r.status_code == 200
    assert len(recorder.calls) == 1
    url, _, _ = recorder.calls[0]
    assert url == "https://ntfy.sh/household-a1b2"
