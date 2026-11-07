"""The outcome verifier: the network confirms (or not) that following the advice worked
(plan 001 §6.2).

An `Outcome` event says an action ran on an entity at `t_action`, following an assertion's
advice. The verifier looks at the telemetry of that port in [t_action, t_action + 30 min]:
  - the counters recover (traffic flowing with the port UP)   -> resolved
  - they stay at zero until the window has closed             -> not_resolved
  - the window has closed with no telemetry at all            -> unknown
  - the window is still open and nothing has recovered yet    -> pending (checked again)

It also records whether the assertion's conditions held just before the action. The
repository turns the verdict into evidence (`verify_outcome`): a recovery adds SUPPORTS,
puts the entity in `observed_on` and makes unverified knowledge believed_true, with no
human click; a failure counts against the rule only if its conditions were met.

Two telemetry sources, read together: the 15-minute PM samples (DuckDB, history and the
simulator's live table), using only intervals that lie wholly inside the window, and the
simulator's fine series, which shows a recovery seconds after a bounce. The fine series is
what lets the live demo confirm a bounce while the person is still looking at the thread.

Trigger: `report_outcome` verifies the new outcome at once. An outcome the network can't
judge yet stays `pending`, and `run_forever` (started by the api) re-checks pending outcomes
every minute until the window closes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from kaiku.context.models import AssertionView
from kaiku.domain.time import PM_INTERVAL, ensure_utc
from kaiku.graph.repository import ContextRepository
from kaiku.logging import get_logger
from kaiku.telemetry.store import (
    TRAFFIC_ZERO_MBPS,
    ConditionResult,
    ConditionStatus,
    TelemetryStore,
)

log = get_logger(__name__)

VERIFY_WINDOW = timedelta(minutes=30)
SWEEP_INTERVAL_S = 60.0

Verdict = Literal["resolved", "not_resolved", "unknown", "pending"]

# The effect the verifier knows how to observe: an assertion about a traffic stall (its
# conditions include zero counters) expects the counters to recover after its action.
_TRAFFIC_STALL = "counters_zero"


def beta_confidence(support_n: int, refute_n: int) -> float:
    """The mean of Beta(1 + support_n, 1 + refute_n): 0.5 with no evidence, 2/3 after one
    success. The repository stores the same formula on every evidence write."""
    return (1 + support_n) / (2 + support_n + refute_n)


def evidence_line(view: AssertionView) -> str:
    """The evidence as the card shows it: "network-confirmed on TRN-017 · support 1 ·
    confidence 0.67". Empty while there is no evidence."""
    if not (view.support_n or view.refute_n or view.network_confirmed_on):
        return ""
    parts = []
    if view.network_confirmed_on:
        parts.append("network-confirmed on " + ", ".join(view.network_confirmed_on))
    parts.append(f"support {view.support_n}")
    if view.refute_n:
        parts.append(f"refute {view.refute_n}")
    parts.append(f"confidence {beta_confidence(view.support_n, view.refute_n):.2f}")
    return " · ".join(parts)


@dataclass(frozen=True)
class Observation:
    """What the port did over [start, end]: a PM interval, or one instant of the fine
    series (start == end)."""

    start: datetime
    end: datetime
    oper_status: str
    in_mbps: float

    @property
    def flowing(self) -> bool:
        return self.oper_status == "UP" and self.in_mbps >= TRAFFIC_ZERO_MBPS


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict
    detail: str


def judge(
    observations: Sequence[Observation],
    t_action: datetime,
    now: datetime,
    window: timedelta = VERIFY_WINDOW,
) -> Judgement:
    """The verdict from what the port did after the action. Only observations wholly
    inside [t_action, t_action + window] count: an interval that began before the action
    mixes before and after."""
    end = t_action + window
    seen = sorted(
        (o for o in observations if o.start >= t_action and o.end <= end), key=lambda o: o.end
    )
    flowing = [o for o in seen if o.flowing]
    if flowing:
        first = flowing[0]
        return Judgement(
            "resolved",
            f"traffic back by {first.end:%H:%M:%S} UTC ({first.in_mbps:.2f} Mbps, port UP)",
        )
    if now < end:
        return Judgement("pending", f"no recovery seen yet; watching until {end:%H:%M} UTC")
    if seen:
        return Judgement(
            "not_resolved",
            f"counters stayed at zero from {t_action:%H:%M} to {end:%H:%M} UTC "
            f"({len(seen)} observation(s))",
        )
    return Judgement("unknown", f"no telemetry from {t_action:%H:%M} to {end:%H:%M} UTC")


