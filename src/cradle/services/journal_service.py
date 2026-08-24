"""Baby journal use-cases (task U44): stories & temperament, photos, book export.

Not a quick-entry domain (U1/U16's <=2-tap philosophy doesn't fit writing a
story), so this is its own service rather than a LoggingService method, wired
into the Services bundle the same additive way GrowthService/MilestoneService
were. render_book renders its own jinja2 Environment rather than reusing
routers/pages.py's TEMPLATES instance: services may not import routers
(SPEC 3, layering).
"""

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import jinja2

from cradle.models import JournalEntry, JournalPhoto, to_local
from cradle.ports.clock import Clock
from cradle.repos.events_repo import EventsRepo

BABY_ID = 1  # single-baby v1 (D11), same constant as LoggingService's

# A phone-camera JPEG/HEIC typically runs a few MB; this caps a single upload
# without needing an image-processing dependency to downscale it first (SPEC
# §6 - Pillow would be a new dependency requiring architect sign-off, so this
# task deliberately doesn't add one). Documented in task U44's notes.
MAX_PHOTO_BYTES = 8 * 1024 * 1024

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "routers" / "templates"


class UnsupportedPhotoTypeError(ValueError):
    """Raised when an uploaded photo's content-type is not image/*."""


class PhotoTooLargeError(ValueError):
    """Raised when an uploaded photo exceeds MAX_PHOTO_BYTES."""


class UnknownJournalEntryError(ValueError):
    """Raised when a photo is attached to a journal entry that doesn't exist."""


@dataclass(frozen=True, slots=True)
class JournalPhotoRef:
    photo_id: int
    caption: str


@dataclass(frozen=True, slots=True)
class JournalCard:
    event_id: int
    ts: datetime
    title: str
    story: str
    temperament: tuple[str, ...]
    photos: tuple[JournalPhotoRef, ...]


class JournalService:
    def __init__(self, repo: EventsRepo, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock
        self._jinja = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=jinja2.select_autoescape(["html"]),
        )

    def _at(self, ts: datetime | None) -> datetime:
        return ts if ts is not None else self._clock.now()

    def create_entry(
        self,
        title: str,
        story: str = "",
        temperament: tuple[str, ...] = (),
        logged_by: str = "",
        ts: datetime | None = None,
    ) -> int:
        return self._repo.insert_journal_entry(
            JournalEntry(
                event_id=None,
                baby_id=BABY_ID,
                ts=self._at(ts),
                logged_by=logged_by,
                title=title,
                story=story,
                temperament=temperament,
            )
        )

    def add_photo(
        self,
        entry_id: int,
        content_type: str,
        image: bytes,
        caption: str = "",
        ts: datetime | None = None,
    ) -> int:
        if not content_type.startswith("image/"):
            raise UnsupportedPhotoTypeError(content_type)
        if len(image) > MAX_PHOTO_BYTES:
            raise PhotoTooLargeError(len(image))
        if self._repo.get_journal_entry(entry_id) is None:
            raise UnknownJournalEntryError(entry_id)
        return self._repo.insert_journal_photo(
            JournalPhoto(
                photo_id=None,
                journal_entry_id=entry_id,
                ts=self._at(ts),
                content_type=content_type,
                caption=caption,
                image=image,
            )
        )

    def list_entries(self, limit: int = 200) -> tuple[JournalCard, ...]:
        """Newest-first (EventsRepo._rows orders ts DESC)."""
        cards = []
        for e in self._repo.list_journal_entries(limit):
            if e.event_id is None:
                continue
            refs = tuple(
                JournalPhotoRef(photo_id=pid, caption=caption)
                for pid, caption in self._repo.list_journal_photo_refs(e.event_id)
            )
            cards.append(
                JournalCard(
                    event_id=e.event_id,
                    ts=e.ts,
                    title=e.title,
                    story=e.story,
                    temperament=e.temperament,
                    photos=refs,
                )
            )
        return tuple(cards)

    def get_photo_bytes(self, photo_id: int) -> tuple[bytes, str] | None:
        photo = self._repo.get_journal_photo(photo_id)
        return None if photo is None else (photo.image, photo.content_type)

    def render_book(self, entry_ids: Sequence[int]) -> str:
        """One self-contained HTML document: every selected entry, in
        chronological order, with photos inlined as base64 data: URIs so it
        opens standalone in any browser with no server (task U44)."""
        picked: list[JournalEntry] = []
        for entry_id in entry_ids:
            entry = self._repo.get_journal_entry(entry_id)
            if entry is not None:
                picked.append(entry)
        picked.sort(key=lambda e: e.ts)

        entries = []
        for e in picked:
            if e.event_id is None:
                continue
            photos = self._repo.list_journal_photos(e.event_id)
            entries.append(
                {
                    "title": e.title,
                    "story": e.story,
                    "temperament": e.temperament,
                    "ts_display": to_local(e.ts).strftime("%d %b %Y"),
                    "photos": [
                        {
                            "caption": p.caption,
                            "data_uri": (
                                f"data:{p.content_type};base64,"
                                f"{base64.b64encode(p.image).decode('ascii')}"
                            ),
                        }
                        for p in photos
                    ],
                }
            )

        template = self._jinja.get_template("journal_book.html")
        return template.render(
            entries=entries,
            generated_at=to_local(self._clock.now()).strftime("%d %b %Y %H:%M"),
        )
