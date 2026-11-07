"""extraction/llm.py: retries with backoff, and FAST falling back to CHAT (no network).

Found live: Featherless answered the FAST model's "Resolved" check with HTTP 200 and
`choices: null`, the OpenAI client raised TypeError twice, and the card waited for the
10-minute idle sweep instead of appearing within seconds."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from silentctx.config import Settings
from silentctx.extraction import llm
from silentctx.extraction.llm import ExtractionError, LlmJudge, ProviderEmptyResponseError, Resolved
from silentctx.llm.chat import ModelRole


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
