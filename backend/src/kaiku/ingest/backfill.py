"""Historical backfill (BE-2.1, BE-2.2): Teams JSONL and the Outlook `.msg` export from
mock_data, and Elisa's example, with their original times (R1, R8). The demo profile skips
what plan 001 §3.5 keeps out of the demo, so the live Slack capture is the first time the
TRN-042 knowledge enters the system.

Re-running is a no-op: event ids are stable and the store is idempotent (ST-62).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from kaiku.domain.models import Platform
from kaiku.domain.time import elisa_backfill_time
from kaiku.ingest import trigger
from kaiku.ingest.email import from_msg, msg_files
from kaiku.ingest.msg_reader import MsgError, read_msg
from kaiku.ingest.store import CaptureStore, MessageIn
from kaiku.ingest.threads import teams_thread_id
from kaiku.seed.profiles import Profile, demo_exclusions
from kaiku.seed.sources import MockData

# Elisa's email has no date. It is the same email as the corpus copy
# <BE1A9D274C68A253...> sent 2025-10-14T08:40:00Z, so it gets that time.
ELISA_EMAIL_TIME = datetime(2025, 10, 14, 8, 40, tzinfo=UTC)
# ...so with Elisa's example loaded, the corpus copy is skipped: one email, one event.
ELISA_EMAIL_TWIN = "<BE1A9D274C68A253.nfurufochrhq@volga.example>"


class BackfillReport(BaseModel):
    teams_read: int = 0
    teams_skipped_demo: int = 0
    emails_read: int = 0
    emails_skipped_demo: int = 0
    emails_unreadable: int = 0
    elisa_read: int = 0
    new_events: int = 0


def teams_message(m: dict[str, Any]) -> MessageIn:
    text = m.get("text") or ""
    reactions = [r.get("reaction", "") for r in m.get("reactions") or []]
    return MessageIn(
        event_id=f"teams:{m['message_id']}",
        platform=Platform.TEAMS,
        channel=m["channel_id"],
        thread_id=teams_thread_id(m),
        event_time=m["created_at"],
        platform_seq=f"{int(m.get('created_iid') or 0):012d}",
        author=m.get("author_name"),
        author_role=m.get("role"),
        text=text,
        resolution=trigger.is_resolution(text)
        or any(trigger.is_resolution_reaction(r) for r in reactions),
    )


def elisa_example(folder: Path) -> list[MessageIn]:
    """`teams_chat.json` (times only, dated 2025-10-14 Helsinki, R8) and `email.txt`."""
    chat = json.loads((folder / "teams_chat.json").read_text("utf-8"))
    out = [
        MessageIn(
            event_id=f"elisa:teams_chat:{m['seq']}",
            platform=Platform.TEAMS,
            channel="elisa-example",
            thread_id="elisa:teams_chat",
            event_time=elisa_backfill_time(m["seq"], m["time"]),
            platform_seq=f"{m['seq']:02d}",
            author=m["author"],
            author_role=m.get("role"),
            text=m["text"],
            resolution=trigger.is_resolution(m["text"]),
        )
        for m in chat["messages"]
    ]
    raw = (folder / "email.txt").read_text("utf-8")
    head, _, body = raw.partition("\n\n")
    headers = dict(
        (k.strip().casefold(), v.strip())
        for k, _, v in (line.partition(":") for line in head.splitlines())
    )
    out.append(
        MessageIn(
            event_id="elisa:email",
            platform=Platform.EMAIL,
            channel="email",
            thread_id="elisa:email",
            event_time=ELISA_EMAIL_TIME,
            author=headers.get("from"),
            author_role="Network Expert",
            # The subject names the node ("RE: TRN-042 traffic ..."); the body doesn't.
            text=f"{headers.get('subject', '')}\n\n{body.strip()}".strip(),
        )
    )
    return out


async def backfill(
    store: CaptureStore,
    data: MockData,
    profile: Profile,
    *,
    elisa_dir: Path | None = None,
    now: datetime | None = None,
) -> BackfillReport:
    report = BackfillReport()
    exclusions = demo_exclusions(data) if profile is Profile.DEMO else None
    messages: list[MessageIn] = []
    for m in data.teams_messages():
        report.teams_read += 1
        if exclusions and exclusions.excludes_teams(m["message_id"], m["conversation_id"]):
            report.teams_skipped_demo += 1
            continue
        messages.append(teams_message(m))
    with_elisa = elisa_dir is not None and profile is Profile.FULL
    for path in msg_files(data.root):
        try:
            mail = read_msg(path)
        except MsgError:
            report.emails_unreadable += 1
            continue
        report.emails_read += 1
        if exclusions and exclusions.excludes_email(mail.message_id, mail.subject):
            report.emails_skipped_demo += 1
            continue
        if with_elisa and mail.message_id == ELISA_EMAIL_TWIN:
            continue
        messages.append(from_msg(mail))
    # Elisa's example is the 2025 TRN-042 thread itself: never in the demo (§3.5).
    if with_elisa and elisa_dir is not None:
        elisa = elisa_example(elisa_dir)
        report.elisa_read = len(elisa)
        messages += elisa
    report.new_events = await store.store(messages, now or datetime.now(UTC))
    return report
