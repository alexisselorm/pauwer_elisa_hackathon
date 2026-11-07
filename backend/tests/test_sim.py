"""SIM-1: the network simulator, and its telemetry as `check_conditions` sees it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from silentctx.sim.app import create_app
from silentctx.sim.network import Simulator
from silentctx.telemetry.store import ConditionStatus, TelemetryStore

SYMPTOMS = ["oper_up", "rx_power_normal", "counters_zero"]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 7, 12, 7, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _conditions(sim: Simulator, node: str, port: str) -> dict[str, ConditionStatus]:
    store = TelemetryStore(None)
    now = sim.clock()
    store.add_live(sim.samples(node, now - timedelta(days=1), now))
    return {c.name: c.status for c in store.check(node, port, SYMPTOMS, now)}


def test_healthy_port_has_traffic() -> None:
    sim = Simulator(clock=Clock())
    got = _conditions(sim, "TRN-017", "1/1/2")
    assert got == {
        "oper_up": ConditionStatus.MET,
        "rx_power_normal": ConditionStatus.MET,
        "counters_zero": ConditionStatus.NOT_MET,  # ST-31: normal counters -> no bounce
    }


def test_sfp_swap_stall_meets_all_three_conditions_at_once() -> None:
    sim = Simulator(clock=Clock())
    events = sim.fault_sfp_swap_stall("TRN-017", "1/1/2")
    assert [e.type for e in events] == ["MODULE_INSERTED", "LINK_UP", "OPTICAL_POWER_OK"]
    assert all(e.event_time <= sim.clock() for e in events)
    assert set(_conditions(sim, "TRN-017", "1/1/2").values()) == {ConditionStatus.MET}
    # The other port and the other nodes are untouched.
    assert _conditions(sim, "TRN-017", "1/1/3")["counters_zero"] == ConditionStatus.NOT_MET
    assert _conditions(sim, "TRN-019", "1/1/2")["counters_zero"] == ConditionStatus.NOT_MET


def test_a_fresh_stall_is_not_yet_sixty_minutes_of_zero() -> None:
    sim = Simulator(clock=Clock())
    sim.fault_sfp_swap_stall("TRN-010", "1/1/2", stalled_for=timedelta(0))
    assert _conditions(sim, "TRN-010", "1/1/2")["counters_zero"] == ConditionStatus.NOT_MET


def test_bounce_recovers_after_about_30_seconds() -> None:
    clock = Clock()
    sim = Simulator(clock=clock)
    sim.fault_sfp_swap_stall("TRN-017", "1/1/2")
    events = sim.bounce("TRN-017", "1/1/2")
    assert [e.type for e in events] == ["IF_DISABLED", "IF_ENABLED"]
    # IF_ENABLED happens 20 s later: not visible yet (R1, no future events).
    assert [e.type for e in sim.visible_events()][-1] == "IF_DISABLED"

    def last_point() -> dict[str, object]:
        return sim.live("TRN-017", "1/1/2", timedelta(seconds=1), timedelta(seconds=1))[-1]

    assert last_point()["oper_status"] == "DOWN"
    clock.now += timedelta(seconds=25)
    assert last_point() | {"ts": None} == {
        "ts": None,
        "oper_status": "UP",
        "in_octets_mbps": 0.0,
        "rx_power_dbm": -4.0,
    }
    assert sim.visible_events()[-1].type == "IF_ENABLED"
    clock.now += timedelta(seconds=10)
    assert float(last_point()["in_octets_mbps"]) > 1  # type: ignore[arg-type]
    clock.now += timedelta(minutes=30)
    assert _conditions(sim, "TRN-017", "1/1/2")["counters_zero"] == ConditionStatus.NOT_MET


def test_bounce_with_outcome_fail_leaves_counters_at_zero() -> None:
    clock = Clock()
    sim = Simulator(clock=clock)
    sim.fault_sfp_swap_stall("TRN-017", "1/1/2")
    sim.bounce("TRN-017", "1/1/2", outcome="fail")
    clock.now += timedelta(minutes=90)
    assert set(_conditions(sim, "TRN-017", "1/1/2").values()) == {ConditionStatus.MET}


def test_samples_are_complete_interval_start_bins_only() -> None:
    sim = Simulator(clock=Clock())  # 12:07:30 -> the 12:00 bin is still open
    samples = sim.samples("TRN-019", datetime(2026, 11, 7, 11, 0, tzinfo=UTC), sim.clock())
    assert sorted({s.ts.strftime("%H:%M") for s in samples}) == ["11:00", "11:15", "11:30", "11:45"]
    assert {s.port for s in samples} == {"1/1/2", "1/1/3"}
    assert sim.samples("TRN-042", sim.clock() - timedelta(hours=2), sim.clock()) == []


def test_http_endpoints() -> None:
    with TestClient(create_app(Simulator(clock=Clock()))) as client:
        assert (
            client.post(
                "/sim/fault", json={"node": "TRN-099", "port": "1/1/2", "kind": "sfp_swap_stall"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/sim/fault", json={"node": "TRN-017", "port": "1/1/2", "kind": "reboot"}
            ).status_code
            == 422
        )
        r = client.post(
            "/sim/fault", json={"node": "TRN-017", "port": "1/1/2", "kind": "sfp_swap_stall"}
        )
        assert r.status_code == 200
        assert [e["kind"] for e in r.json()] == ["Alarm"] * 3
        r = client.post(
            "/sim/action",
            json={"node": "TRN-017", "port": "1/1/2", "action": "bounce_interface"},
        )
        assert [e["type"] for e in r.json()] == ["IF_DISABLED", "IF_ENABLED"]
        since = r.json()[0]["event_time"]
        assert [e["type"] for e in client.get("/sim/events").json()] == [
            "MODULE_INSERTED",
            "LINK_UP",
            "OPTICAL_POWER_OK",
            "IF_DISABLED",
        ]
        assert client.get("/sim/events", params={"since": since}).json() == []
        rows = client.get(
            "/sim/samples",
            params={
                "ne_id": "TRN-017",
                "start": "2026-11-07T11:00:00Z",
                "end": "2026-11-07T12:00:00Z",
            },
        ).json()
        assert len(rows) == 8
        assert len(client.get("/sim/live", params={"node": "TRN-017", "minutes": 1}).json()) == 13
