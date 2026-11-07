"""The ops agent's interactive side in Slack (SL-2, plan 002 §3.4).

- Proactive: the simulator's fault events become a post in the alarm channel, with the
  agent's advice in its thread and outcome buttons [Run bounce (simulated)]
  [Done manually ✓] [Didn't work ✗]. Each click reports the outcome through MCP.
- Not documented: [Ask] [Don't ask] under an answer that opened a knowledge gap. Ask posts
  the question to the expert the gap was routed to.

The watcher triggers on OPTICAL_POWER_OK, the last of a fault's point events: module in,
link up, optics fine, so from here on a stall is silent unless someone looks at counters.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import aiohttp
from slack_sdk.web.async_client import AsyncWebClient

from silentctx.agents.ops_agent import AnswerStatus, OpsAnswer
from silentctx.logging import get_logger
from silentctx.slack.format import (
    actions,
    button,
    context,
    format_answer,
    replace_actions,
    text_blocks,
)

log = get_logger(__name__)

TRIGGER = "OPTICAL_POWER_OK"
ALARM_NAME = "noc-alarms"
POLL_S = 3.0
OUTCOME_BLOCK = "ops_outcome"
GAP_BLOCK = "ops_gap"
RUN_BOUNCE, DONE_MANUALLY, DIDNT_WORK = "ops_run_bounce", "ops_done_manually", "ops_didnt_work"
GAP_ASK, GAP_DONT_ASK = "ops_gap_ask", "ops_gap_dont_ask"
TRAFFIC_MBPS = 0.01  # the "traffic zero" threshold of the telemetry spec


class Answerer(Protocol):
    async def ask(
        self, question: str, *, request_id: str, asker: str | None = None
    ) -> OpsAnswer: ...


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class Network(Protocol):
    async def events(self) -> list[dict[str, Any]]: ...

    async def bounce(self, node: str, port: str) -> None: ...

    async def traffic_flowing(self, node: str, port: str) -> bool | None: ...


class SimNetwork:
    """The network simulator over HTTP (SIM_URL)."""

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=10)

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        async with (
            aiohttp.ClientSession(timeout=self._timeout) as http,
            http.request(method, f"{self.base}{path}", **kw) as resp,
        ):
            resp.raise_for_status()
            return await resp.json()

    async def events(self) -> list[dict[str, Any]]:
        return list(await self._request("GET", "/sim/events"))

    async def bounce(self, node: str, port: str) -> None:
        await self._request(
            "POST",
            "/sim/action",
            json={"node": node, "port": port, "action": "bounce_interface"},
        )

    async def traffic_flowing(self, node: str, port: str) -> bool | None:
        """From the fine live series: is in-traffic above zero right now?"""
        points = await self._request(
            "GET", "/sim/live", params={"node": node, "port": port, "minutes": 0.25, "step_s": 5}
        )
        if not points:
            return None
        return float(points[-1]["in_octets_mbps"]) >= TRAFFIC_MBPS


def _value(body: dict[str, Any]) -> dict[str, Any]:
    return dict(json.loads(body["actions"][0]["value"]))


def answer_blocks(
    answer: OpsAnswer, *, fault: dict[str, Any] | None = None, question: str | None = None
) -> list[dict[str, Any]]:
    """The answer, plus the buttons that fit it: outcome buttons when it advises an action
    on a faulty port, Ask / Don't ask when it opened a knowledge gap."""
    blocks = text_blocks(format_answer(answer))
    if fault is not None and answer.steps:
        value = {
            "node": fault["ne_id"],
            "port": fault["port"],
            "assertion_id": answer.steps[0].assertion_ids[0],
        }
        blocks.append(
            actions(
                OUTCOME_BLOCK,
                button("Run bounce (simulated)", RUN_BOUNCE, value, "primary"),
                button("Done manually ✓", DONE_MANUALLY, value),
                button("Didn't work ✗", DIDNT_WORK, value, "danger"),
            )
        )
    if answer.status is AnswerStatus.NOT_DOCUMENTED and answer.gap_id:
        who = answer.gap_routed_to or "an expert"
        value = {
            "gap_id": answer.gap_id,
            "routed_to": answer.gap_routed_to,
            "question": (question or answer.summary)[:1500],
        }
        blocks.append(context(f"I found no record of this. Shall I ask *{who}*?"))
        blocks.append(
            actions(
                GAP_BLOCK,
                button(f"Ask {who}", GAP_ASK, value, "primary"),
                button("Don't ask", GAP_DONT_ASK, value),
            )
        )
    return blocks


