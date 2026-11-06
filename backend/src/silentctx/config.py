"""Settings, read from the environment and `.env` (plan 001 WS0).

Required variables (NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, MCP_TOKENS) have no default,
so a missing one fails at startup instead of at the first request.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Provider prefix of a `provider:model` string -> the settings field holding its key.
_PROVIDER_KEY_FIELD: dict[str, str] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "featherless": "featherless_api_key",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # `ANTHROPIC_API_KEY=` in .env means "not set", not an empty key.
        env_ignore_empty=True,
    )

    # --- Runtime -----------------------------------------------------------------------
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # --- Neo4j (required) --------------------------------------------------------------
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: SecretStr
    neo4j_database: str = "neo4j"
    neo4j_connect_timeout_s: float = Field(default=5.0, gt=0)
    # Backoff between connection attempts while Neo4j is down (no crash-loop).
    neo4j_retry_initial_s: float = Field(default=1.0, gt=0)
    neo4j_retry_max_s: float = Field(default=30.0, gt=0)

    # --- LLMs (optional: missing keys degrade /readyz, they don't stop the app) -------
    chat_model: str = "anthropic:claude-sonnet-5-5"
    fast_model: str = "anthropic:claude-haiku-5-5"
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    # --- Embeddings (Featherless, OpenAI-compatible) ----------------------------------
    embedding_base_url: str = "https://api.featherless.ai/v1"
    embedding_model: str = "Qwen/Qwen3-Embedding-8B"
    embedding_dim: int = Field(default=1024, ge=32, le=4096)
    embedding_max_concurrency: int = Field(default=2, ge=1)
    embedding_batch_size: int = Field(default=32, ge=1, le=2048)
    embedding_timeout_s: float = Field(default=30.0, gt=0)
    featherless_api_key: SecretStr | None = None

    # --- Data and state ------------------------------------------------------------------
    data_dir: Path = Path("../mock_data")
    state_dir: Path = Path(".state")
    # True: the demo profile (plan 001 §3.5). False: the full backfill, used for the T1 eval.
    demo_profile: bool = True

    # --- Slack (optional for the API; required by the slackbot process) ---------------
    slack_bot_token: SecretStr | None = None
    slack_app_token: SecretStr | None = None
    slack_capture_channels: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- MCP (required): agent_id -> bearer token, as JSON ------------------------------
    mcp_tokens: dict[str, SecretStr]

    @field_validator("neo4j_uri")
    @classmethod
    def _check_neo4j_uri(cls, v: str) -> str:
        schemes = ("bolt://", "bolt+s://", "bolt+ssc://", "neo4j://", "neo4j+s://", "neo4j+ssc://")
        if not v.startswith(schemes):
            raise ValueError(f"NEO4J_URI must start with one of {', '.join(schemes)}")
        return v

    @field_validator("slack_capture_channels", mode="before")
    @classmethod
    def _split_channels(cls, v: object) -> object:
        # Comma-separated in the environment: SLACK_CAPTURE_CHANNELS=C01,C02
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

    @field_validator("slack_bot_token", "slack_app_token")
    @classmethod
    def _check_slack_token(cls, v: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        name = info.field_name or ""
        prefix = "xoxb-" if name == "slack_bot_token" else "xapp-"
        if v is not None and not v.get_secret_value().startswith(prefix):
            raise ValueError(f"{name.upper()} must start with {prefix}")
        return v

    @field_validator("mcp_tokens")
    @classmethod
    def _check_mcp_tokens(cls, v: dict[str, SecretStr]) -> dict[str, SecretStr]:
        if not v:
            raise ValueError("MCP_TOKENS needs at least one agent_id -> token entry")
        secrets = [t.get_secret_value() for t in v.values()]
        if any(len(s) < 16 for s in secrets):
            raise ValueError("every MCP token must be at least 16 characters")
        if len(set(secrets)) != len(secrets):
            raise ValueError("MCP tokens must be unique, so each write is attributable")
        if any(not agent_id.strip() for agent_id in v):
            raise ValueError("MCP agent_id must not be blank")
        return v

    @field_validator("chat_model", "fast_model")
    @classmethod
    def _check_model_string(cls, v: str) -> str:
        provider, sep, model = v.partition(":")
        if not sep or not provider or not model:
            raise ValueError(
                "model must be a 'provider:model' string, e.g. anthropic:claude-sonnet-5-5"
            )
        if provider not in _PROVIDER_KEY_FIELD:
            raise ValueError(f"provider must be one of {', '.join(sorted(_PROVIDER_KEY_FIELD))}")
        return v

    @property
    def checkpoint_db(self) -> Path:
        return self.state_dir / "checkpoints.sqlite"

    def missing_llm_keys(self) -> list[str]:
        """Env var names of keys the configured chat/embedding models need but don't have."""
        missing: list[str] = []
        for model in (self.chat_model, self.fast_model):
            provider = model.partition(":")[0]
            key_field = _PROVIDER_KEY_FIELD.get(provider)
            if key_field and getattr(self, key_field) is None:
                missing.append(key_field.upper())
        if self.featherless_api_key is None:
            missing.append("FEATHERLESS_API_KEY")
        return sorted(set(missing))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment; missing ones raise
