"""extraction/llm.py: retries with backoff, and FAST falling back to CHAT (no network).

Found live: Featherless answered the FAST model's "Resolved" check with HTTP 200 and
`choices: null`, the OpenAI client raised TypeError twice, and the card waited for the
10-minute idle sweep instead of appearing within seconds."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from kaiku.config import Settings
from kaiku.extraction import llm
from kaiku.extraction.llm import ExtractionError, LlmJudge, ProviderEmptyResponseError, Resolved
from kaiku.llm.chat import ModelRole


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "BACKOFF_S", 0.0)


class _NullChoices:
    """A chat model whose structured call fails the way the OpenAI client does on
    `choices: null`."""

    def __init__(self) -> None:
        self.calls = 0

    def with_structured_output(self, *_: Any, **__: Any) -> _NullChoices:
        return self

    async def ainvoke(self, _: Any) -> Any:
        self.calls += 1
        raise TypeError("'NoneType' object is not iterable")


class _Answers:
    def __init__(self, parsed: dict[str, Any]) -> None:
        self.parsed = parsed
        self.calls = 0

    def with_structured_output(self, *_: Any, **__: Any) -> _Answers:
        return self

    async def ainvoke(self, _: Any) -> Any:
        self.calls += 1
        return {"parsed": self.parsed, "raw": None, "parsing_error": None}


def _judge(make_settings: Callable[..., Settings], fast: Any, chat: Any) -> LlmJudge:
    j = LlmJudge(make_settings())
    j._fast, j._chat = fast, chat
    return j


async def test_null_choices_are_retried_then_fast_falls_back_to_chat(
    make_settings: Callable[..., Settings],
) -> None:
    fast, chat = _NullChoices(), _Answers({"resolved": True})
    judge = _judge(make_settings, fast, chat)
    assert await judge.resolves("counters moving again", "port up, zero traffic") is True
    assert (fast.calls, chat.calls) == (llm.ATTEMPTS, 1)


async def test_a_transient_failure_is_retried_on_the_same_model(
    make_settings: Callable[..., Settings],
) -> None:
    class Flaky(_Answers):
        async def ainvoke(self, msg: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise TypeError("'NoneType' object is not iterable")
            return {"parsed": self.parsed, "raw": None, "parsing_error": None}

    fast, chat = Flaky({"resolved": False}), _NullChoices()
    judge = _judge(make_settings, fast, chat)
    assert await judge.resolves("on my way", "port up, zero traffic") is False
    assert (fast.calls, chat.calls) == (2, 0)


async def test_chat_has_no_fallback_and_the_error_names_the_cause(
    make_settings: Callable[..., Settings],
) -> None:
    fast, chat = _NullChoices(), _NullChoices()
    judge = _judge(make_settings, fast, chat)
    with pytest.raises(ExtractionError, match="ProviderEmptyResponseError") as exc:
        await judge._call(ModelRole.CHAT, Resolved, "s", "u")
    assert isinstance(exc.value.__cause__, ProviderEmptyResponseError)
    assert (fast.calls, chat.calls) == (0, llm.ATTEMPTS)


async def test_the_judge_sees_what_each_side_is_about(
    make_settings: Callable[..., Settings],
) -> None:
    """C3 (G-01): without scope the judge called a TNC-3X rule a contradiction of Gen1 ones."""
    from kaiku.extraction.models import Quoted, RuleDraft

    seen: list[Any] = []

    class Recording(_Answers):
        async def ainvoke(self, messages: Any) -> Any:
            seen.append(messages)
            return {"parsed": self.parsed, "raw": None, "parsing_error": None}

    judge = _judge(make_settings, _NullChoices(), Recording({"relations": []}))
    draft = RuleDraft(
        statement="On TNC-3X, replace the SFP again",
        topic="forwarding_state_after_sfp_swap",
        action=Quoted(value="replace_sfp", quote="Swap the SFP again"),
    )
    cand = {
        "assertion_id": "ASR-0001",
        "statement": "bounce it",
        "about": [{"entity_id": "TNC-1X", "scope": "applies_to"}],
    }
    mine = [{"entity_id": "TRN-008", "scope": "observed_on"}]
    await judge.relate(draft, [cand], "", scope=mine)
    user = seen[0][1][1]
    assert 'About: {"observed_on": ["TRN-008"]}' in user
    earlier = json.loads(user.split("Earlier:\n", 1)[1])
    assert earlier[0]["about"] == {"applies_to": ["TNC-1X"]}
