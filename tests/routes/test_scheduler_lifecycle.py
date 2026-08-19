"""N4: the scheduler started by create_app must shut down on app shutdown,
not leak a background thread per create_app/TestClient cycle."""

import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402


def _app(db_path: Path):
    return create_app(
        db_path=db_path,
        config_path=ROOT / "rules_config.toml",
        reference=(None, "task R2 outstanding"),
        start_scheduler=True,
    )


def test_scheduler_shuts_down_on_app_shutdown() -> None:
    tmp_dir = Path(tempfile.mkdtemp())
    app = _app(tmp_dir / "a.db")
    scheduler = app.state.scheduler
    assert scheduler.running is True

    with TestClient(app):
        assert scheduler.running is True

    assert scheduler.running is False


def test_no_scheduler_thread_survives_repeated_create_app_shutdown_cycles() -> None:
    tmp_dir = Path(tempfile.mkdtemp())
    initial_threads = threading.active_count()

    for i in range(5):
        app = _app(tmp_dir / f"a{i}.db")
        with TestClient(app):
            pass

    assert threading.active_count() == initial_threads
