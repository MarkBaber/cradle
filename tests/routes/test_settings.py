"""N3: ntfy topic editable from /settings, and test-send reaches it.

Also U22: quick-entry smart defaults (bottle_volume_ml, breast_duration_min)
editable from /settings the same way.
"""

import re
import shutil
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.ports.notifier import NtfyNotifier  # noqa: E402
from cradle.routers import api as api_module  # noqa: E402

PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}


@contextmanager
def _entry_defaults_config_path(path: Path):
    """Point api.py's own CONFIG_PATH (task U22 - not threaded via create_app,
    see api.py's module docstring comment) at an isolated tmp copy, same
    monkeypatch convention as tests/unit/test_timefmt.py's _config_path.
    Without this, a write test would mutate the real repo rules_config.toml.
    """
    original = api_module.CONFIG_PATH
    api_module.CONFIG_PATH = path
    try:
        yield
    finally:
        api_module.CONFIG_PATH = original


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


# --------------------------------------------------------------------- U22


def test_get_entry_defaults_reflects_config_out_of_the_box(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        assert client.get("/api/settings/entry-defaults").json() == {
            "bottle_volume_ml": 60,
            "breast_duration_min": 20,
        }


def test_post_entry_defaults_persists_and_is_reflected(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)

        r = client.post(
            "/api/settings/entry-defaults",
            data={"bottle_volume_ml": "90", "breast_duration_min": "15"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200

        assert client.get("/api/settings/entry-defaults").json() == {
            "bottle_volume_ml": 90,
            "breast_duration_min": 15,
        }
        parsed = tomllib.loads(config_path.read_text())
        assert parsed["entry_defaults"]["bottle_volume_ml"] == 90
        assert parsed["entry_defaults"]["breast_duration_min"] == 15
        # scoped writer: must never touch a clinical threshold elsewhere in
        # the file (CLAUDE.md's architect-only carve-out, mirrored from N3).
        assert parsed["weight"]["loss_red_fraction"] == 0.10


def test_post_entry_defaults_without_htmx_redirects_to_settings(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        r = client.post(
            "/api/settings/entry-defaults",
            data={"bottle_volume_ml": "90", "breast_duration_min": "15"},
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/settings"


def test_post_entry_defaults_rejects_non_positive_values(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        r = client.post(
            "/api/settings/entry-defaults",
            data={"bottle_volume_ml": "0", "breast_duration_min": "15"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 400
        assert client.get("/api/settings/entry-defaults").json()["bottle_volume_ml"] == 60


def test_settings_page_shows_the_current_entry_defaults(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        page = client.get("/settings").text
        bottle = re.search(r'name="bottle_volume_ml"[^>]*>', page)
        breast = re.search(r'name="breast_duration_min"[^>]*>', page)
        assert bottle is not None and 'value="60"' in bottle.group(0)
        assert breast is not None and 'value="20"' in breast.group(0)


def test_new_entry_default_appears_on_the_next_panel_open(tmp_path: Path) -> None:
    """The value editable in /settings must be what a tile tap pre-fills next
    - not just an API round-trip (U22 exit criterion)."""
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        assert client.post("/api/settings/profile", data=PROFILE).status_code == 303

        def volume_value(page: str) -> str | None:
            match = re.search(r'name="volume_ml"[^>]*>', page)
            return re.search(r'value="(\d*)"', match.group(0)).group(1) if match else None

        before = client.get("/?panel=feed&method=bottle_expressed").text
        assert volume_value(before) == "60"

        r = client.post(
            "/api/settings/entry-defaults",
            data={"bottle_volume_ml": "90", "breast_duration_min": "15"},
        )
        assert r.status_code == 303

        after = client.get("/?panel=feed&method=bottle_expressed").text
        assert volume_value(after) == "90"
