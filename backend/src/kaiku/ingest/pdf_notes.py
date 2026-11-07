"""Expert notes as PDF (plan 001 WS2, §3.5) -> one `Message` event per file.

`event_time` is the PDF's own CreationDate (R1): for a scanned note, when it was scanned.
A PDF without one can't be placed in time, so it is skipped rather than given the
ingestion clock. The notes are an expert's own words, so they keep the usual authority:
in the demo the 2019 note corroborates the rule captured live.

A PDF is a finished document, so it counts as a resolution signal: its thread is triaged
at the next sweep.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from kaiku.domain.models import Platform
from kaiku.ingest.store import MessageIn

CHANNEL = "expert_notes"


class PdfError(ValueError):
    """A PDF that can't become an event (unreadable, no text or no CreationDate)."""


def pdf_files(root: Path) -> Iterable[Path]:
    d = root / "documents" / "expert_notes"
    if d.is_dir():
        yield from sorted(d.glob("*.pdf"))


def _lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def pdf_message(path: Path) -> MessageIn:
    try:
        reader = PdfReader(path)
        meta = reader.metadata
        pages = [page.extract_text() or "" for page in reader.pages]
        created = meta.creation_date if meta is not None else None
    except (PyPdfError, OSError, ValueError) as exc:
        raise PdfError(f"{path.name}: {exc}") from exc
    if created is None:
        raise PdfError(f"{path.name}: no CreationDate")
    if created.tzinfo is None:  # the spec leaves the zone unknown: read it as UTC
        created = created.replace(tzinfo=UTC)
    title = (meta.title if meta is not None else None) or path.stem
    # A scan repeats its heading on every page: keep the title once, then each page's text.
    body = []
    for page in pages:
        body += [line for line in _lines(page) if line != title]
    if not body:
        raise PdfError(f"{path.name}: no text (an image-only scan needs OCR)")
    event_id = f"pdf:{path.name}"
    return MessageIn(
        event_id=event_id,
        platform=Platform.PDF,
        channel=CHANNEL,
        thread_id=event_id,
        event_time=created.astimezone(UTC),
        author=(meta.author if meta is not None else None) or None,
        text=f"{title}\n\n" + "\n".join(body),
        resolution=True,
    )


def read_pdfs(root: Path) -> tuple[list[MessageIn], list[str]]:
    """Every expert note under `documents/expert_notes`, and the names of unreadable ones."""
    out, unreadable = [], []
    for path in pdf_files(root):
        try:
            out.append(pdf_message(path))
        except PdfError:
            unreadable.append(path.name)
    return out, unreadable
