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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.models import FeedMethod, NappyKind  # noqa: E402
from cradle.ports.clock import Clock, FixedClock  # noqa: E402
from cradle.ports.notifier import NtfyNotifier  # noqa: E402
from cradle.routers import api as api_module  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}


def config_value(*path: str) -> Any:
    """The out-of-the-box value at *path* in the repo's own rules_config.toml.

    A test named "reflects config out of the box" is asserting a property, not
    a constant, and the constants it used to hard-code were architect-owned
    (CLAUDE.md makes rules_config.toml's values theirs to change without any
    test's involvement). Commit 135b072 changed bottle_volume_ml 60 -> 70 and
    wheel_steps.weight 25 -> 10 and broke seven tests that had pinned the old
    numbers; reading the file is what stops the next such change from doing it
    again (task Q5).

    Deliberately parsed here rather than borrowed from api.wheel_steps() /
    api.entry_defaults(): comparing an endpoint's output to the very helper
    that endpoint calls would assert nothing about the values, only that the
    call happened. This reads the file the app reads, independently.
    """
    node: Any = tomllib.loads((ROOT / "rules_config.toml").read_text(encoding="utf-8"))
    for key in path:
        node = node[key]
    return node


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


def _client(config_path: Path, notifier=None, clock: Clock | None = None) -> TestClient:
    app = create_app(
        db_path=Path(tempfile.mkdtemp()) / "a.db",
        notifier=notifier,
        clock=clock,
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
            "bottle_volume_ml": config_value("entry_defaults", "bottle_volume_ml"),
            "breast_duration_min": config_value("entry_defaults", "breast_duration_min"),
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
        assert client.get("/api/settings/entry-defaults").json()[
            "bottle_volume_ml"
        ] == config_value("entry_defaults", "bottle_volume_ml")


def test_settings_page_shows_the_current_entry_defaults(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        page = client.get("/settings").text
        bottle = re.search(r'name="bottle_volume_ml"[^>]*>', page)
        breast = re.search(r'name="breast_duration_min"[^>]*>', page)
        bottle_default = config_value("entry_defaults", "bottle_volume_ml")
        breast_default = config_value("entry_defaults", "breast_duration_min")
        assert bottle is not None and f'value="{bottle_default}"' in bottle.group(0)
        assert breast is not None and f'value="{breast_default}"' in breast.group(0)


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
        assert volume_value(before) == str(config_value("entry_defaults", "bottle_volume_ml"))

        r = client.post(
            "/api/settings/entry-defaults",
            data={"bottle_volume_ml": "90", "breast_duration_min": "15"},
        )
        assert r.status_code == 303

        after = client.get("/?panel=feed&method=bottle_expressed").text
        assert volume_value(after) == "90"


# --------------------------------------------------------------------- U24


def _seed_projection_timeline(client: TestClient) -> None:
    """Same synthetic timeline as V3's own unit test: five 90ml bottles 3h
    apart (30ml/hour, 90ml typical) and four wet nappies 6h apart (360min
    typical gap) - so the computed values this seeds are known constants."""
    assert client.post("/api/settings/profile", data=PROFILE).status_code == 303
    logging = client.app.state.services.logging
    for h in (13, 10, 7, 4, 1):
        logging.log_feed(FeedMethod.BOTTLE_FORMULA, volume_ml=90, ts=NOW - timedelta(hours=h))
    for h in (21, 15, 9, 3):
        logging.log_nappy(NappyKind.WET, ts=NOW - timedelta(hours=h))


def test_get_projections_reflects_config_out_of_the_box(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        body = client.get("/api/settings/projections").json()
        for key in ("ml_per_hour", "typical_feed_ml", "mess_interval_min"):
            assert body[key] == {"override": 0.0, "computed": None}


def test_get_projections_computed_reflects_the_log(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path, clock=FixedClock(NOW))
        _seed_projection_timeline(client)

        body = client.get("/api/settings/projections").json()
        assert body["ml_per_hour"] == {"override": 0.0, "computed": 30.0}
        assert body["typical_feed_ml"] == {"override": 0.0, "computed": 90.0}
        assert body["mess_interval_min"] == {"override": 0.0, "computed": 360.0}


def test_post_projections_persists_and_is_reflected(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)

        r = client.post(
            "/api/settings/projections",
            data={"ml_per_hour": "95", "typical_feed_ml": "80", "mess_interval_min": "150"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200

        body = client.get("/api/settings/projections").json()
        assert body["ml_per_hour"]["override"] == 95.0
        assert body["typical_feed_ml"]["override"] == 80.0
        assert body["mess_interval_min"]["override"] == 150.0
        parsed = tomllib.loads(config_path.read_text())
        assert parsed["projections"]["ml_per_hour"] == 95
        assert parsed["projections"]["typical_feed_ml"] == 80
        assert parsed["projections"]["mess_interval_min"] == 150


def test_post_projections_leaves_other_tables_byte_identical(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        original = config_path.read_text()
        client = _client(config_path)

        r = client.post(
            "/api/settings/projections",
            data={"ml_per_hour": "95", "typical_feed_ml": "80", "mess_interval_min": "150"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200

        new_text = config_path.read_text()
        assert new_text != original
        # Byte-identical outside [projections]: strip that one section from
        # both copies and compare what remains, including the [weight]
        # clinical threshold specifically (CLAUDE.md's architect-only carve-out).
        section_re = api_module._PROJECTIONS_SECTION_RE
        assert section_re.sub("", original) == section_re.sub("", new_text)
        parsed = tomllib.loads(new_text)
        assert parsed["weight"]["loss_red_fraction"] == 0.10


def test_post_projections_without_htmx_redirects_to_settings(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        r = client.post(
            "/api/settings/projections",
            data={"ml_per_hour": "95", "typical_feed_ml": "80", "mess_interval_min": "150"},
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/settings"


def test_post_projections_rejects_non_positive_values(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        r = client.post(
            "/api/settings/projections",
            data={"ml_per_hour": "0", "typical_feed_ml": "80", "mess_interval_min": "150"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 400
        assert client.get("/api/settings/projections").json()["ml_per_hour"]["override"] == 0.0


def test_post_projections_rejects_non_numeric_values(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        r = client.post(
            "/api/settings/projections",
            data={"ml_per_hour": "fast", "typical_feed_ml": "80", "mess_interval_min": "150"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 400
        assert client.get("/api/settings/projections").json()["ml_per_hour"]["override"] == 0.0


def test_clearing_override_returns_to_the_computed_value_on_next_projection(
    tmp_path: Path,
) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path, clock=FixedClock(NOW))
        _seed_projection_timeline(client)
        projections = client.app.state.services.projections

        computed = projections.projections()
        assert computed.feed_due_at == NOW + timedelta(hours=2)
        assert computed.mess_due_at == NOW + timedelta(hours=3)

        r = client.post(
            "/api/settings/projections",
            data={"ml_per_hour": "999", "typical_feed_ml": "999", "mess_interval_min": "5"},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        overridden = projections.projections()
        assert overridden.feed_due_at != computed.feed_due_at
        assert overridden.mess_due_at != computed.mess_due_at

        r = client.post(
            "/api/settings/projections",
            data={"ml_per_hour": "", "typical_feed_ml": "", "mess_interval_min": ""},
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200
        cleared = projections.projections()
        assert cleared.feed_due_at == computed.feed_due_at
        assert cleared.mess_due_at == computed.mess_due_at


def test_settings_page_carries_the_projections_panel(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        page = client.get("/settings").text
        assert 'name="ml_per_hour"' in page
        assert 'name="typical_feed_ml"' in page
        assert 'name="mess_interval_min"' in page
        assert '"/api/settings/projections"' in page


# --------------------------------------------------------------------- U37


def test_get_wheel_steps_reflects_config_out_of_the_box(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        assert client.get("/api/settings/wheel-steps").json() == config_value("wheel_steps")


def test_post_wheel_steps_persists_and_is_reflected(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)

        data = {
            "weight": "10",
            "length": "2",
            "head_circ": "2",
            "temp_c": "0.2",
            "bottle_volume_ml": "10",
            "breast_duration_min": "10",
            "tummy_time_duration_min": "2",
            "reading_talking_duration_min": "2",
            "sensory_play_duration_min": "2",
            "foreign_language_duration_min": "2",
        }
        r = client.post(
            "/api/settings/wheel-steps",
            data=data,
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200

        res = client.get("/api/settings/wheel-steps").json()
        assert res["weight"] == 10
        assert res["temp_c"] == 0.2
        parsed = tomllib.loads(config_path.read_text())
        assert parsed["wheel_steps"]["weight"] == 10
        assert parsed["wheel_steps"]["temp_c"] == 0.2


def test_post_wheel_steps_leaves_other_tables_byte_identical(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        original = config_path.read_text()
        client = _client(config_path)

        data = {
            "weight": "10",
            "length": "2",
            "head_circ": "2",
            "temp_c": "0.2",
            "bottle_volume_ml": "10",
            "breast_duration_min": "10",
            "tummy_time_duration_min": "2",
            "reading_talking_duration_min": "2",
            "sensory_play_duration_min": "2",
            "foreign_language_duration_min": "2",
        }
        r = client.post(
            "/api/settings/wheel-steps",
            data=data,
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 200

        new_text = config_path.read_text()
        assert new_text != original
        section_re = api_module._WHEEL_STEPS_SECTION_RE
        assert section_re.sub("", original) == section_re.sub("", new_text)
        parsed = tomllib.loads(new_text)
        assert parsed["weight"]["loss_red_fraction"] == 0.10


def test_post_wheel_steps_without_htmx_redirects_to_settings(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        data = {
            "weight": "10",
            "length": "2",
            "head_circ": "2",
            "temp_c": "0.2",
            "bottle_volume_ml": "10",
            "breast_duration_min": "10",
            "tummy_time_duration_min": "2",
            "reading_talking_duration_min": "2",
            "sensory_play_duration_min": "2",
            "foreign_language_duration_min": "2",
        }
        r = client.post("/api/settings/wheel-steps", data=data)
        assert r.status_code == 303
        assert r.headers["location"] == "/settings"


def test_post_wheel_steps_rejects_non_positive_values(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        data = {
            "weight": "0",
            "length": "2",
            "head_circ": "2",
            "temp_c": "0.2",
            "bottle_volume_ml": "10",
            "breast_duration_min": "10",
            "tummy_time_duration_min": "2",
            "reading_talking_duration_min": "2",
            "sensory_play_duration_min": "2",
            "foreign_language_duration_min": "2",
        }
        r = client.post(
            "/api/settings/wheel-steps",
            data=data,
            headers={"HX-Request": "true"},
        )
        assert r.status_code == 400
        assert client.get("/api/settings/wheel-steps").json()["weight"] == config_value(
            "wheel_steps", "weight"
        )


def test_settings_page_carries_scroll_wheel_increments_section(tmp_path: Path) -> None:
    config_path = _config_copy(tmp_path)
    with _entry_defaults_config_path(config_path):
        client = _client(config_path)
        page = client.get("/settings").text
        assert "Scroll-wheel increments" in page
        assert 'name="weight"' in page
        assert 'name="length"' in page
        assert 'name="head_circ"' in page
        assert 'name="temp_c"' in page
        assert 'name="bottle_volume_ml"' in page
        assert 'name="breast_duration_min"' in page
        assert 'name="tummy_time_duration_min"' in page
        assert 'name="reading_talking_duration_min"' in page
        assert 'name="sensory_play_duration_min"' in page
        assert 'name="foreign_language_duration_min"' in page
        assert '"/api/settings/wheel-steps"' in page

