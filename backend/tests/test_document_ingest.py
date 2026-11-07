"""Document connectors: expert PDF notes, Confluence HTML pages and ITSM KB articles, each
read from mock_data with its own time (R1), and their place in the backfill. Wiki and KB
pages are low authority: they never outrank expert chat or email."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pypdf import PdfWriter

from kaiku.extraction import reconcile
from kaiku.extraction.models import Relation
from kaiku.ingest.backfill import backfill
from kaiku.ingest.confluence_html import ConfluenceError, page_message, read_pages
from kaiku.ingest.itsm_kb import KbError, kb_message, read_articles
from kaiku.ingest.pdf_notes import PdfError, pdf_message, read_pdfs
from kaiku.ingest.store import MessageIn
from kaiku.seed.profiles import Profile
from kaiku.seed.sources import MockData

MOCK = Path(__file__).parents[2] / "mock_data"


class FakeStore:
    def __init__(self) -> None:
        self.messages: dict[str, MessageIn] = {}

    async def store(self, messages: Sequence[MessageIn], now: datetime) -> int:
        new = [m for m in messages if m.event_id not in self.messages]
        self.messages.update({m.event_id: m for m in new})
        return len(new)


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# --- PDF expert notes ----------------------------------------------------------------------


def test_expert_pdfs_are_events_at_their_creation_date() -> None:
    notes, unreadable = read_pdfs(MOCK)
    assert unreadable == []
    by_id = {m.event_id: m for m in notes}
    assert set(by_id) == {
        "pdf:2019-11_transmission_upgrade_notes.pdf",
        "pdf:2024-06_ivalo_site_access.pdf",
    }
    gen1 = by_id["pdf:2019-11_transmission_upgrade_notes.pdf"]
    assert gen1.event_time == _utc(2019, 11, 12, 9)  # CreationDate D:20191112090000Z
    assert gen1.platform == "pdf"
    assert gen1.thread_id == gen1.event_id  # one document, one thread
    assert gen1.author == "Erkki Expert"
    assert gen1.resolution  # a finished document: triage at the next sweep
    assert gen1.authority is None  # an expert's own notes keep the usual authority
    assert reconcile.channel_authority(gen1.platform) == reconcile.channel_authority("email")
    # Both pages, the scanned heading once.
    assert "Bounce the port. Do NOT swap the module" in gen1.text
    assert "a port bounce cleared" in gen1.text  # page 2
    assert gen1.text.count("Transmission upgrade notes - personal\n") == 1
    ivalo = by_id["pdf:2024-06_ivalo_site_access.pdf"]
    assert ivalo.event_time == _utc(2024, 6, 12, 8)
    assert ivalo.author == "Pekka Hämäläinen"
    assert "it is not locked" in ivalo.text


def _pdf(path: Path, **metadata: str) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if metadata:
        writer.add_metadata(metadata)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def test_a_pdf_without_a_date_or_text_is_skipped_not_given_the_clock(tmp_path: Path) -> None:
    with pytest.raises(PdfError, match="no CreationDate"):
        pdf_message(_pdf(tmp_path / "undated.pdf"))
    with pytest.raises(PdfError, match="no text"):
        pdf_message(_pdf(tmp_path / "scan.pdf", **{"/CreationDate": "D:20200101120000Z"}))
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    with pytest.raises(PdfError):
        pdf_message(tmp_path / "broken.pdf")
    notes_dir = tmp_path / "documents" / "expert_notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "broken.pdf").write_bytes(b"not a pdf")
    assert read_pdfs(tmp_path) == ([], ["broken.pdf"])


# --- Confluence HTML -----------------------------------------------------------------------


def test_confluence_pages_are_low_authority_at_their_last_edit() -> None:
    pages, unreadable = read_pages(MOCK)
    assert unreadable == []
    assert len(pages) == 3
    by_id = {m.event_id: m for m in pages}
    assert {k: v.event_time for k, v in by_id.items()} == {
        "confluence:c51affe7": _utc(2023, 5, 4),
        "confluence:3d6f1381": _utc(2025, 12, 15),
        "confluence:ce562b4a": _utc(2025, 6, 1),
    }
    for page in pages:
        assert page.platform == "confluence"
        assert page.authority == "low"
        assert reconcile.channel_authority(page.platform) < reconcile.channel_authority("email")
        assert page.author is None  # the export's anonymous "Confluence user"
        assert page.resolution
        assert "Last edited by" not in page.text  # the footer is metadata
    mtu = by_id["confluence:3d6f1381"]
    assert mtu.text == (
        "Core MTU policy\n\nAll core and transport links use MTU 9114. Do not change the MTU "
        "without an approved change and a lab test."
    )
    runbook = by_id["confluence:c51affe7"].text
    assert "Last reviewed 2023: only covers Gen1 up to TNC-2X." in runbook
    register = by_id["confluence:ce562b4a"].text
    assert "Ivalo | gate | contractor (snow)" in register  # table rows stay rows
    assert "Kemi: not in register." in register


def test_a_confluence_page_without_an_edit_date_is_skipped(tmp_path: Path) -> None:
    page = tmp_path / "draft.html"
    page.write_text("<html><head><title>Draft</title></head><body><p>TODO</p></body></html>")
    with pytest.raises(ConfluenceError):
        page_message(page)
    named = tmp_path / "named.html"
    named.write_text(
        "<html><head><title>Kemi access</title></head><body><p>Code lock &amp; key box.</p>"
        "<p class='meta'>Last edited by Heikki K - 2026-08-03</p></body></html>"
    )
    m = page_message(named)
    assert m.event_id == "confluence:named"  # no page id: the file name
    assert m.author == "Heikki K"
    assert m.event_time == _utc(2026, 8, 3)
    assert m.text == "Kemi access\n\nCode lock & key box."


# --- ITSM KB -------------------------------------------------------------------------------


def test_itsm_kb_articles_are_low_authority_at_their_publication() -> None:
    articles, unpublished, unreadable = read_articles(MOCK)
    assert (len(articles), unpublished, unreadable) == (4, 0, [])
    by_id = {m.event_id: m for m in articles}
    assert {k: v.event_time for k, v in by_id.items()} == {
        "itsm:KB0012345": _utc(2022, 1, 10),
        "itsm:KB0017001": _utc(2024, 3, 2),
        "itsm:KB0018222": _utc(2025, 12, 15),
        "itsm:KB0018420": _utc(2023, 6, 30),
    }
    for article in articles:
        assert article.platform == "itsm"
        assert article.authority == "low"
        assert reconcile.channel_authority(article.platform) < reconcile.channel_authority("teams")
        assert article.resolution
        # The answer key's annotation is not part of the article.
        assert "CONFLICT" not in article.text
    stale = by_id["itsm:KB0017001"]
    assert stale.text.startswith("KB0017001 TRN-042 known issue")  # the title names the node
    assert "reboot the whole node" in stale.text
    assert "bounce the port" not in stale.text.casefold()


def test_only_published_dated_articles_become_events(tmp_path: Path) -> None:
    (tmp_path / "itsm").mkdir()
    rows = [
        {"article_id": "KB1", "title": "Old", "body": "x", "valid_from": "2020-01-01",
         "status": "retired"},
        {"article_id": "KB2", "title": "Undated", "body": "x", "status": "published"},
        {"article_id": "KB3", "title": "Ok", "body": "Do y.", "valid_from": "2021-02-03",
         "status": "published"},
    ]  # fmt: skip
    lines = [json.dumps(r) for r in rows] + ["{not json"]
    (tmp_path / "itsm" / "knowledge_articles.jsonl").write_text("\n".join(lines) + "\n")
    articles, unpublished, unreadable = read_articles(tmp_path)
    assert [a.event_id for a in articles] == ["itsm:KB3"]
    assert articles[0].event_time == _utc(2021, 2, 3)
    assert unpublished == 1
    assert unreadable == ["line 2", "line 4"]
    with pytest.raises(KbError):
        kb_message({"title": "no id", "valid_from": "2021-01-01"})
    assert read_articles(tmp_path / "missing") == ([], 0, [])


# --- Backfill and authority ----------------------------------------------------------------


@pytest.mark.parametrize("profile", [Profile.DEMO, Profile.FULL])
async def test_backfill_brings_every_document_in_both_profiles(profile: Profile) -> None:
    store = FakeStore()
    report = await backfill(store, MockData(MOCK), profile)  # type: ignore[arg-type]
    assert (report.pdfs_read, report.confluence_read, report.kb_read) == (2, 3, 4)
    assert report.pdfs_unreadable == report.confluence_unreadable == report.kb_unreadable == 0
    docs = [m for m in store.messages.values() if m.platform in ("pdf", "confluence", "itsm")]
    assert len(docs) == 9
    # The 2019 note corroborates and the stale KB contradicts, in the demo too (§3.5).
    assert "pdf:2019-11_transmission_upgrade_notes.pdf" in store.messages
    assert "itsm:KB0017001" in store.messages
    again = await backfill(store, MockData(MOCK), profile)  # type: ignore[arg-type]
    assert again.new_events == 0  # stable event ids: a re-run writes nothing


T0 = datetime(2025, 10, 14, 6, 22, 8, tzinfo=UTC)
LATER = datetime(2026, 1, 10, tzinfo=UTC)


def _expert(**kw: object) -> dict[str, object]:
    return {"assertion_id": "EXPERT", "threads": ["slack:C:1"], "valid_from": T0,
            "recorded_at": T0, "platform": "slack", "authority": 1, **kw}  # fmt: skip


def test_a_newer_kb_page_contradicts_expert_knowledge_but_never_supersedes_it() -> None:
    def decide(authority: int, cand: dict[str, object]) -> reconcile.Decision:
        return reconcile.decide(
            thread_id="itsm:KB9",
            valid_from=LATER,  # published after the expert spoke
            candidates=[cand],
            relations=[Relation(assertion_id="EXPERT", relation="supersedes")],
            authority=authority,
        )

    kb = reconcile.channel_authority("itsm")
    wiki = reconcile.channel_authority("confluence")
    assert decide(kb, _expert()) == reconcile.Decision(contradicts=["EXPERT"])
    assert decide(wiki, _expert()) == reconcile.Decision(contradicts=["EXPERT"])
    # Equal authority keeps the R4b path: a later expert (or a later KB over a KB) supersedes.
    assert decide(reconcile.channel_authority("email"), _expert()).supersede == "EXPERT"
    assert decide(kb, _expert(platform="itsm", authority=0)).supersede == "EXPERT"
    # Candidates written before authority was stored count as expert chat.
    assert decide(kb, _expert(authority=None)).supersede is None
