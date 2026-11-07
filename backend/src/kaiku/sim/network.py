"""The simulated as-running state of the demo nodes (plan 001 WS6, §3.5), as pure logic.

Each port has a timeline of modes: `normal` (traffic flows), `zero` (oper UP, optics fine,
no traffic: the forwarding stall after an SFP swap) and `down` (interface disabled or module
out). Telemetry is derived from that timeline when asked, so nothing ticks in the background:
- 15-minute PM samples, stamped at the interval START, only for intervals that have ended;
- a finer live series (one point every few seconds) for the demo.

Events carry the time they happen (R1). A step that happens later (the interface coming
back after a bounce) is recorded with its future time and only becomes visible once the
clock passes it.
"""

from __future__ import annotations

import itertools
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from kaiku.domain.time import PM_INTERVAL, pm_interval_start
from kaiku.telemetry.store import Sample

Mode = Literal["normal", "zero", "down"]
Clock = Callable[[], datetime]

# node -> (model, site). The live-demo nodes of plan 001 §8 WS6.
NODES = {
    "TRN-017": ("TNC-1X", "Pori"),
    "TRN-019": ("TNC-1X", "Lappeenranta"),
    "TRN-010": ("TNC-2X", "Lahti"),
}
PORTS = ("1/1/2", "1/1/3")
BASE_MBPS = 1.8  # the historical TRN-* port level (about 1.7-1.9 Mbps)
RX_DBM = -4.0
LOS_DBM = -40.0
# The stall in Elisa's case began about an hour before anyone asked (05:15 -> 06:14), and the
# "counters zero" condition needs 60 minutes of complete intervals, so a fault defaults to
# having started 75 minutes ago. Pass 0 to watch it develop in real time.
DEFAULT_STALLED_FOR = timedelta(minutes=75)
ENABLE_AFTER = timedelta(seconds=20)  # IF_DISABLED -> IF_ENABLED (06:23:00 -> 06:23:40 in 2025)
RECOVER_AFTER = timedelta(seconds=30)  # bounce -> traffic back


@dataclass(frozen=True)
class SimEvent:
    event_id: str
    event_time: datetime
    ne_id: str
    port: str
    type: str
    label: Literal["Alarm", "DeviceEvent"]
    severity: str
    text: str


@dataclass
class _Port:
    timeline: list[tuple[datetime, Mode]]

    def set_mode(self, at: datetime, mode: Mode) -> None:
        """From `at` on, the port is in `mode`; anything planned after `at` is replaced."""
        self.timeline = [seg for seg in self.timeline if seg[0] < at] + [(at, mode)]

    def mode_at(self, t: datetime) -> Mode:
        mode: Mode = self.timeline[0][1]
        for start, m in self.timeline:
            if start > t:
                break
            mode = m
        return mode

    def time_in(self, start: datetime, end: datetime) -> dict[Mode, float]:
        """Seconds spent in each mode during [start, end)."""
        out: dict[Mode, float] = {"normal": 0.0, "zero": 0.0, "down": 0.0}
        bounds = [s for s, _ in self.timeline if start < s < end]
        cuts = [start, *bounds, end]
        for a, b in itertools.pairwise(cuts):
            out[self.mode_at(a)] += (b - a).total_seconds()
        return out


