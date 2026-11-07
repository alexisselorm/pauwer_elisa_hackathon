from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from kaiku.config import Settings, get_settings

AGENT_TOKEN = "test-token-ops-agent-0123456789"
OTHER_TOKEN = "test-token-other-agent-987654321"

# Every variable Settings reads, so a developer's shell or .env can't leak into tests.
_ENV_VARS = [name.upper() for name in Settings.model_fields]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    """Settings with no .env file, pointing at an unreachable Neo4j unless overridden."""

    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "env": "test",
            "log_json": False,
            "neo4j_uri": "bolt://127.0.0.1:1",  # nothing listens on port 1
            "neo4j_user": "neo4j",
            "neo4j_password": "unused-password",
            "neo4j_connect_timeout_s": 0.5,
            "neo4j_retry_initial_s": 0.05,
            "neo4j_retry_max_s": 0.2,
            "state_dir": tmp_path / "state",
            "mcp_tokens": {"ops-agent": AGENT_TOKEN, "other-agent": OTHER_TOKEN},
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Live tests call real LLMs and post to the real Slack workspace: they run only when
    asked for by name (`-m live`). Any other -m expression would otherwise select them."""
    expr = config.option.markexpr or ""
    if "live" in expr and "not live" not in expr:
        return
    skip = pytest.mark.skip(reason="live test: run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
