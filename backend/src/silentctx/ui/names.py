"""Slack ids -> names for the console ("U0CF…" -> "Erkki Expert", "C0CF…" -> "#noc-transport").

Uses the bot token the slackbot already has; without it, ids stay as they are. Results are
cached for the life of the process (names rarely change during a demo).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from silentctx.logging import get_logger

log = get_logger(__name__)

_USER = re.compile(r"^[UW][A-Z0-9]{6,}$")
_CHANNEL = re.compile(r"^[CGD][A-Z0-9]{6,}$")


class SlackNames:
    def __init__(self, token: str | None) -> None:
        self._client = AsyncWebClient(token=token) if token else None
        self._cache: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def user(self, user_id: str | None) -> str | None:
        if not user_id or self._client is None or not _USER.match(user_id):
            return user_id
        return await self._lookup(user_id, self._user)

    async def channel(self, channel_id: str | None) -> str | None:
        if not channel_id or self._client is None or not _CHANNEL.match(channel_id):
            return channel_id
        return await self._lookup(channel_id, self._channel)

    async def _lookup(self, key: str, fetch: Callable[[str], Awaitable[str]]) -> str:
        if key in self._cache:
            return self._cache[key]
        async with self._lock:
            if key not in self._cache:
                try:
                    self._cache[key] = await fetch(key)
                except (SlackApiError, OSError) as exc:
                    log.warning("slack_name_lookup_failed", key=key, error=str(exc))
                    return key  # not cached: retried on the next request
        return self._cache[key]

    async def _user(self, user_id: str) -> str:
        assert self._client is not None
        r = await self._client.users_info(user=user_id)
        u = r.get("user") or {}
        profile = u.get("profile") or {}
        return str(profile.get("real_name") or u.get("real_name") or u.get("name") or user_id)

    async def _channel(self, channel_id: str) -> str:
        assert self._client is not None
        r = await self._client.conversations_info(channel=channel_id)
        name = (r.get("channel") or {}).get("name")
        return f"#{name}" if name else channel_id