@dataclass
class Simulator:
    clock: Clock = field(default=lambda: datetime.now(UTC))
    history: timedelta = timedelta(hours=24)
    events: list[SimEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.origin = pm_interval_start(self.clock() - self.history)
        self.ports = {
            (node, port): _Port([(self.origin, "normal")]) for node in NODES for port in PORTS
        }

    def _port(self, node: str, port: str) -> _Port:
        try:
            return self.ports[(node, port)]
        except KeyError:
            raise LookupError(
                f"{node} {port} is not simulated (nodes: {', '.join(NODES)}; "
                f"ports: {', '.join(PORTS)})"
            ) from None

    def _event(
        self,
        at: datetime,
        node: str,
        port: str,
        type_: str,
        label: Literal["Alarm", "DeviceEvent"],
        text: str,
        severity: str = "info",
    ) -> SimEvent:
        ev = SimEvent(
            event_id=f"SIM-{uuid.uuid4().hex[:10].upper()}",
            event_time=at,
            ne_id=node,
            port=port,
            type=type_,
            label=label,
            severity=severity,
            text=text,
        )
        self.events.append(ev)
        return ev

    def visible_events(self) -> list[SimEvent]:
        now = self.clock()
        return sorted((e for e in self.events if e.event_time <= now), key=lambda e: e.event_time)

    # --- Faults and actions ---------------------------------------------------------------

    def fault_sfp_swap_stall(
        self, node: str, port: str, stalled_for: timedelta = DEFAULT_STALLED_FOR
    ) -> list[SimEvent]:
        """A new SFP goes in, the link and optics come up clean, and traffic stays at zero
        with the port UP. The point events say everything is fine; only the counters don't."""
        p = self._port(node, port)
        t0 = max(self.clock() - stalled_for, self.origin)
        p.set_mode(t0, "zero")
        return [
            self._event(
                t0, node, port, "MODULE_INSERTED", "Alarm", f"Optical module inserted on {port}"
            ),
            self._event(
                t0 + timedelta(seconds=20), node, port, "LINK_UP", "Alarm", f"Link up on {port}"
            ),
            self._event(
                t0 + timedelta(seconds=40),
                node,
                port,
                "OPTICAL_POWER_OK",
                "Alarm",
                f"Received optical power within range on {port}",
            ),
        ]

    def bounce(
        self,
        node: str,
        port: str,
        outcome: Literal["ok", "fail"] = "ok",
        enable_after: timedelta = ENABLE_AFTER,
        recover_after: timedelta = RECOVER_AFTER,
    ) -> list[SimEvent]:
        """shutdown / no shutdown. With outcome=fail the counters stay at zero afterwards
        (used to demo a refutation); otherwise traffic returns `recover_after` later."""
        p = self._port(node, port)
        now = self.clock()
        p.set_mode(now, "down")
        p.set_mode(now + enable_after, "zero")  # back up; forwarding not yet resumed
        if outcome == "ok":
            p.set_mode(now + recover_after, "normal")
        return [
            self._event(
                now, node, port, "IF_DISABLED", "DeviceEvent", f"Interface {port} disabled"
            ),
            self._event(
                now + enable_after,
                node,
                port,
                "IF_ENABLED",
                "DeviceEvent",
                f"Interface {port} enabled",
            ),
        ]

    # --- Telemetry ------------------------------------------------------------------------

    def samples(self, node: str, start: datetime, end: datetime) -> list[Sample]:
        """15-minute samples of every port of `node` whose interval starts in [start, end)
        and has already ended."""
        if node not in NODES:
            return []
        last_end = pm_interval_start(self.clock())
        ts = max(pm_interval_start(start), self.origin)
        if ts < start:
            ts += PM_INTERVAL
        out: list[Sample] = []
        while ts < end and ts + PM_INTERVAL <= last_end:
            out.extend(self._bin(node, port, ts) for port in PORTS)
            ts += PM_INTERVAL
        return out

    def _bin(self, node: str, port: str, ts: datetime) -> Sample:
        spent = self.ports[(node, port)].time_in(ts, ts + PM_INTERVAL)
        total = PM_INTERVAL.total_seconds()
        rng = random.Random(f"{node}|{port}|{ts.isoformat()}")  # noqa: S311  # not crypto
        normal = spent["normal"] / total
        down = spent["down"] / total > 0.5
        return Sample(
            ts=ts,
            ne_id=node,
            port=port,
            in_octets_mbps=round(BASE_MBPS * normal * rng.uniform(0.9, 1.1), 4),
            out_octets_mbps=round(BASE_MBPS * normal * rng.uniform(0.9, 1.1), 4),
            rx_power_dbm=LOS_DBM if down else round(RX_DBM + rng.uniform(-0.4, 0.4), 2),
            oper_status="DOWN" if down else "UP",
        )

    def live(
        self, node: str, port: str, window: timedelta, step: timedelta
    ) -> list[dict[str, object]]:
        """The fine-grained series for the demo: one point every `step` over the last
        `window`, each the port's state at that instant."""
        p = self._port(node, port)
        now = self.clock()
        t = max(now - window, self.origin)
        out: list[dict[str, object]] = []
        while t <= now:
            mode = p.mode_at(t)
            rng = random.Random(f"{node}|{port}|{t.isoformat()}")  # noqa: S311
            out.append(
                {
                    "ts": t,
                    "oper_status": "DOWN" if mode == "down" else "UP",
                    "in_octets_mbps": round(BASE_MBPS * rng.uniform(0.9, 1.1), 4)
                    if mode == "normal"
                    else 0.0,
                    "rx_power_dbm": LOS_DBM if mode == "down" else RX_DBM,
                }
            )
            t += step
        return out
