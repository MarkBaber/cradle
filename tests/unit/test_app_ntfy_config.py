"""N3: ntfy notifier wired from [ntfy] in rules_config.toml, not hard-coded."""

import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cradle.app import _write_ntfy_topic, notifier_from_config  # noqa: E402
from cradle.ports.notifier import ConsoleNotifier, NtfyNotifier  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG = ROOT / "rules_config.toml"

CONFIG_TEMPLATE = """\
[weight]
loss_red_fraction = 0.10
regain_by_day = 14
centile_cross_z = 1.33

[ntfy]
server = "https://ntfy.sh"
topic = "{topic}"                      # set in /settings; shared-secret topic (SPEC 8)
"""


def _write_config(tmp_path: Path, topic: str) -> Path:
    path = tmp_path / "rules_config.toml"
    path.write_text(CONFIG_TEMPLATE.format(topic=topic))
    return path


def test_configured_topic_produces_a_real_ntfy_notifier(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "household-a1b2")
    notifier = notifier_from_config(path)
    assert isinstance(notifier, NtfyNotifier)
    assert notifier.configured
    assert notifier.url == "https://ntfy.sh/household-a1b2"


def test_empty_topic_falls_back_to_console_without_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "")
    notifier = notifier_from_config(path)
    assert isinstance(notifier, ConsoleNotifier)


def test_missing_config_file_falls_back_to_console_without_error(tmp_path: Path) -> None:
    notifier = notifier_from_config(tmp_path / "does-not-exist.toml")
    assert isinstance(notifier, ConsoleNotifier)


def test_real_shipped_config_has_empty_topic_and_falls_back(tmp_path: Path) -> None:
    """The committed rules_config.toml ships with no topic set (task N3 notes)."""
    notifier = notifier_from_config(REAL_CONFIG)
    assert isinstance(notifier, ConsoleNotifier)


def test_write_ntfy_topic_only_changes_the_ntfy_table(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "old-topic")
    before = tomllib.loads(path.read_text())

    _write_ntfy_topic(path, "new-topic")

    after = tomllib.loads(path.read_text())
    assert after["ntfy"]["topic"] == "new-topic"
    assert after["ntfy"]["server"] == before["ntfy"]["server"]
    assert after["weight"] == before["weight"]


def test_write_ntfy_topic_preserves_trailing_comment(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "old-topic")
    _write_ntfy_topic(path, "new-topic")
    assert "# set in /settings; shared-secret topic (SPEC 8)" in path.read_text()


def test_write_ntfy_topic_cannot_inject_new_keys(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "old-topic")
    payload = '"\n[weight]\nloss_red_fraction = 0.0\n#'

    _write_ntfy_topic(path, payload)

    parsed = tomllib.loads(path.read_text())
    assert parsed["ntfy"]["topic"] == payload
    assert parsed["weight"]["loss_red_fraction"] == 0.10
