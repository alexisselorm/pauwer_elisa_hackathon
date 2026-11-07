"""ITSM knowledge articles (plan 001 WS2, §3.5) -> one `Message` event per article.

`event_time` is the article's publication date (`valid_from`, a calendar day; R1). Only
published articles are read: a draft or retired article is not what anyone was told.

KB articles are often stale, so they are tagged `authority: low`: in the demo the
"reboot the whole node" workaround contradicts the expert rule and is the side that is
disputed (reconcile.py). The export's `conflict_note` is the answer key's annotation, not
part of the article, so it never reaches the event.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from kaiku.domain.models import Platform
from kaiku.ingest.store import MessageIn

CHANNEL = "itsm_kb"
AUTHORITY = "low"


class KbError(ValueError):
    """An article that can't become an event (no id or no publication date)."""


def kb_path(root: Path) -> Path:
    return root / "itsm" / "knowledge_articles.jsonl"


def kb_message(article: dict[str, Any]) -> MessageIn:
    article_id = str(article.get("article_id") or "").strip()
    if not article_id:
        raise KbError("no article_id")
    published = str(article.get("valid_from") or "").strip()
    if not published:
        raise KbError(f"{article_id}: no publication date")
    try:
        day = date.fromisoformat(published[:10])
    except ValueError as exc:
        raise KbError(f"{article_id}: bad publication date {published!r}") from exc
    title = str(article.get("title") or "").strip()
    body = str(article.get("body") or "").strip()
    event_id = f"itsm:{article_id}"
    return MessageIn(
        event_id=event_id,
        platform=Platform.ITSM,
        channel=CHANNEL,
        thread_id=event_id,
        event_time=datetime(day.year, day.month, day.day, tzinfo=UTC),
        # The title often names the node ("TRN-042 known issue ..."), the body may not.
        text=f"{article_id} {title}\n\n{body}".strip(),
        resolution=True,
        authority=AUTHORITY,
    )


def read_articles(root: Path) -> tuple[list[MessageIn], int, list[str]]:
    """The published articles, how many others were skipped, and the unreadable ids."""
    path = kb_path(root)
    if not path.is_file():
        return [], 0, []
    out, unpublished, unreadable = [], 0, []
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                article = json.loads(line)
                if str(article.get("status") or "published").casefold() != "published":
                    unpublished += 1
                    continue
                out.append(kb_message(article))
            except (KbError, ValueError, AttributeError):
                unreadable.append(f"line {n}")
    return out, unpublished, unreadable
