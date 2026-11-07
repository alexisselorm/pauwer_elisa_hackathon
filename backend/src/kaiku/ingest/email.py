"""Email -> `Message` events (BE-2.2, plan 001 WS2).

Sources: an `.eml` drop folder (live: a forwarded recap lands there) and the Outlook `.msg`
backfill. `event_time` is the email's own time (R1): the `Date` header for `.eml`,
PR_CLIENT_SUBMIT_TIME for `.msg`. Event ids are the Message-ID, so the same email arriving
twice (drop folder and backfill) is one event (ST-62).

An email is a finished message, not a live conversation, so it counts as a resolution
signal: its thread is triaged at the next sweep instead of after the idle wait.
Captures from email are never announced in work channels; they go to #context-log, and a
Slack card they corroborate gains "+1 source (email)" (slack/capture.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from kaiku.domain.models import Platform
from kaiku.ingest.msg_reader import MsgError, MsgFields, read_msg
from kaiku.ingest.store import CaptureStore, MessageIn
from kaiku.ingest.threads import email_thread_id
from kaiku.logging import get_logger

log = get_logger(__name__)
CHANNEL = "email"


class EmailError(ValueError):
    """An email that can't become an event (no Message-ID or no date)."""


def email_event(
    *,
    message_id: str,
    sent_at: datetime,
    subject: str,
    body: str,
    sender: str | None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> MessageIn:
    if not message_id:
        raise EmailError("no Message-ID")
    return MessageIn(
        event_id=f"email:{message_id}",
        platform=Platform.EMAIL,
        channel=CHANNEL,
        thread_id=email_thread_id(message_id, in_reply_to, references, subject),
        event_time=sent_at,
        author=sender or None,
        # The subject often names the node ("RE: TRN-042 traffic ..."), the body may not.
        text=f"{subject}\n\n{body.strip()}".strip(),
        resolution=True,
    )


def eml_message(path: Path) -> MessageIn:
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    date = msg.get("Date")
    if not date:
        raise EmailError(f"{path.name}: no Date header")
    sent_at = parsedate_to_datetime(str(date))
    if sent_at.tzinfo is None:  # RFC 5322 "-0000": the time is UTC, origin unknown
        sent_at = sent_at.replace(tzinfo=UTC)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part is not None else ""
    name, addr = parseaddr(str(msg.get("From", "")))
    return email_event(
        message_id=str(msg.get("Message-ID", "")).strip(),
        sent_at=sent_at,
        subject=str(msg.get("Subject", "")),
        body=str(body),
        sender=name or addr,
        in_reply_to=str(msg.get("In-Reply-To") or "").strip() or None,
        references=str(msg.get("References") or "").strip() or None,
    )


def msg_message(path: Path) -> MessageIn:
    return from_msg(read_msg(path))


def from_msg(m: MsgFields) -> MessageIn:
    return email_event(
        message_id=m.message_id,
        sent_at=m.sent_at,
        subject=m.subject,
        body=m.body,
        sender=m.sender_name or m.sender_email,
        in_reply_to=m.in_reply_to,
        references=m.references,
    )


def read_folder(folder: Path) -> list[MessageIn]:
    """Every readable `.eml` and `.msg` in a folder; unreadable files are logged, skipped."""
    out = []
    for path in sorted(folder.iterdir()):
        reader = {".eml": eml_message, ".msg": msg_message}.get(path.suffix.casefold())
        if reader is None:
            continue
        try:
            out.append(reader(path))
        except (EmailError, MsgError, ValueError) as exc:
            log.warning("email_unreadable", file=path.name, error=str(exc))
    return out


def _now() -> datetime:
    return datetime.now(UTC)


class EmailDrop:
    """Polls a drop folder. Files already stored are skipped by name; the store is
    idempotent on the Message-ID anyway, so a restart re-reading everything is harmless."""

    def __init__(
        self,
        store: CaptureStore,
        folder: Path,
        *,
        on_new: Callable[[], None] | None = None,
        every_s: float = 10.0,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        self.folder = folder
        self.on_new = on_new
        self.every_s = every_s
        self.clock = clock
        self._seen: set[str] = set()

    async def poll_once(self) -> int:
        if not self.folder.is_dir():
            return 0
        fresh = [p for p in self.folder.iterdir() if p.name not in self._seen]
        if not fresh:
            return 0
        messages = await asyncio.to_thread(read_folder, self.folder)
        new = await self.store.store(messages, self.clock())
        self._seen.update(p.name for p in fresh)
        if new:
            log.info("email_ingested", new=new, folder=str(self.folder))
            if self.on_new is not None:
                self.on_new()
        return new

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # a bad poll must not stop the bot
                log.exception("email_poll_failed", folder=str(self.folder))
            await asyncio.sleep(self.every_s)


def msg_files(root: Path) -> Iterable[Path]:
    """The Outlook export: Inbox and Sent Items."""
    for folder in ("Inbox", "Sent Items"):
        d = root / "email_outlook" / folder
        if d.is_dir():
            yield from sorted(d.glob("*.msg"))
