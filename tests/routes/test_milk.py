"""U14: the Express one-tap button and the /milk stock page, proven at the
HTTP boundary.

Skipped by the offline runner when fastapi is unavailable.
"""

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.models import MilkStore  # noqa: E402
from cradle.ports.clock import FixedClock  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}


def _client(seed_profile: bool = True) -> TestClient:
    db = Path(tempfile.mkdtemp()) / "routes.db"
    app = create_app(db_path=db, clock=FixedClock(NOW), config_path=ROOT / "rules_config.toml")
    client = TestClient(app, follow_redirects=False)
    if seed_profile:
        assert client.post("/api/settings/profile", data=PROFILE).status_code == 303
    return client


def _store(
    client: TestClient, store: str = "freezer", colour: str = "blue", volume_ml: str = "90"
) -> int:
    r = client.post(
        "/api/milk/store",
        data={"store": store, "colour": colour, "volume_ml": volume_ml},
    )
    assert r.status_code == 303, r.text
    stock = client.app.state.services.milk.stock_on_hand()
    batches = stock[MilkStore(store)].batches
    return [ba.batch.batch_id for ba in batches if ba.batch.colour.value == colour][-1]


# ------------------------------------------------------------------ Express


def test_express_is_one_tap_and_volume_is_never_required() -> None:
    client = _client()
    r = client.post("/api/express", data={}, headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "Undo" in r.text


def test_express_without_htmx_redirects_and_never_4xxs() -> None:
    client = _client()
    r = client.post("/api/express", data={})
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("/?logged=expression:")


# --------------------------------------------------------- store/thaw/open/use


def test_store_thaw_open_use_end_to_end() -> None:
    client = _client()
    batch_id = _store(client, store="freezer", colour="blue", volume_ml="90")

    stock = client.app.state.services.milk.stock_on_hand()
    batch = next(
        ba.batch for ba in stock[MilkStore.FREEZER].batches if ba.batch.batch_id == batch_id
    )
    assert batch.state.value == "stored"
    assert batch.store.value == "freezer"

    r = client.post("/api/milk/thaw", data={"batch_id": batch_id})
    assert r.status_code == 303, r.text
    stock = client.app.state.services.milk.stock_on_hand()
    batch = next(
        ba.batch for ba in stock[MilkStore.FRIDGE].batches if ba.batch.batch_id == batch_id
    )
    assert batch.state.value == "thawed"
    assert batch.store.value == "fridge"

    r = client.post("/api/milk/open", data={"batch_id": batch_id})
    assert r.status_code == 303, r.text
    stock = client.app.state.services.milk.stock_on_hand()
    batch = next(
        ba.batch for ba in stock[MilkStore.FRIDGE].batches if ba.batch.batch_id == batch_id
    )
    assert batch.state.value == "opened"

    r = client.post("/api/milk/use", data={"batch_id": batch_id})
    assert r.status_code == 303, r.text
    stock = client.app.state.services.milk.stock_on_hand()
    all_ids = {ba.batch.batch_id for stock_entry in stock.values() for ba in stock_entry.batches}
    assert batch_id not in all_ids


def test_store_discard_end_to_end() -> None:
    client = _client()
    batch_id = _store(client, store="fridge", colour="green", volume_ml="60")

    r = client.post("/api/milk/discard", data={"batch_id": batch_id})
    assert r.status_code == 303, r.text
    stock = client.app.state.services.milk.stock_on_hand()
    all_ids = {ba.batch.batch_id for stock_entry in stock.values() for ba in stock_entry.batches}
    assert batch_id not in all_ids


def test_store_via_htmx_returns_toast() -> None:
    client = _client()
    r = client.post(
        "/api/milk/store",
        data={"store": "fridge", "colour": "orange", "volume_ml": "70"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200, r.text
    assert "Stored" in r.text


def test_lifecycle_actions_via_htmx_return_toast() -> None:
    client = _client()
    batch_id = _store(client, store="fridge", colour="purple", volume_ml="50")
    r = client.post("/api/milk/open", data={"batch_id": batch_id}, headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert "toast" in r.text


# ---------------------------------------------------------------- colour picker


def test_colour_picker_excludes_colours_already_live() -> None:
    client = _client()
    _store(client, store="freezer", colour="blue", volume_ml="90")
    page = client.get("/milk").text
    assert '<option value="blue">Blue</option>' not in page
    assert '<option value="red">Red</option>' in page


# ------------------------------------------------------------------ /milk view


def test_milk_page_shows_store_colour_volume_and_age() -> None:
    client = _client()
    r = client.post(
        "/api/milk/store", data={"store": "fridge", "colour": "red", "volume_ml": "120"}
    )
    assert r.status_code == 303, r.text

    page = client.get("/milk").text
    assert "Fridge" in page
    assert "Red" in page
    assert "120" in page
    assert "0m" in page


# ---------------------------------------------------------------- invalid input


def test_use_on_a_batch_still_in_the_freezer_is_rejected() -> None:
    client = _client()
    batch_id = _store(client, store="freezer", colour="yellow", volume_ml="80")
    r = client.post("/api/milk/use", data={"batch_id": batch_id})
    assert r.status_code == 400
    assert "err" in r.text


def test_store_into_room_is_rejected() -> None:
    client = _client()
    r = client.post(
        "/api/milk/store", data={"store": "room", "colour": "yellow", "volume_ml": "50"}
    )
    assert r.status_code == 400
    assert "err" in r.text


# ------------------------------------------------------------------- profile guard


def test_milk_page_redirects_to_settings_without_a_profile() -> None:
    client = _client(seed_profile=False)
    r = client.get("/milk")
    assert r.status_code == 303
    assert "/settings" in r.headers["location"]
