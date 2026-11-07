"""Demo replay (plan 002 §3.6): post Elisa's 12 messages verbatim into a channel as personas
(`chat:write.customize`), in one thread, with the original gaps shortened by `speed`.
Event times are the real Slack `ts`. The capture loop treats them as people's words.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from slack_sdk.web.async_client import AsyncWebClient

from silentctx.ingest.backfill import elisa_example


@dataclass
class Posted:
    seq: int
    ts: str
    author: str
    text: str


async def replay_elisa(
    client: AsyncWebClient, channel: str, elisa_dir: Path, *, speed: float = 30.0
) -> list[Posted]:
    chat = [m for m in elisa_example(elisa_dir) if m.thread_id == "elisa:teams_chat"]
    posted: list[Posted] = []
    root: str | None = None
    for i, m in enumerate(chat):
        if i:
            gap = (m.event_time - chat[i - 1].event_time).total_seconds()
            await asyncio.sleep(gap / speed)
        out = await client.chat_postMessage(
            channel=channel,
            text=m.text,
            thread_ts=root,
            username=m.author,
            icon_emoji=":construction_worker:" if m.author_role == "Field Engineer" else ":mage:",
        )
        root = root or out["ts"]
        posted.append(Posted(seq=i + 1, ts=out["ts"], author=m.author or "", text=m.text))
    return posted
