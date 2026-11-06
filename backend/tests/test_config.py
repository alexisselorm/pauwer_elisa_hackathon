from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError

from silentctx.config import Settings

REQUIRED = {
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "pw",
    "MCP_TOKENS": '{"ops-agent": "a-token-that-is-long-enough"}',
}


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_reads_required_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, REQUIRED)
    s = Settings(_env_file=None)
    assert s.neo4j_uri == "bolt://localhost:7687"
    assert s.mcp_tokens["ops-agent"].get_secret_value() == "a-token-that-is-long-enough"
    assert s.embedding_dim == 1024
    assert s.demo_profile is True


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_variable_fails_fast(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _set_env(monkeypatch, {k: v for k, v in REQUIRED.items() if k != missing})
    with pytest.raises(ValidationError) as err:
        Settings(_env_file=None)
    assert missing.lower() in str(err.value)


def test_capture_channels_are_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {**REQUIRED, "SLACK_CAPTURE_CHANNELS": "C01, C02 ,,C03"})
    assert Settings(_env_file=None).slack_capture_channels == ["C01", "C02", "C03"]


def test_empty_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {**REQUIRED, "ANTHROPIC_API_KEY": "", "SLACK_BOT_TOKEN": ""})
    s = Settings(_env_file=None)
    assert s.anthropic_api_key is None
    assert s.slack_bot_token is None


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        ("{}", "at least one"),
        ('{"a": "short"}', "at least 16"),
        ('{"a": "same-token-0123456789", "b": "same-token-0123456789"}', "unique"),
        ('{" ": "a-token-that-is-long-enough"}', "blank"),
    ],
)
def test_bad_mcp_tokens_rejected(
    monkeypatch: pytest.MonkeyPatch, tokens: str, message: str
) -> None:
    _set_env(monkeypatch, {**REQUIRED, "MCP_TOKENS": tokens})
    with pytest.raises(ValidationError, match=f"(?i){message}"):
        Settings(_env_file=None)


def test_mcp_tokens_must_be_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {**REQUIRED, "MCP_TOKENS": "ops-agent=token"})
    with pytest.raises(SettingsError, match="mcp_tokens"):
        Settings(_env_file=None)


@pytest.mark.parametrize("uri", ["http://localhost:7474", "localhost:7687", ""])
def test_bad_neo4j_uri_rejected(make_settings: Callable[..., Settings], uri: str) -> None:
    with pytest.raises(ValidationError, match="NEO4J_URI"):
        make_settings(neo4j_uri=uri)


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "anthropic:", ":model"])
def test_model_must_be_provider_colon_model(
    make_settings: Callable[..., Settings], model: str
) -> None:
    with pytest.raises(ValidationError, match="provider:model"):
        make_settings(chat_model=model)


def test_missing_llm_keys(make_settings: Callable[..., Settings]) -> None:
    s = make_settings()
    assert s.missing_llm_keys() == ["ANTHROPIC_API_KEY", "FEATHERLESS_API_KEY"]
    s = make_settings(anthropic_api_key="k", featherless_api_key="k")
    assert s.missing_llm_keys() == []
    s = make_settings(chat_model="openai:gpt-x", fast_model="openai:gpt-y", featherless_api_key="k")
    assert s.missing_llm_keys() == ["OPENAI_API_KEY"]


def test_env_example_lists_every_setting_and_loads() -> None:
    example = Path(__file__).parents[1] / ".env.example"
    names = {
        line.split("=", 1)[0].strip()
        for line in example.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert {f.upper() for f in Settings.model_fields} <= names
    s = Settings(_env_file=example)
    assert s.env == "dev"
    assert s.anthropic_api_key is None  # empty in the example
    assert s.slack_capture_channels == []
    assert list(s.mcp_tokens) == ["ops-agent"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("slack_bot_token", "xapp-wrong-kind"), ("slack_app_token", "xoxb-wrong-kind")],
)
def test_slack_tokens_must_have_the_right_prefix(
    make_settings: Callable[..., Settings], field: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=field.upper()):
        make_settings(**{field: value})


def test_env_example_has_no_inline_comments() -> None:
    # docker compose's env_file keeps "value  # comment" verbatim as the value.
    example = Path(__file__).parents[1] / ".env.example"
    for n, line in enumerate(example.read_text().splitlines(), 1):
        if "=" in line and not line.lstrip().startswith("#"):
            assert " #" not in line.split("=", 1)[1], f".env.example:{n}: {line}"
