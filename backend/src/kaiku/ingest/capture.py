"""The proactive capture loop (plan 001 WS2, BE-2.4). Plain code; the LLM only judges.

Every `CAPTURE_SWEEP_SECONDS` the sweeper triages threads with a message newer than their
last triage that are resolved or idle for `CAPTURE_IDLE_MINUTES`. A resolution signal
("Traffic is back 👍", ✅) kicks a sweep at once, so the card appears within seconds (ST-01).
Each decision is remembered on the Thread, so a thread with no new message costs nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from kaiku.extraction.graph import CaptureResult, Extractor
from kaiku.ingest.store import CaptureStore, ThreadView
from kaiku.logging import get_logger

log = get_logger(__name__)

Announce = Callable[[ThreadView, CaptureResult], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


class CaptureLoop:
    def __init__(
        self,
        store: CaptureStore,
        extractor: Extractor,
        announce: Announce | None = None,
        *,
        idle: timedelta = timedelta(minutes=10),
        every_s: float = 60.0,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.announce = announce
        self.idle = idle
        self.every_s = every_s
        self.clock = clock
        self._kick = asyncio.Event()

    def kick(self) -> None:
        """A resolution signal arrived: sweep now instead of at the next tick."""
        self._kick.set()

    async def sweep_once(self) -> list[CaptureResult]:
        results = []
        for thread_id in await self.store.due_threads(self.clock(), self.idle):
            results.append(await self.process(thread_id))
        return results

    async def process(self, thread_id: str) -> CaptureResult:
        thread = await self.store.thread(thread_id)
        try:
            result = await self.extractor.run(thread)
        except Exception as exc:  # one bad thread must not stop the loop
            log.exception("capture_failed", thread_id=thread_id)
            result = CaptureResult(
                thread_id=thread_id,
                status="extraction_failed",
                kind="none",
                reason=f"{type(exc).__name__}: {exc}"[:300],
            )
        corrected = {i.corrected for i in result.items if i.corrected}
        ids = [i for i in thread.assertion_ids if i not in corrected]
        ids += [i for i in result.assertion_ids if i not in ids]
        # An LLM outage is retried on the next sweep (ST-60); any other decision stands
        # until a new message arrives.
        retry = result.status == "extraction_failed" and result.reason.startswith("the model")
        await self.store.set_triage(
            thread_id,
            status="captured" if ids and result.status != "extraction_failed" else result.status,
            kind=result.kind,
            reason=result.reason,
            seen_up_to=datetime.min.replace(tzinfo=UTC) if retry else thread.last_message_ts,
            assertion_ids=ids,
            capture_json=result.model_dump_json(),
        )
        log.info(
            "thread_triaged",
            thread_id=thread_id,
            status=result.status,
            kind=result.kind,
            reason=result.reason,
            assertions=result.assertion_ids,
        )
        if self.announce is not None:
            try:
                await self.announce(thread.model_copy(update={"assertion_ids": ids}), result)
            except Exception:
                log.exception("announce_failed", thread_id=thread_id)
        return result

    async def run(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("sweep_failed")
            self._kick.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), self.every_s)
