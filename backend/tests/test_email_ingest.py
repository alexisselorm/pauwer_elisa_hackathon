"""BE-2.2: email ingest. The .msg export, the .eml drop folder, threading and the backfill."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

from kaiku.ingest.backfill import ELISA_EMAIL_TWIN, backfill
from kaiku.ingest.email import EmailDrop, eml_message, msg_files, msg_message
from kaiku.ingest.msg_reader import PR_TRANSPORT_MESSAGE_HEADERS, read_msg, read_properties
from kaiku.ingest.store import MessageIn
from kaiku.ingest.threads import email_thread_id
from kaiku.seed.profiles import Profile, demo_exclusions
from kaiku.seed.sources import MockData

MOCK = Path(__file__).parents[2] / "mock_data"
ELISA = Path(__file__).parents[2] / "Elisa_SinceAI2026" / "ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026"


class FakeStore:
    def __init__(self) -> None:
        self.messages: dict[str, MessageIn] = {}

    async def store(self, messages: Sequence[MessageIn], now: datetime) -> int:
        new = [m for m in messages if m.event_id not in self.messages]
        self.messages.update({m.event_id: m for m in new})
        return len(new)


def test_every_msg_parses_with_the_index_time_and_date_header() -> None:
    index = {
        r["message_id"]: r
        for r in json.loads((MOCK / "email_outlook" / "mail_export_index.json").read_text())
    }
    files = list(msg_files(MOCK))
    assert len(files) == 145  # same count as mock_data/scripts/verify_msg.py
    for path in files:
        m = read_msg(path)
        assert m.message_class == "IPM.Note"
        row = index[m.message_id]
        assert m.sent_at == datetime.fromisoformat(row["sent_at"].replace("Z", "+00:00"))
        headers = str(read_properties(path)[PR_TRANSPORT_MESSAGE_HEADERS])
        date = next(h for h in headers.splitlines() if h.startswith("Date:"))
        assert parsedate_to_datetime(date[5:].strip()) == m.sent_at, path.name


def test_msg_event_is_a_resolved_email_threaded_by_subject() -> None:
    path = next(p for p in msg_files(MOCK) if " - Re SFP stock levels" in p.name)
    m = msg_message(path)
    assert m.event_id.startswith("email:<")
    assert m.platform == "email"
    assert m.resolution  # an email is a finished message: triage at the next sweep
    assert m.thread_id.startswith("email:subject:sfp stock levels")  # no In-Reply-To in .msg
    assert m.text.startswith("Re: SFP stock levels")


def test_subject_threads_a_forwarded_reply_with_its_root() -> None:
    root = email_thread_id("<a@x>", None, None, "TRN-042 traffic not recovering")
    reply = email_thread_id("<b@x>", None, None, "RE: Fwd: TRN-042  traffic not recovering")
    assert root == reply == "email:subject:trn-042 traffic not recovering"
    assert email_thread_id("<c@x>", "<a@x>", None, "RE: other") == "email:<a@x>"
    assert email_thread_id("<d@x>", "<b@x>", "<a@x> <b@x>", "x") == "email:<a@x>"


def _eml(folder: Path, name: str, extra: str = "") -> Path:
    path = folder / name
    path.write_text(
        "From: Erkki Expert <erkki@example.fi>\n"
        "To: Veikko Vikkela <veikko@example.fi>\n"
        "Subject: RE: TRN-042 traffic not recovering after SFP replacement\n"
        "Date: Tue, 14 Oct 2025 11:40:00 +0300\n"
        "Message-ID: <recap-1@example.fi>\n"
        f"{extra}"
        "Content-Type: text/plain; charset=utf-8\n\n"
        "Before replacing the SFP again or rebooting the unit, check the traffic counters.\n",
        encoding="utf-8",
    )
    return path


def test_eml_event_time_is_the_date_header_in_utc(tmp_path: Path) -> None:
    m = eml_message(_eml(tmp_path, "recap.eml", "In-Reply-To: <question-1@example.fi>\n"))
    assert m.event_time == datetime(2025, 10, 14, 8, 40, tzinfo=UTC)  # R1
    assert m.event_id == "email:<recap-1@example.fi>"
    assert m.thread_id == "email:<question-1@example.fi>"  # In-Reply-To wins over subject
    assert m.author == "Erkki Expert"
    assert "TRN-042" in m.text  # the subject names the node
    assert "check the traffic counters" in m.text


async def test_drop_folder_ingests_once_and_kicks_the_sweeper(tmp_path: Path) -> None:
    store, kicks = FakeStore(), []
    drop = EmailDrop(store, tmp_path, on_new=lambda: kicks.append(1))  # type: ignore[arg-type]
    assert await drop.poll_once() == 0
    _eml(tmp_path, "recap.eml")
    (tmp_path / "notes.txt").write_text("not an email")
    (tmp_path / "broken.eml").write_text("Subject: no date or id\n\nhi")
    assert await drop.poll_once() == 1
    assert await drop.poll_once() == 0  # nothing new
    assert kicks == [1]
    # The same email again under another name is the same event (Message-ID).
    _eml(tmp_path, "recap-copy.eml")
    assert await drop.poll_once() == 0


async def test_demo_backfill_skips_the_forwarding_stall_emails() -> None:
    data = MockData(MOCK)
    store = FakeStore()
    report = await backfill(store, data, Profile.DEMO)  # type: ignore[arg-type]
    assert report.emails_read == 145
    assert report.emails_unreadable == 0
    excluded = demo_exclusions(data).email_message_ids
    assert report.emails_skipped_demo == len(excluded) > 0
    assert not {f"email:{i}" for i in excluded} & set(store.messages)


@pytest.mark.skipif(not ELISA.is_dir(), reason="Elisa's example folder is not present")
async def test_full_backfill_has_elisas_email_once() -> None:
    store = FakeStore()
    report = await backfill(store, MockData(MOCK), Profile.FULL, elisa_dir=ELISA)  # type: ignore[arg-type]
    assert report.emails_skipped_demo == 0
    assert "elisa:email" in store.messages
    assert f"email:{ELISA_EMAIL_TWIN}" not in store.messages
