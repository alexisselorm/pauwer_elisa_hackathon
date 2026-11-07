"""Thread keys (plan 001 WS2): one conversation, whatever the platform.

Slack groups by `thread_ts` (a top-level message is its own thread), Teams by
`conversation_id`, email by the root of its `In-Reply-To` / `References` chain.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def slack_event_id(channel: str, ts: str) -> str:
    return f"slack:{channel}:{ts}"


def slack_thread_id(channel: str, message: Mapping[str, Any]) -> str:
    return f"slack:{channel}:{message.get('thread_ts') or message['ts']}"


def teams_thread_id(message: Mapping[str, Any]) -> str:
    return f"teams:{message['conversation_id']}"


def email_thread_id(
    message_id: str,
    in_reply_to: str | None,
    references: str | None,
    subject: str | None = None,
) -> str:
    """The first Message-ID of `References` is the thread root (RFC 5322 §3.6.4), else the
    parent. Without either (Outlook exports often drop them), the normalised subject, as
    Outlook's conversation topic does: "RE: Fwd: TRN-042 traffic" joins "TRN-042 traffic".
    Else the message itself."""
    from kaiku.seed.profiles import thread_subject

    root = (references or "").split()
    parent = (root[0] if root else None) or in_reply_to
    if parent:
        return f"email:{parent}"
    if subject and (topic := thread_subject(subject)):
        return f"email:subject:{topic}"
    return f"email:{message_id}"
