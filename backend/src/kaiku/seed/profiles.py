"""Seed profiles (plan 001 §3.5).

`demo`: what the live demo starts from. The network layers only, never gold, and a
historical backfill (BE-2.1) WITHOUT any corpus source about the forwarding stall after an
SFP swap, so the live capture in Slack is the first time that knowledge enters the system.
The 2019 PDF note (and the email that forwards it) and the stale ITSM KB stay in, as
corroboration and contradiction.

`full`: everything, for the T1 regression eval.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from kaiku.seed.sources import MockData

FORWARDING_TOPIC = "forwarding_state_after_sfp_swap"
# Source types of the forwarding-stall gold that stay in the demo backfill.
KEPT_SOURCE_TYPES = frozenset({"pdf", "itsm", "confluence"})
# ASR-0004 is Veikko's 2025-11-03 email forwarding the scanned 2019 PDF note. Its body only
# asks "is this still valid?"; the knowledge is the PDF, which stays in.
KEPT_ASSERTIONS = frozenset({"ASR-0004"})

_REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|vs|vl|sv|aw)\s*:\s*)+", re.IGNORECASE)


class Profile(StrEnum):
    DEMO = "demo"
    FULL = "full"


def thread_subject(subject: str) -> str:
    """Email thread key: 'RE: Fwd: TRN-042 traffic ...' -> 'trn-042 traffic ...'."""
    return " ".join(_REPLY_PREFIX.sub("", subject).split()).casefold()


class DemoExclusions(BaseModel):
    """What the demo backfill must skip. Message-level for Teams (whole conversations) and
    email (whole reply chains by subject)."""

    model_config = ConfigDict(frozen=True)

    gold_assertion_ids: frozenset[str]
    teams_conversation_ids: frozenset[str]
    teams_message_ids: frozenset[str]
    email_message_ids: frozenset[str]
    email_thread_subjects: frozenset[str]

    def excludes_teams(self, message_id: str, conversation_id: str) -> bool:
        return (
            message_id in self.teams_message_ids or conversation_id in self.teams_conversation_ids
        )

    def excludes_email(self, message_id: str, subject: str) -> bool:
        return (
            message_id in self.email_message_ids
            or thread_subject(subject) in self.email_thread_subjects
        )


def demo_exclusions(data: MockData) -> DemoExclusions:
    excluded = [
        a
        for a in data.gold_assertions()
        if a.topic == FORWARDING_TOPIC
        and a.source_type not in KEPT_SOURCE_TYPES
        and a.assertion_id not in KEPT_ASSERTIONS
    ]
    teams_refs = {a.source_ref for a in excluded if a.source_type == "teams_message"}
    email_refs = {a.source_ref for a in excluded if a.source_type == "email"}

    messages = list(data.teams_messages())
    conversations = {m["conversation_id"] for m in messages if m["message_id"] in teams_refs}
    teams_ids = {m["message_id"] for m in messages if m["conversation_id"] in conversations}

    mail = data.mail_index()
    subjects = {thread_subject(m["subject"]) for m in mail if m["message_id"] in email_refs}
    email_ids = {m["message_id"] for m in mail if thread_subject(m["subject"]) in subjects}

    missing = (teams_refs - teams_ids) | (email_refs - email_ids)
    if missing:
        raise ValueError(f"gold sources not found in the corpus: {sorted(missing)}")
    return DemoExclusions(
        gold_assertion_ids=frozenset(a.assertion_id for a in excluded),
        teams_conversation_ids=frozenset(conversations),
        teams_message_ids=frozenset(teams_ids),
        email_message_ids=frozenset(email_ids),
        email_thread_subjects=frozenset(subjects),
    )
