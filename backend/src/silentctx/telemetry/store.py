"""Interface telemetry in DuckDB, and the condition checks the ops agent relies on.

Samples are 15-minute PM intervals stamped at the interval START (telemetry spec). A check at
time t only looks at intervals that had ENDED by t (gate V4 / `last_complete_pm_interval`):
an interval still open may already contain post-fix traffic.

History comes from `telemetry/interface_counters_trn_15min.ndjson.gz` (2025-10-07..21). The
network simulator (SIM-1) appends live samples at "now" into the `live` table; both are read
through one view, so a check doesn't care where a sample came from.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

import duckdb
from pydantic import BaseModel

from silentctx.domain.models import UtcDatetime
from silentctx.domain.time import PM_INTERVAL, ensure_utc, last_complete_pm_interval

HISTORY_FILE = Path("telemetry") / "interface_counters_trn_15min.ndjson.gz"
# "traffic zero" (telemetry spec): oper_status == UP and in_octets_mbps < 0.01 over 60 minutes.
TRAFFIC_ZERO_MBPS = 0.01
TRAFFIC_ZERO_WINDOW = timedelta(minutes=60)
# Normal received optical power for these ports. UP samples sit at -4.6..-3.3 dBm; LOS
# reads -40 dBm. Anything inside this range counts as "optics fine".
RX_NORMAL_DBM = (-10.0, 0.0)


class ConditionStatus(StrEnum):
    MET = "met"
    NOT_MET = "not_met"
    UNKNOWN = "unknown"


class ConditionResult(BaseModel):
    name: str
    status: ConditionStatus
    detail: str
    window_start: UtcDatetime | None = None
    window_end: UtcDatetime | None = None
    values: list[float | str] = []


class Sample(BaseModel):
    ts: UtcDatetime
    ne_id: str
    port: str
    in_octets_mbps: float
    out_octets_mbps: float
    rx_power_dbm: float
    oper_status: str


# Symptom names used in assertion qualifiers -> how to evaluate them.
KNOWN_SYMPTOMS = ("oper_up", "rx_power_normal", "counters_zero")


class TelemetryStore:
    """Thread-safe wrapper around one in-memory DuckDB connection."""

    def __init__(self, data_dir: Path | None) -> None:
        self._lock = threading.Lock()
        self._db = duckdb.connect(":memory:")
        self._db.execute("SET TimeZone = 'UTC'")
        columns = (
            "ts TIMESTAMPTZ, ne_id VARCHAR, port VARCHAR, in_octets_mbps DOUBLE, "
            "out_octets_mbps DOUBLE, rx_power_dbm DOUBLE, oper_status VARCHAR"
        )
        self._db.execute(f"CREATE TABLE history ({columns})")
        self._db.execute(f"CREATE TABLE live ({columns})")
        self._db.execute(
            "CREATE VIEW samples AS SELECT * FROM history UNION ALL SELECT * FROM live"
        )
        path = data_dir / HISTORY_FILE if data_dir else None
        if path is not None and path.exists():
            self._db.execute(
                "INSERT INTO history SELECT ts, ne_id, port, in_octets_mbps, out_octets_mbps, "
                "rx_power_dbm, oper_status FROM read_json_auto(?)",
                [str(path)],
            )

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT count(*) FROM samples").fetchone()
        return int(row[0]) if row else 0

    def ports(self, ne_id: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT port FROM samples WHERE ne_id = ? ORDER BY port", [ne_id]
            ).fetchall()
        return [str(r[0]) for r in rows]

    def add_live(self, samples: Sequence[Sample]) -> None:
        """For the simulator: append samples at "now"."""
        with self._lock:
            self._db.executemany(
                "INSERT INTO live VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        s.ts,
                        s.ne_id,
                        s.port,
                        s.in_octets_mbps,
                        s.out_octets_mbps,
                        s.rx_power_dbm,
                        s.oper_status,
                    )
                    for s in samples
                ],
            )

    def window(self, ne_id: str, port: str, start: datetime, end: datetime) -> list[Sample]:
        """Samples whose interval starts in [start, end)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, ne_id, port, in_octets_mbps, out_octets_mbps, rx_power_dbm, "
                "oper_status FROM samples WHERE ne_id = ? AND port = ? AND ts >= ? AND ts < ? "
                "ORDER BY ts",
                [ne_id, port, start, end],
            ).fetchall()
        keys = list(Sample.model_fields)
        return [Sample.model_validate(dict(zip(keys, r, strict=True))) for r in rows]

    # --- Conditions ------------------------------------------------------------------------

    def check(
        self, ne_id: str, port: str, symptoms: Sequence[str], at: datetime
    ) -> list[ConditionResult]:
        at = ensure_utc(at)
        last_start, last_end = last_complete_pm_interval(at)
        last = self.window(ne_id, port, last_start, last_end)
        results: list[ConditionResult] = []
        for symptom in symptoms:
            if symptom == "oper_up":
                results.append(self._oper_up(last, last_start, last_end))
            elif symptom == "rx_power_normal":
                results.append(self._rx_normal(last, last_start, last_end))
            elif symptom == "counters_zero":
                start = last_end - TRAFFIC_ZERO_WINDOW
                results.append(
                    self._counters_zero(self.window(ne_id, port, start, last_end), start, last_end)
                )
            else:
                results.append(
                    ConditionResult(
                        name=symptom,
                        status=ConditionStatus.UNKNOWN,
                        detail="no telemetry check is defined for this condition",
                    )
                )
        return results

    async def acheck(
        self, ne_id: str, port: str, symptoms: Sequence[str], at: datetime
    ) -> list[ConditionResult]:
        return await asyncio.to_thread(self.check, ne_id, port, symptoms, at)

    @staticmethod
    def _missing(name: str, start: datetime, end: datetime) -> ConditionResult:
        return ConditionResult(
            name=name,
            status=ConditionStatus.UNKNOWN,
            detail=f"no telemetry for {start:%Y-%m-%d %H:%M}-{end:%H:%M} UTC",
            window_start=start,
            window_end=end,
        )

    def _oper_up(self, last: list[Sample], start: datetime, end: datetime) -> ConditionResult:
        if not last:
            return self._missing("oper_up", start, end)
        status = last[-1].oper_status
        return ConditionResult(
            name="oper_up",
            status=ConditionStatus.MET if status == "UP" else ConditionStatus.NOT_MET,
            detail=f"interface {status}",
            window_start=start,
            window_end=end,
            values=[status],
        )

    def _rx_normal(self, last: list[Sample], start: datetime, end: datetime) -> ConditionResult:
        if not last:
            return self._missing("rx_power_normal", start, end)
        rx = last[-1].rx_power_dbm
        low, high = RX_NORMAL_DBM
        ok = low <= rx <= high
        return ConditionResult(
            name="rx_power_normal",
            status=ConditionStatus.MET if ok else ConditionStatus.NOT_MET,
            detail=f"RX {rx:.1f} dBm ({'normal' if ok else 'outside'} {low:g}..{high:g} dBm)",
            window_start=start,
            window_end=end,
            values=[rx],
        )

    def _counters_zero(
        self, samples: list[Sample], start: datetime, end: datetime
    ) -> ConditionResult:
        expected = int(TRAFFIC_ZERO_WINDOW / PM_INTERVAL)
        if len(samples) < expected:
            return self._missing("counters_zero", start, end)
        traffic = [s.in_octets_mbps for s in samples]
        down = [s for s in samples if s.oper_status != "UP"]
        moving = [t for t in traffic if t >= TRAFFIC_ZERO_MBPS]
        zero = not down and not moving
        if zero:
            detail = f"in-traffic below {TRAFFIC_ZERO_MBPS} Mbps with the port UP for 60 min"
        elif down:
            detail = f"port was {down[0].oper_status} at {down[0].ts:%H:%M} UTC within the 60 min"
        else:
            detail = f"traffic flowing in the last 60 min (max {max(moving):.3f} Mbps)"
        return ConditionResult(
            name="counters_zero",
            status=ConditionStatus.MET if zero else ConditionStatus.NOT_MET,
            detail=detail,
            window_start=start,
            window_end=end,
            values=list(traffic),
        )
