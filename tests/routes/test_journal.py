"""U44: the /journal page, photo upload/serving, and the HTML book export,
proven at the HTTP boundary.

Skipped by the offline runner when fastapi is unavailable.
"""

import base64
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.models.timefmt import to_local  # noqa: E402
from cradle.ports.clock import FixedClock  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _client(seed_profile: bool = True) -> TestClient:
    db = Path(tempfile.mkdtemp()) / "routes.db"
    app = create_app(db_path=db, clock=FixedClock(NOW), config_path=ROOT / "rules_config.toml")
    client = TestClient(app, follow_redirects=False)
    if seed_profile:
        assert client.post("/api/settings/profile", data=PROFILE).status_code == 303
    return client


def _create_entry(
    client: TestClient,
    title: str,
    story: str = "",
    temperament: str = "",
    date: str | None = None,
) -> int:
    data = {"title": title, "story": story, "temperament": temperament}
    if date is not None:
        data["date"] = date
        data["ts"] = "09:00"
    r = client.post("/api/journal", data=data)
    assert r.status_code == 303, r.text
    return max(c.event_id for c in client.app.state.services.journal.list_entries())


# --------------------------------------------------------------------- page


def test_journal_page_redirects_to_settings_without_a_profile() -> None:
    client = _client(seed_profile=False)
    r = client.get("/journal")
    assert r.status_code == 303
    assert "/settings" in r.headers["location"]


def test_journal_reachable_from_tabbar() -> None:
    client = _client()
    page = client.get("/journal").text
    assert 'href="/journal"' in page


def test_base_tabbar_links_to_journal() -> None:
    client = _client()
    page = client.get("/today").text
    assert 'href="/journal"' in page


def test_journal_page_lists_entries_newest_first_with_checkbox_and_thumbnail() -> None:
    client = _client()
    _create_entry(client, "Older entry", date="2026-07-01")
    _create_entry(client, "Newer entry", date="2026-07-15")
    entry_id = client.app.state.services.journal.list_entries()[0].event_id  # newest

    r = client.post(
        "/api/journal/photo",
        data={"entry_id": entry_id, "caption": "smiling"},
        files={"photo": ("p.png", _PNG, "image/png")},
    )
    assert r.status_code == 303, r.text

    page = client.get("/journal").text
    assert page.index("Newer entry") < page.index("Older entry")
    assert 'type="checkbox" name="entry_id" value="' in page
    assert "/api/journal/photo/" in page  # thumbnail <img src>


# -------------------------------------------------------------------- entry


def test_create_entry_round_trips_title_story_and_temperament() -> None:
    client = _client()
    entry_id = _create_entry(client, "First giggle", "She laughed.", "giggly, curious")
    card = client.app.state.services.journal.list_entries()[0]
    assert card.event_id == entry_id
    assert card.title == "First giggle"
    assert card.story == "She laughed."
    assert card.temperament == ("giggly", "curious")


# --------------------------------------------------------------------- U45


def test_journal_entry_form_uses_the_vendored_combined_picker() -> None:
    """U45: /journal's create-entry ts field is wired to the same AnyPicker
    bundle + entry.js as quick-entry/history (U29's bindPanelPickers), not a
    bare native date/time input with no picker."""
    client = _client()
    page = client.get("/journal").text
    assert '<script src="/static/vendor/jquery.min.js" defer></script>' in page
    assert '<script src="/static/vendor/anypicker.min.js" defer></script>' in page
    assert '<script src="/static/entry.js" defer></script>' in page
    assert '<link rel="stylesheet" href="/static/vendor/anypicker-all.min.css">' in page
    assert '<input type="hidden" name="date" value="">' in page
    assert '<input type="time" name="ts">' in page


def test_journal_entry_ts_field_still_submits_with_the_picker_script_unloaded() -> None:
    """With the picker script unloaded - true of this client, which never
    executes JS - the plain native date/time inputs alone must still post a
    value /api/journal already accepts (the no-JS contract of U19/U22/U29/U31)."""
    client = _client()
    entry_id = _create_entry(client, "Backdated story", date="2026-07-10")
    cards = client.app.state.services.journal.list_entries()
    card = next(c for c in cards if c.event_id == entry_id)
    local = to_local(card.ts)
    assert local.date().isoformat() == "2026-07-10"
    assert (local.hour, local.minute) == (9, 0)


# -------------------------------------------------------------------- photo


def test_upload_and_serve_photo_byte_for_byte() -> None:
    client = _client()
    entry_id = _create_entry(client, "Bath time")

    r = client.post(
        "/api/journal/photo",
        data={"entry_id": entry_id, "caption": "splashing"},
        files={"photo": ("p.png", _PNG, "image/png")},
    )
    assert r.status_code == 303, r.text

    photo_id = client.app.state.services.journal.list_entries()[0].photos[0].photo_id
    got = client.get(f"/api/journal/photo/{photo_id}")
    assert got.status_code == 200
    assert got.content == _PNG
    assert got.headers["content-type"] == "image/png"


def test_non_image_content_type_is_rejected() -> None:
    client = _client()
    entry_id = _create_entry(client, "Bath time")

    r = client.post(
        "/api/journal/photo",
        data={"entry_id": entry_id},
        files={"photo": ("p.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert r.status_code == 400
    assert "err" in r.text


def test_photo_for_unknown_entry_is_rejected_not_a_500() -> None:
    client = _client()
    r = client.post(
        "/api/journal/photo",
        data={"entry_id": 999999},
        files={"photo": ("p.png", _PNG, "image/png")},
    )
    assert r.status_code == 400
    assert "err" in r.text


# --------------------------------------------------------------------- book


def test_book_contains_only_checked_entries_in_chronological_order() -> None:
    client = _client()
    older = _create_entry(client, "Older", date="2026-07-01")
    _create_entry(client, "Skipped", date="2026-07-10")
    newer = _create_entry(client, "Newer", date="2026-07-15")

    r = client.post("/api/journal/book", data={"entry_id": [str(newer), str(older)]})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert 'attachment; filename="journal-book.html"' in r.headers["content-disposition"]

    body = r.text
    assert body.index("Older") < body.index("Newer")
    assert "Skipped" not in body


def test_book_embeds_photo_as_base64_with_no_external_references() -> None:
    client = _client()
    entry_id = _create_entry(client, "Bath time", "splashed around")
    client.post(
        "/api/journal/photo",
        data={"entry_id": entry_id, "caption": "splashing"},
        files={"photo": ("p.png", _PNG, "image/png")},
    )

    r = client.post("/api/journal/book", data={"entry_id": [str(entry_id)]})
    assert r.status_code == 200, r.text
    body = r.text

    b64 = base64.b64encode(_PNG).decode("ascii")
    assert f"data:image/png;base64,{b64}" in body
    assert "/static/" not in body
    assert "/api/" not in body
    assert "http://" not in body
    assert "https://" not in body


def test_book_request_with_malformed_entry_id_does_not_500() -> None:
    client = _client()
    kept = _create_entry(client, "Keep me")

    r = client.post("/api/journal/book", data={"entry_id": [str(kept), "not-a-number"]})
    assert r.status_code == 200, r.text
    assert "Keep me" in r.text


def test_unchecked_entry_is_excluded_from_book() -> None:
    client = _client()
    kept = _create_entry(client, "Keep me")
    _create_entry(client, "Drop me")

    r = client.post("/api/journal/book", data={"entry_id": [str(kept)]})
    assert r.status_code == 200, r.text
    assert "Keep me" in r.text
    assert "Drop me" not in r.text
