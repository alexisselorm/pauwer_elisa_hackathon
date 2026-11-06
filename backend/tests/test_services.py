"""The slackbot and simulator processes (no Slack connection needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from silentctx.config import Settings
from silentctx.sim.app import create_app as create_sim_app
from silentctx.slack import bot


def test_sim_health() -> None:
    with TestClient(create_sim_app()) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert len(r.headers["x-request-id"]) == 32


async def test_slackbot_without_tokens_idles_instead_of_crashing(
    make_settings: Callable[..., Settings],
) -> None:
    task = asyncio.create_task(bot.run(make_settings()))
    await asyncio.sleep(0.1)
    assert not task.done()  # still running: no crash-loop under `restart: unless-stopped`
    assert not bot.READY_FILE.exists()  # healthcheck stays red
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