class OutcomeVerifier:
    def __init__(
        self,
        repo: ContextRepository,
        telemetry: TelemetryStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        window: timedelta = VERIFY_WINDOW,
    ) -> None:
        self.repo, self.telemetry = repo, telemetry
        self.clock, self.window = clock, window

    async def verify(self, outcome_id: str) -> dict[str, Any]:
        """Judge one outcome and record the verdict. Returns the outcome's state (see
        `ContextRepository.outcome_state`). A final verdict is never revisited."""
        o = await self.repo.outcome_to_verify(outcome_id)
        if o is None:
            raise LookupError(f"unknown outcome {outcome_id!r}")
        if o["verification"] != "pending" or not o["assertion_id"] or not o["entity_id"]:
            return await self.repo.outcome_state(outcome_id)
        entity_id, t_action = o["entity_id"], ensure_utc(o["t_action"])
        symptoms = [str(s) for s in json.loads(o["qualifiers_json"] or "{}").get("symptom", [])]
        port: str | None = o["port"]
        met: bool | None = None
        if _TRAFFIC_STALL not in symptoms:
            verdict = Judgement("unknown", "no telemetry check for this advice's effect")
        else:
            await self.telemetry.refresh(entity_id)
            port, note = await self._port(entity_id, port)
            if port is None:
                verdict = Judgement("unknown", note)
            else:
                observed = await self._observations(entity_id, port, t_action)
                verdict = judge(observed, t_action, self.clock(), self.window)
                if o["conditions_met"] is None:
                    met = conditions_met(
                        await self.telemetry.acheck(entity_id, port, symptoms, t_action)
                    )
        log.info(
            "outcome_verified",
            outcome_id=outcome_id,
            entity_id=o["entity_id"],
            port=port,
            verdict=verdict.verdict,
            detail=verdict.detail,
        )
        return await self.repo.verify_outcome(
            outcome_id,
            verdict=verdict.verdict,
            detail=verdict.detail,
            checked_at=self.clock(),
            conditions_met=met,
        )

    async def sweep(self) -> int:
        """Re-check every pending outcome. Returns how many got a final verdict."""
        done = 0
        for outcome_id in await self.repo.outcomes_to_verify():
            try:
                state = await self.verify(outcome_id)
            except Exception as exc:  # noqa: BLE001  # one bad outcome doesn't stop the rest
                log.warning(
                    "outcome_verify_failed",
                    outcome_id=outcome_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            done += state["verification"] != "pending"
        return done

    async def run_forever(self, ready: asyncio.Event, every_s: float = SWEEP_INTERVAL_S) -> None:
        await ready.wait()
        while True:
            try:
                await self.sweep()
            except Exception as exc:  # noqa: BLE001  # Neo4j away: retried next round
                log.warning("outcome_sweep_failed", error=f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(every_s)

    # --- Telemetry ------------------------------------------------------------------------

    async def _port(self, entity_id: str, port: str | None) -> tuple[str | None, str]:
        """The port the action was on: as reported, else the device's only monitored one."""
        if port:
            return port, ""
        ports = await asyncio.to_thread(self.telemetry.ports, entity_id)
        if len(ports) == 1:
            return ports[0], ""
        if not ports:
            return None, f"no telemetry for {entity_id}"
        return None, f"several ports have telemetry ({', '.join(ports)}) and none was given"

    async def _observations(
        self, entity_id: str, port: str, t_action: datetime
    ) -> list[Observation]:
        end = t_action + self.window
        samples = await asyncio.to_thread(self.telemetry.window, entity_id, port, t_action, end)
        out = [
            Observation(s.ts, s.ts + PM_INTERVAL, s.oper_status, s.in_octets_mbps) for s in samples
        ]
        points = await self.telemetry.live_points(entity_id, port, t_action)
        out += [Observation(p.ts, p.ts, p.oper_status, p.in_octets_mbps) for p in points]
        return out


def conditions_met(results: Sequence[ConditionResult]) -> bool | None:
    """Were the advice's conditions true just before the action? True only when every
    condition was met; False when any was not met; None when telemetry couldn't tell."""
    statuses = {c.status for c in results}
    if results and statuses == {ConditionStatus.MET}:
        return True
    if ConditionStatus.NOT_MET in statuses:
        return False
    return None
