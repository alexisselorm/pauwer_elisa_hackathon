"""`silentctx-sim`: the network simulator for the live demo (plan 001 WS6, SIM-1).

The demo happens "now" while the corpus is history, so this process plays the network for
TRN-017 / TRN-019 / TRN-010 at the current time:
- `POST /sim/fault` and `POST /sim/action` change the as-running state and emit events;
- `GET /sim/samples` serves 15-minute PM samples; the api pulls them into the DuckDB `live`
  table that `check_conditions` reads together with history (`SIM_URL`);
- `GET /sim/live` is the finer series for the demo; `GET /sim/events?since=` lets the
  slackbot pick up fault events for the proactive thread (SL-2).
Events are also written to Neo4j as `(:Event:Alarm|DeviceEvent)-[:ON]->(:Device)`, once
their time has come, so `get_events` sees them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import uvicorn
from asgi_correlation_id import CorrelationIdMiddleware
from fastapi import FastAPI, HTTPException
from neo4j import AsyncDriver
from pydantic import BaseModel, Field, ValidationError

from silentctx import __version__
from silentctx.config import Settings, get_settings
from silentctx.domain.models import UtcDatetime
from silentctx.errors import install_error_handlers
from silentctx.graph.driver import create_driver
from silentctx.health import Liveness
from silentctx.logging import REQUEST_ID_HEADER, configure_logging, get_logger
from silentctx.sim.network import PORTS, SimEvent, Simulator
from silentctx.telemetry.store import Sample

log = get_logger(__name__)
PUBLISH_INTERVAL_S = 2.0


class FaultIn(BaseModel):
    node: str
    port: str
    kind: Literal["sfp_swap_stall"]
    # How long ago the stall began; the default lets "counters zero for 60 min" hold at once.
    stalled_for_min: float = Field(default=75.0, ge=0, le=24 * 60)


class ActionIn(BaseModel):
    node: str
    port: str
    action: Literal["bounce_interface"]
    outcome: Literal["ok", "fail"] = "ok"


class EventOut(BaseModel):
    event_id: str
    event_time: UtcDatetime
    ne_id: str
    port: str
    type: str
    kind: str
    severity: str
    text: str


def _out(ev: SimEvent) -> EventOut:
    d = asdict(ev)
    d["kind"] = d.pop("label")
    return EventOut.model_validate(d)


async def publish(driver: AsyncDriver, database: str, events: list[SimEvent]) -> None:
    """Write events to the event stream (R1: event_time is when it happened)."""
    now = datetime.now(UTC)
    await driver.execute_query(
        "UNWIND $rows AS r MATCH (d:Device {entity_id: r.ne_id}) "
        "MERGE (e:Event {event_id: r.event_id}) "
        "ON CREATE SET e:$(r.label), e.event_time = r.event_time, e.ingested_at = $now, "
        "  e.platform = 'sim', e.origin = 'sim', e.type = r.type, e.port = r.port, "
        "  e.severity = r.severity, e.text = r.text "
        "MERGE (e)-[:ON]->(d)",
        rows=[asdict(e) for e in events],
        now=now,
        database_=database,
    )


async def _publish_forever(sim: Simulator, driver: AsyncDriver, database: str) -> None:
    done: set[str] = set()
    while True:
        due = [e for e in sim.visible_events() if e.event_id not in done]
        if due:
            try:
                await publish(driver, database, due)
                done.update(e.event_id for e in due)
            except Exception as exc:  # noqa: BLE001  # Neo4j down: retried next round
                log.warning("sim_publish_failed", error=f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(PUBLISH_INTERVAL_S)


def create_app(sim: Simulator | None = None, settings: Settings | None = None) -> FastAPI:
    sim = sim or Simulator()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings is None:
            yield
            return
        driver = create_driver(settings)
        task = asyncio.create_task(_publish_forever(sim, driver, settings.neo4j_database))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await driver.close()

    app = FastAPI(title="silentctx-sim", version=__version__, redoc_url=None, lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(CorrelationIdMiddleware, header_name=REQUEST_ID_HEADER)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> Liveness:
        """Liveness of the simulator process."""
        return Liveness()

    def _guard(fn: Any, *args: Any) -> list[SimEvent]:
        try:
            events: list[SimEvent] = fn(*args)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return events

    @app.post("/sim/fault", tags=["sim"])
    async def fault(body: FaultIn) -> list[EventOut]:
        """Inject a fault. `sfp_swap_stall`: module inserted, link up, optics OK, then zero
        traffic with the port UP."""
        events = _guard(
            sim.fault_sfp_swap_stall,
            body.node,
            body.port,
            timedelta(minutes=body.stalled_for_min),
        )
        log.info("sim_fault", node=body.node, port=body.port, kind=body.kind)
        return [_out(e) for e in events]

    @app.post("/sim/action", tags=["sim"])
    async def action(body: ActionIn) -> list[EventOut]:
        """Run an action on the network. `bounce_interface`: disable, enable after ~20 s,
        traffic back after ~30 s unless `outcome=fail`."""
        events = _guard(sim.bounce, body.node, body.port, body.outcome)
        log.info("sim_action", node=body.node, port=body.port, outcome=body.outcome)
        return [_out(e) for e in events]

    @app.get("/sim/samples", tags=["sim"])
    async def samples(ne_id: str, start: UtcDatetime, end: UtcDatetime) -> list[Sample]:
        """Complete 15-minute samples (interval-start stamped) of every simulated port."""
        return sim.samples(ne_id, start, end)

    @app.get("/sim/live", tags=["sim"])
    async def live(
        node: str, port: str = PORTS[0], minutes: float = 10, step_s: float = 5
    ) -> list[dict[str, Any]]:
        """The fine-grained series for the demo."""
        if not 0 < minutes <= 120 or not 1 <= step_s <= 60:
            raise HTTPException(status_code=422, detail="minutes in (0, 120], step_s in [1, 60]")
        try:
            return sim.live(node, port, timedelta(minutes=minutes), timedelta(seconds=step_s))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/sim/events", tags=["sim"])
    async def events(since: UtcDatetime | None = None) -> list[EventOut]:
        """Events that have happened, oldest first; `since` is exclusive."""
        return [_out(e) for e in sim.visible_events() if since is None or e.event_time > since]

    return app


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        settings: Settings | None = get_settings()
    except ValidationError as exc:
        # Telemetry and the endpoints still work; events just don't reach Neo4j.
        log.warning("sim_without_neo4j", error=str(exc))
        settings = None
    uvicorn.run(
        create_app(settings=settings),
        host=os.environ.get("SIM_HOST", "0.0.0.0"),  # noqa: S104  # served inside the container
        port=int(os.environ.get("SIM_PORT", "8100")),
        log_config=None,
    )
