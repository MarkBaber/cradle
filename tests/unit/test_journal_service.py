"""U44: journal stories & temperament, photos, and the HTML book export."""

import base64
from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.services.journal_service import (
    JournalService,
    PhotoTooLargeError,
    UnknownJournalEntryError,
    UnsupportedPhotoTypeError,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _service() -> JournalService:
    db = make_db()
    repo = make_repo(db)
    return JournalService(repo, clock())


# ------------------------------------------------------------------ entries


def test_create_entry_round_trips_with_multiple_temperament_tags() -> None:
    svc = _service()
    svc.create_entry("First giggle", "She laughed at the dog.", ("giggly", "curious"), ts=NOW)

    cards = svc.list_entries()
    assert len(cards) == 1
    assert cards[0].title == "First giggle"
    assert cards[0].story == "She laughed at the dog."
    assert cards[0].temperament == ("giggly", "curious")


def test_create_entry_round_trips_with_empty_temperament() -> None:
    svc = _service()
    svc.create_entry("Quiet afternoon", "Just napped a lot.", (), ts=NOW)

    cards = svc.list_entries()
    assert len(cards) == 1
    assert cards[0].temperament == ()


def test_list_entries_is_newest_first() -> None:
    svc = _service()
    svc.create_entry("Older", ts=NOW - timedelta(days=2))
    svc.create_entry("Newer", ts=NOW)

    cards = svc.list_entries()
    assert [c.title for c in cards] == ["Newer", "Older"]


# -------------------------------------------------------------------- photos


def test_add_photo_round_trips_bytes_and_content_type_exactly() -> None:
    svc = _service()
    entry_id = svc.create_entry("Bath time", ts=NOW)
    photo_id = svc.add_photo(entry_id, "image/png", _PNG, caption="splashing", ts=NOW)

    found = svc.get_photo_bytes(photo_id)
    assert found is not None
    data, content_type = found
    assert data == _PNG
    assert content_type == "image/png"


def test_add_photo_rejects_non_image_content_type() -> None:
    svc = _service()
    entry_id = svc.create_entry("Bath time", ts=NOW)
    try:
        svc.add_photo(entry_id, "application/pdf", b"%PDF-1.4", ts=NOW)
    except UnsupportedPhotoTypeError:
        return
    raise AssertionError("non-image content-type must be rejected")


def test_add_photo_rejects_oversized_upload() -> None:
    svc = _service()
    entry_id = svc.create_entry("Bath time", ts=NOW)
    from cradle.services.journal_service import MAX_PHOTO_BYTES

    too_big = b"\x00" * (MAX_PHOTO_BYTES + 1)
    try:
        svc.add_photo(entry_id, "image/png", too_big, ts=NOW)
    except PhotoTooLargeError:
        return
    raise AssertionError("oversized photo must be rejected")


def test_list_entries_exposes_photo_refs_for_thumbnails() -> None:
    svc = _service()
    entry_id = svc.create_entry("Bath time", ts=NOW)
    photo_id = svc.add_photo(entry_id, "image/png", _PNG, caption="splashing", ts=NOW)

    cards = svc.list_entries()
    assert len(cards[0].photos) == 1
    assert cards[0].photos[0].photo_id == photo_id
    assert cards[0].photos[0].caption == "splashing"


def test_get_photo_bytes_returns_none_for_unknown_id() -> None:
    svc = _service()
    assert svc.get_photo_bytes(999) is None


def test_add_photo_rejects_unknown_entry_id() -> None:
    svc = _service()
    try:
        svc.add_photo(999, "image/png", _PNG, ts=NOW)
    except UnknownJournalEntryError:
        return
    raise AssertionError("a photo attached to a nonexistent entry must be rejected")


# --------------------------------------------------------------------- book


def test_render_book_includes_only_selected_entries_in_chronological_order() -> None:
    svc = _service()
    first = svc.create_entry("Older", "story one", ts=NOW - timedelta(days=2))
    svc.create_entry("Skipped", "should not appear", ts=NOW - timedelta(days=1))
    second = svc.create_entry("Newer", "story two", ts=NOW)

    html = svc.render_book([second, first])

    assert html.index("Older") < html.index("Newer")
    assert "Skipped" not in html
    assert "should not appear" not in html


def test_render_book_embeds_photos_as_base64_data_uris_with_no_external_refs() -> None:
    svc = _service()
    entry_id = svc.create_entry("Bath time", "splashed around", ts=NOW)
    svc.add_photo(entry_id, "image/png", _PNG, caption="splashing", ts=NOW)

    html = svc.render_book([entry_id])

    b64 = base64.b64encode(_PNG).decode("ascii")
    assert f"data:image/png;base64,{b64}" in html
    assert "/static/" not in html
    assert "/api/" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_render_book_ignores_unknown_entry_ids() -> None:
    svc = _service()
    entry_id = svc.create_entry("Real entry", ts=NOW)
    html = svc.render_book([entry_id, 99999])
    assert "Real entry" in html
