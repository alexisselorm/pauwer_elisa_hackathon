"""[Ask] remembers where it asked, so the expert's reply answers the gap (no Slack needed).
The whole loop against Neo4j is in test_integration_gap_answer.py."""

from __future__ import annotations

from typing import Any

from kaiku.slack import actions as a
from tests.test_slack_actions import Client, _ack, _click, _gap_message, _ops


class Gaps:
    """The context layer's side of [Ask], in memory."""

    def __init__(self, known: a.AskThread | None = None, fail: bool = False) -> None:
        self.known, self.fail = known, fail
        self.asked_calls: list[tuple[str, dict[str, Any]]] = []
        self.waiting: list[str] = []

    async def ask_thread(self, gap_id: str, *, asker_message: str) -> a.AskThread | None:
        if self.known is not None:
            self.waiting.append(asker_message)
        return self.known

    async def asked(self, gap_id: str, **kw: Any) -> None:
        if self.fail:
            raise RuntimeError("neo4j down")
        self.asked_calls.append((gap_id, kw))


async def test_ask_records_the_ask_thread_and_who_waits_for_the_answer() -> None:
    client, gaps = Client(), Gaps()
    ops = _ops(gap_channel="CTRANSPORT", gaps=gaps)
    await ops.on_gap(_ack, _click(_gap_message(), a.GAP_ASK), client)  # type: ignore[arg-type]

    [ask] = client.posted
    assert ask["channel"] == "CTRANSPORT"
    assert gaps.asked_calls == [
        (
            "GAP-1",
            {
                "channel": "CTRANSPORT",
                "ts": "9.000001",  # the ask post: its thread's replies answer the gap
                "question": "Kajaani boom gate code?",
                "asker": "U5",
                "asker_message": "CASK:5.000001",  # updated in place with the answer
            },
        )
    ]
    [update] = client.updated
    assert "Asked <@U9>" in update["text"]
    assert update["text"].endswith("The answer will show up here.")


async def test_the_same_gap_is_asked_once() -> None:
    """A second [Ask] on a gap already asked links the existing thread and posts nothing:
    one question, one expert reply."""
    client, gaps = Client(), Gaps(known=a.AskThread(channel="CTRANSPORT", ts="7.000001"))
    ops = _ops(gap_channel="CTRANSPORT", gaps=gaps)
    await ops.on_gap(_ack, _click(_gap_message(), a.GAP_ASK), client)  # type: ignore[arg-type]

    assert client.posted == []
    assert gaps.asked_calls == []
    assert gaps.waiting == ["CASK:5.000001"]  # this asker gets the answer too
    [update] = client.updated
    assert update["text"].startswith("Already asked (<https://x.slack.com/CTRANSPORT/7.000001|")
    assert a.GAP_ASK not in str(update["blocks"])

    client = Client()
    gaps.known = a.AskThread(channel="CTRANSPORT", ts="7.000001", answered=True)
    await ops.on_gap(_ack, _click(_gap_message(), a.GAP_ASK), client)  # type: ignore[arg-type]
    assert client.posted == []
    assert client.updated[0]["text"].startswith("Already answered")


async def test_the_question_is_asked_even_if_recording_it_fails() -> None:
    client = Client()
    ops = _ops(gap_channel="CTRANSPORT", gaps=Gaps(fail=True))
    await ops.on_gap(_ack, _click(_gap_message(), a.GAP_ASK), client)  # type: ignore[arg-type]
    assert len(client.posted) == 1
    assert client.updated[0]["text"].startswith("Asked <@U9>")
    assert "show up here" not in client.updated[0]["text"]


async def test_dont_ask_records_nothing() -> None:
    client, gaps = Client(), Gaps(known=a.AskThread(channel="C", ts="1.0"))
    await _ops(gaps=gaps).on_gap(_ack, _click(_gap_message(), a.GAP_DONT_ASK), client)  # type: ignore[arg-type]
    assert client.posted == []
    assert gaps.waiting == []
    assert gaps.asked_calls == []
