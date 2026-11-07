"""Live in the real Slack workspace against the running compose stack (`docker compose up`):
Elisa's thread is replayed into #noc-transport as personas and the real bot must post ONE
context card within 60 s of message 9 (ST-01), bound and recorded as in ST-10 / ST-20.
Opt-in: uv run pytest -m live -s tests/test_live_slack.py"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from dotenv import dotenv_values
from neo4j import AsyncGraphDatabase
from slack_sdk.web.async_client import AsyncWebClient

from kaiku.domain.time import slack_ts_to_datetime
from kaiku.slack.replay import replay_elisa
from tests.capture_helpers import ELISA_DIR, MOCK_DATA

pytestmark = pytest.mark.live
ENV = dotenv_values(MOCK_DATA.parent / "backend" / ".env")
CARD_WAIT_S = 120


def _bot_posts(replies: list[dict[str, Any]], bot_id: str) -> list[dict[str, Any]]:
    """The bot's own messages in the thread (personas carry a username)."""
    return [m for m in replies if m.get("bot_id") == bot_id and not m.get("username")]


async def test_live_slack_one_card_within_60s_of_message_9() -> None:
    if not ENV.get("SLACK_BOT_TOKEN") or not ENV.get("SLACK_CAPTURE_CHANNELS"):
        pytest.skip("Slack tokens are not set in backend/.env")
    channel = str(ENV["SLACK_CAPTURE_CHANNELS"]).split(",")[0]  # #noc-transport
    client = AsyncWebClient(token=str(ENV["SLACK_BOT_TOKEN"]))
    bot_id = (await client.auth_test())["bot_id"]

    posted = await replay_elisa(client, channel, ELISA_DIR, speed=30)
    root, msg8, msg9 = posted[0].ts, posted[7].ts, posted[8].ts
    deadline = time.monotonic() + CARD_WAIT_S
    card: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        replies = (await client.conversations_replies(channel=channel, ts=root))["messages"]
        if mine := _bot_posts(replies, bot_id):
            card = mine[0]
            break
        await asyncio.sleep(2)
    assert card is not None, f"no card within {CARD_WAIT_S} s"
    latency = (slack_ts_to_datetime(card["ts"]) - slack_ts_to_datetime(msg9)).total_seconds()
    blocks = json.dumps(card.get("blocks", []), ensure_ascii=False)
    print(f"\nthread {root}: card {latency:.1f} s after message 9\n{card.get('text')}")
    assert latency <= 60  # ST-01
    for part in ("Captured for", "TRN-042", "Fix", "Not knowledge"):
        assert part in blocks
    # ✅ is offered on an unverified rule. On a graph with gold loaded the capture also
    # contradicts gold ASR-0020/0018 (as gold itself does), so it is disputed and has no ✅.
    assert ("Correct" in blocks) == ("unverified" in blocks)

    await asyncio.sleep(15)  # messages 10-12 have arrived; any second post would show now
    replies = (await client.conversations_replies(channel=channel, ts=root))["messages"]
    assert len(_bot_posts(replies, bot_id)) == 1  # ≤1 bot message in the thread

    pw = ENV.get("NEO4J_PASSWORD") or "silentctx-local-dev"
    async with AsyncGraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", pw)) as d:
        records, _, _ = await d.execute_query(
            "MATCH (t:Thread {thread_id: $tid}) UNWIND t.assertion_ids AS aid "
            "MATCH (a:ContextAssertion {assertion_id: aid}) "
            "RETURN t.status AS status, a.recorded_at AS recorded, a.valid_from AS vf, "
            "a.qualifiers_json AS q, [(a)-[b:ABOUT]->(e) | [e.entity_id, b.scope]] AS binds",
            tid=f"slack:{channel}:{root}",
        )
    assert len(records) == 1, "exactly one assertion from the thread"
    r = records[0]
    q = json.loads(r["q"])
    print(
        "bindings:",
        r["binds"],
        "\nqualifiers:",
        {k: q.get(k) for k in ("action", "avoid", "symptom", "telemetry")},
    )
    assert r["status"] == "captured"
    assert r["recorded"].to_native() == r["vf"].to_native() == slack_ts_to_datetime(msg8)  # ST-20
    assert q["action"] == "bounce_interface"
    assert set(q["avoid"]) >= {"replace_sfp", "reboot_node"}
    binds = {tuple(b) for b in r["binds"]}
    assert ("TRN-042", "observed_on") in binds
    assert {("TNC-1X", "applies_to"), ("Gen1", "applies_to")} <= binds
