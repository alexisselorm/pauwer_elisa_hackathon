"""Confluence pages exported as HTML (plan 001 WS2, §3.5) -> one `Message` event per page.

`event_time` is when the page was last edited (R1), read from the export's footer
("Last edited by ... - 2025-12-15 - page id 3d6f1381"). A page without that date can't be
placed in time, so it is skipped rather than given the ingestion clock.

Wiki pages are often stale, so they are tagged `authority: low`: in a contradiction the
page is disputed, never the expert chat or email it disagrees with (reconcile.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from kaiku.domain.models import Platform
from kaiku.ingest.store import MessageIn

CHANNEL = "confluence"
AUTHORITY = "low"
# The footer of an exported page. The date is a calendar day: the edit time isn't exported.
_EDITED = re.compile(
    r"Last edited by\s+(?P<who>.+?)\s+-\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"(?:\s+-\s+page id\s+(?P<page>\S+))?",
    re.IGNORECASE,
)
# Elements whose text ends a line; table cells are joined with " | ".
_BLOCKS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "hr"})
_SKIP = frozenset({"script", "style", "head"})


class ConfluenceError(ValueError):
    """A page that can't become an event (no last-edited date)."""


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.lines: list[str] = []
        self._line: list[str] = []
        self._cells: list[str] | None = None
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip += 1
        elif tag == "tr":
            self._cells = []
        elif tag in ("td", "th"):
            self._flush_cell()
        elif tag in _BLOCKS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th"):
            self._flush_cell()
        elif tag == "tr":
            cells, self._cells = self._cells or [], None
            if cells:
                self.lines.append(" | ".join(cells))
        elif tag in _BLOCKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._line.append(data)

    def _flush_cell(self) -> None:
        text = " ".join("".join(self._line).split())
        self._line = []
        if self._cells is not None and text:
            self._cells.append(text)

    def _flush(self) -> None:
        text = " ".join("".join(self._line).split())
        self._line = []
        if text:
            self.lines.append(text)

    def close(self) -> None:
        super().close()
        self._flush()


def page_files(root: Path) -> Iterable[Path]:
    d = root / "documents" / "confluence"
    if d.is_dir():
        yield from sorted(d.glob("*.htm*"))


def page_message(path: Path) -> MessageIn:
    parser = _Text()
    parser.feed(path.read_text("utf-8"))
    parser.close()
    edited = next((m for line in parser.lines if (m := _EDITED.search(line))), None)
    if edited is None:
        raise ConfluenceError(f"{path.name}: no last-edited date")
    edited_at = datetime.fromisoformat(edited["day"]).replace(tzinfo=UTC)
    title = " ".join(parser.title.split()) or path.stem
    # The heading repeats the title; the footer is metadata, kept out of the knowledge.
    body = [line for line in parser.lines if line != title and not _EDITED.search(line)]
    who = edited["who"].strip()
    # An anonymised export says "Confluence user": nobody to credit or ask.
    author = None if who.casefold() in ("", "confluence user") else who
    event_id = f"confluence:{edited['page'] or path.stem}"
    return MessageIn(
        event_id=event_id,
        platform=Platform.CONFLUENCE,
        channel=CHANNEL,
        thread_id=event_id,
        event_time=edited_at,
        author=author,
        text=f"{title}\n\n" + "\n".join(body),
        resolution=True,
        authority=AUTHORITY,
    )


def read_pages(root: Path) -> tuple[list[MessageIn], list[str]]:
    """Every exported page under `documents/confluence`, and the names of unreadable ones."""
    out, unreadable = [], []
    for path in page_files(root):
        try:
            out.append(page_message(path))
        except (ConfluenceError, UnicodeDecodeError):
            unreadable.append(path.name)
    return out, unreadable
