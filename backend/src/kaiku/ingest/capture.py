"""The proactive capture loop (plan 001 WS2, BE-2.4). Plain code; the LLM only judges.

Every `CAPTURE_SWEEP_SECONDS` the sweeper triages threads with a message newer than their
last triage that are resolved or idle for `CAPTURE_IDLE_MINUTES`. A resolution signal
("Traffic is back 👍", ✅) kicks a sweep at once, so the card appears within seconds (ST-01).
Each decision is remembered on the Thread, so a thread with no new message costs nothing.

Due threads run a few at a time (`concurrency`). A thread named by the kick is processed
first, so a live resolution is never queued behind a backfill backlog. A failed extraction
(model outage, a transient write failure) is retried with exponential backoff per thread,
1, 2, 4 ... minutes up to `BACKOFF_MAX`, instead of at the very next tick.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from kaiku.domain.time import now_utc
from kaiku.extraction.graph import CaptureResult, Extractor
from kaiku.ingest.store import CaptureStore, ThreadView
from kaiku.logging import get_logger

log = get_logger(__name__)

Announce = Callable[[ThreadView, CaptureResult], Awaitable[None]]

BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_MAX = timedelta(minutes=30)


def backoff(retry_n: int) -> timedelta:
    """How long to wait after the (retry_n + 1)th failure in a row: 1, 2, 4 ... minutes,
    capped at BACKOFF_MAX."""
    wait: timedelta = BACKOFF_BASE * (2 ** max(retry_n, 0))
    return min(wait, BACKOFF_MAX)


def retryable(result: CaptureResult) -> bool:
    """A failed run that is worth another attempt later: an LLM outage, an exception in
    the pipeline, a transient persist failure. A write the graph rejected on its temporal
    checks (V5) is deterministic and stands until a new message arrives."""
    return result.status == "extraction_failed" and not result.reason.startswith("write rejected")


class CaptureLoop:
    def __init__(
        self,
        store: CaptureStore,
        extractor: Extractor,
        announce: Announce | None = None,
        *,
        idle: timedelta = timedelta(minutes=10),
        every_s: float = 60.0,
        concurrency: int = 4,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.store = store
        self.extractor = extractor
        self.announce = announce
        self.idle = idle
        self.every_s = every_s
        self.concurrency = max(1, concurrency)
        self.clock = clock
        self._kick = asyncio.Event()
        self._first: list[str] = []  # threads a kick named, processed ahead of the rest

    def kick(self, thread_id: str | None = None) -> None:
        """A resolution signal arrived: sweep now instead of at the next tick. With a
        thread id, that thread goes first in the sweep."""
        if thread_id and thread_id not in self._first:
            self._first.append(thread_id)
        self._kick.set()

    async def sweep_once(self) -> list[CaptureResult]:
        due = await self.store.due_threads(self.clock(), self.idle)
        first, self._first = [t for t in self._first if t in due], []
        order = first + [t for t in due if t not in first]
        gate = asyncio.Semaphore(self.concurrency)

        async def one(thread_id: str) -> CaptureResult:
            async with gate:
                return await self.process(thread_id)

        return list(await asyncio.gather(*(one(t) for t in order)))

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
        corrected = [i.corrected for i in result.items if i.corrected]
        retry = retryable(result)
        wait = backoff(thread.retry_n) if retry else None
        # A retried thread stays due (nothing seen) but waits out its backoff first; any
        # other decision stands until a new message arrives.
        ids = await self.store.set_triage(
            thread_id,
            status="captured"
            if result.assertion_ids and result.status != "extraction_failed"
            else result.status,
            kind=result.kind,
            reason=result.reason,
            seen_up_to=datetime.min.replace(tzinfo=UTC) if retry else thread.last_message_ts,
            add=result.assertion_ids,
            remove=corrected,
            capture_json=result.model_dump_json(),
            retry_n=thread.retry_n + 1 if retry else 0,
            retry_after=self.clock() + wait if wait is not None else None,
        )
        if not ids and result.status == "captured":
            # Nothing kept after the merge: the run's versions were retracted meanwhile.
            await self.store.set_triage(
                thread_id,
                status="ignored",
                kind=result.kind,
                reason=result.reason,
                seen_up_to=thread.last_message_ts,
            )
        log.info(
            "thread_triaged",
            thread_id=thread_id,
            status=result.status,
            kind=result.kind,
            reason=result.reason,
            assertions=result.assertion_ids,
            retry_in_s=wait.total_seconds() if wait is not None else None,
        )
        if self.announce is not None:
            try:
                await self.announce(thread.model_copy(update={"assertion_ids": ids}), result)
            except Exception:
                log.exception("announce_failed", thread_id=thread_id)
        return result

    async def run(self) -> None:
        while True:
            # Cleared before the sweep, not after: a resolution that arrives while a sweep
            # runs (the fix lands mid-triage) must start the next sweep at once, not wait
            # `every_s`.
            self._kick.clear()
            try:
                await self.sweep_once()
            except Exception:
                log.exception("sweep_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), self.every_s)