class OpsActions:
    def __init__(
        self,
        answerer: Answerer,
        tools: ToolCaller,
        *,
        network: Network | None = None,
        alarm_channel: str | None = None,
        gap_channel: str | None = None,
        capture_channels: Sequence[str] = (),
        settle: timedelta = timedelta(seconds=40),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.answerer, self.tools, self.network = answerer, tools, network
        self.alarm_channel, self.gap_channel = alarm_channel, gap_channel
        self.capture_channels = list(capture_channels)
        self.settle, self.clock = settle, clock
        self._target: str | None = None
        self._watch_ok = True
        self._seen: set[str] | None = None
        self._people: dict[str, str] | None = None

    # --- Proactive ------------------------------------------------------------------------

    async def alarm_target(self, client: AsyncWebClient) -> str | None:
        """Where faults are posted. The bot watches the network either way; this only picks
        the channel: SLACK_ALARM_CHANNEL, else a channel named #noc-alarms (joined if
        needed), else the first capture channel."""
        if self._target is None:
            self._target = self.alarm_channel or await self._find_channel(client, ALARM_NAME)
            if self._target is None and self.capture_channels:
                self._target = self.capture_channels[0]
        return self._target

    async def _find_channel(self, client: AsyncWebClient, name: str) -> str | None:
        cursor = None
        while True:
            page = await client.conversations_list(
                types="public_channel", exclude_archived=True, limit=200, cursor=cursor
            )
            channels: list[dict[str, Any]] = page.get("channels", [])
            for ch in channels:
                if ch.get("name") == name:
                    if not ch.get("is_member"):
                        await client.conversations_join(channel=ch["id"])
                    return str(ch["id"])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return None

    async def poll_once(self, client: AsyncWebClient) -> int:
        """Post every new fault. The first poll only learns what already happened, so a
        restart doesn't re-post old faults. Returns the number posted."""
        assert self.network is not None
        events = await self.network.events()
        if self._seen is None:
            self._seen = {e["event_id"] for e in events}
            return 0
        posted = 0
        for ev in events:
            if ev["event_id"] in self._seen:
                continue
            self._seen.add(ev["event_id"])
            if ev.get("type") == TRIGGER:
                await self.post_fault(client, ev)
                posted += 1
        return posted

    async def watch(self, client: AsyncWebClient) -> None:
        log.info("fault_watch_started")
        while True:
            try:
                await self.poll_once(client)
                if not self._watch_ok:
                    log.info("fault_watch_recovered")
                self._watch_ok = True
            except Exception as exc:  # noqa: BLE001  # sim restarting: try again next round
                if self._watch_ok:  # once per outage, not every 3 s
                    log.warning("fault_watch_failed", error=f"{type(exc).__name__}: {exc}")
                self._watch_ok = False
            await asyncio.sleep(POLL_S)

    async def post_fault(self, client: AsyncWebClient, ev: dict[str, Any]) -> None:
        node, port = ev["ne_id"], ev["port"]
        channel = await self.alarm_target(client)
        if channel is None:
            log.warning(
                "fault_seen_nowhere_to_post",
                node=node,
                port=port,
                event_id=ev["event_id"],
                hint="create #noc-alarms, or set SLACK_ALARM_CHANNEL",
            )
            return
        at = datetime.fromisoformat(ev["event_time"])
        head = await client.chat_postMessage(
            channel=channel,
            text=(
                f":rotating_light: *{node} {port}*: optical module inserted · link up · "
                f"optics OK ({at:%H:%M} UTC). Checking for a silent stall…"
            ),
        )
        reply = await client.chat_postMessage(
            channel=channel,
            thread_ts=head["ts"],
            text=":hourglass_flowing_sand: Checking conditions and documented knowledge…",
        )
        question = (
            f"Alarm on {node} port {port}: an optical module was inserted, the link came up and "
            f"optical power is OK (at {ev['event_time']}). Check the conditions on {node} port "
            f"{port} and say what to do next."
        )
        try:
            answer = await self.answerer.ask(question, request_id=f"sim:{ev['event_id']}")
            text, blocks = format_answer(answer), answer_blocks(answer, fault=ev)
        except Exception as exc:  # the alarm thread always gets a reply
            log.exception("proactive_answer_failed", event_id=ev["event_id"])
            text = f":warning: I couldn't check this fault ({type(exc).__name__})."
            blocks = text_blocks(text)
        await client.chat_update(channel=channel, ts=reply["ts"], text=text, blocks=blocks)
        log.info("fault_posted", node=node, port=port, event_id=ev["event_id"])

    # --- Outcome buttons ------------------------------------------------------------------

    async def on_outcome(self, ack: Any, body: dict[str, Any], client: AsyncWebClient) -> None:
        await ack()
        action_id = body["actions"][0]["action_id"]
        v = _value(body)
        channel, ts = body["channel"]["id"], body["message"]["ts"]
        blocks = body["message"].get("blocks", [])
        user = body["user"]["id"]
        where = f"{v['node']} {v['port']}"

        async def show(note: str) -> None:
            await client.chat_update(
                channel=channel,
                ts=ts,
                text=note,
                blocks=replace_actions(blocks, OUTCOME_BLOCK, note),
            )

        acted_at = self.clock()
        evidence: str | None = None
        if action_id == RUN_BOUNCE:
            if self.network is None:
                await show(":warning: No network simulator is connected (SIM_URL).")
                return
            await show(f":arrows_counterclockwise: Bouncing {where} for <@{user}>…")
            await self.network.bounce(v["node"], v["port"])
            await asyncio.sleep(self.settle.total_seconds())
            flowing = await self.network.traffic_flowing(v["node"], v["port"])
            result = {True: "resolved", False: "not_resolved", None: "unknown"}[flowing]
            evidence = f"sim live counters {where}"
        else:
            result = "resolved" if action_id == DONE_MANUALLY else "not_resolved"
        try:
            out = await self.tools.call_tool(
                "report_outcome",
                {
                    "entity_id": v["node"],
                    "port": v["port"],
                    "action": "bounce_interface",
                    "result": result,
                    "assertion_id": v["assertion_id"],
                    "observed_at": acted_at.isoformat(),
                    "evidence_ref": evidence,
                    "on_behalf_of": user,
                    "idempotency_key": f"slack:{channel}:{ts}",
                },
            )
        except Exception as exc:
            log.exception("report_outcome_failed", channel=channel, ts=ts)
            await show(f":warning: Couldn't record the outcome ({type(exc).__name__}).")
            return
        await show(_outcome_note(action_id, result, where, user, out))

    # --- Not documented: ask the expert ---------------------------------------------------

    async def on_gap(self, ack: Any, body: dict[str, Any], client: AsyncWebClient) -> None:
        await ack()
        action_id = body["actions"][0]["action_id"]
        v = _value(body)
        channel, ts = body["channel"]["id"], body["message"]["ts"]
        blocks = body["message"].get("blocks", [])
        user = body["user"]["id"]
        if action_id == GAP_DONT_ASK:
            note = f"Not asked (<@{user}>). Knowledge gap {v['gap_id']} stays open."
        else:
            expert = await self._slack_user(client, v.get("routed_to"))
            target = self.gap_channel or channel
            who = f"<@{expert}>" if expert else (v.get("routed_to") or "Anyone")
            posted = await client.chat_postMessage(
                channel=target,
                text=(
                    f":question: {who}, <@{user}> asked something that isn't documented yet:\n"
                    f"> {v['question']}\nA reply in this thread is all it takes "
                    f"(knowledge gap {v['gap_id']})."
                ),
            )
            link = await client.chat_getPermalink(channel=target, message_ts=posted["ts"])
            note = f"Asked {who} (<{link['permalink']}|thread>) for <@{user}>."
        await client.chat_update(
            channel=channel, ts=ts, text=note, blocks=replace_actions(blocks, GAP_BLOCK, note)
        )

    async def _slack_user(self, client: AsyncWebClient, person: str | None) -> str | None:
        """Our person ids are first names ('erkki'); match them to workspace members."""
        if not person:
            return None
        if self._people is None:
            people: dict[str, str] = {}
            cursor = None
            while True:
                page = await client.users_list(cursor=cursor, limit=200)
                members: list[dict[str, Any]] = page.get("members", [])
                for m in members:
                    if m.get("deleted") or m.get("is_bot"):
                        continue
                    prof = m.get("profile", {})
                    for name in (m.get("name"), prof.get("display_name"), prof.get("real_name")):
                        if name:
                            people.setdefault(name.split()[0].casefold(), m["id"])
                cursor = (page.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
            self._people = people
        return self._people.get(person.casefold())


def _outcome_note(action_id: str, result: str, where: str, user: str, out: dict[str, Any]) -> str:
    counts = f"support {out.get('support_n', 0)} · refute {out.get('refute_n', 0)}"
    if action_id == RUN_BOUNCE:
        if result == "resolved":
            return f":white_check_mark: Traffic is back: network-confirmed on {where} · {counts}"
        if result == "not_resolved":
            return f":x: Bounced {where}, counters still at zero. Recorded · {counts}"
        return f":grey_question: Bounced {where}; no telemetry to confirm it. Recorded as unknown."
    if result == "resolved":
        return f":white_check_mark: Done manually by <@{user}> on {where} · {counts}"
    return f":x: <@{user}>: didn't work on {where}. Recorded · {counts}"
