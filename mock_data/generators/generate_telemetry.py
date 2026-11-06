"""Runtime telemetry: the as-running PM layer for the transport ports.

  telemetry/interface_counters_trn_15min.ndjson.gz   15-min PM samples, transport (TRN-*) port 1/1/3,
                                                     2025-10-07 00:00Z .. 2025-10-22 00:00Z (exclusive)
  telemetry/case_trn042_counters_2025-10-12_2025-10-16.jsonl   TRN-042 trace around the case
  telemetry/case_trn042_counters_hourly.csv          hourly extract of the same trace
  telemetry/_case.json                               the case timeline + the bins that show it
  telemetry/spec/interface_counters.yml              collector spec + data contract

Sample convention (also written into the spec): a PM sample is an interval aggregate. `ts` is the
START of the 15-minute interval [ts, ts + 900 s) that the averaged values describe.

TRN-042 (and only TRN-042) shows the canonical SFP symptom: link UP, optical power normal, counters
~0 from the SFP swap until the admin bounce at 06:23Z. The 06:15Z bin (06:15-06:30) contains the
bounce and forwarding resumes at 06:24Z, so it shows partial traffic; the 06:30Z bin is normal.
Any agent that only looks at "is the link up" sees nothing wrong.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os

import common as c

WINDOW_START = dt.datetime(2025, 10, 7, tzinfo=c.UTC)   # one week before the case ...
WINDOW_END = dt.datetime(2025, 10, 22, tzinfo=c.UTC)    # ... to one week after (exclusive)
TRAFFIC_ZERO_MBPS = 0.01                                 # spec threshold (traffic_zero.condition)


def _overlap_s(a0: dt.datetime, a1: dt.datetime, b0: dt.datetime, b1: dt.datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def build_counters():
    reg = json.load(open(os.path.join(c.root_dir(), "network", "registry.json"), encoding="utf-8"))
    case = c.TRN042_CASE
    removed, inserted, recovered = case["sfp_removed"], case["sfp_inserted"], case["recovered"]
    lo, hi = c.instant(WINDOW_START), c.instant(WINDOW_END)
    rows = []
    crng = c.rng_for("counters")
    for nid in reg["transport"]:
        for iid in range(lo, hi):
            t0 = c.ts(iid)
            t1 = t0 + dt.timedelta(seconds=c.SLOT_SECONDS)
            hour = t0.hour
            rx_in = 2.5 + 0.7 * math.sin(2 * math.pi * (hour - 6) / 24) + crng.gauss(0, 0.05)
            tx = 2.5 + 0.6 * math.sin(2 * math.pi * (hour - 6) / 24) + crng.gauss(0, 0.05)
            rx_pow = -4.0 + crng.gauss(0, 0.15)
            oper = "UP"
            if nid == "TRN-042":
                stall_lo, stall_hi = c.instant(removed), c.instant(recovered)
                if stall_lo <= iid <= stall_hi:
                    # share of the interval in which the port actually forwarded traffic
                    flowing = (_overlap_s(t0, t1, t0, removed) + _overlap_s(t0, t1, recovered, t1)) / c.SLOT_SECONDS
                    zero_in = 0.0004 + abs(crng.gauss(0, 0.0001))   # stalled: counters basically zero
                    zero_tx = 0.0003 + abs(crng.gauss(0, 0.0001))
                    rx_in = round(flowing * max(rx_in, 0.0) + (1 - flowing) * zero_in, 4)
                    tx = round(flowing * max(tx, 0.0) + (1 - flowing) * zero_tx, 4)
                    if t0 < inserted:          # module physically out for this whole interval
                        oper, rx_pow = "DOWN", -40.0
                if inserted <= t0 < inserted + dt.timedelta(hours=2):
                    rx_pow += 0.4              # the new optic runs slightly hot at first
            rows.append({
                "ts": c.ts_s(iid), "iid": iid, "ne_id": nid, "port": "1/1/3",
                "in_octets_mbps": round(rx_in, 4), "out_octets_mbps": round(tx, 4),
                "rx_power_dbm": round(rx_pow, 2), "oper_status": oper,
            })
    return rows


def interface_counters_spec() -> str:
    with open(os.path.join(c.root_dir(), "network", "as_built", "devices.json"), encoding="utf-8") as fh:
        devices = json.load(fh)
    reg = json.load(open(os.path.join(c.root_dir(), "network", "registry.json"), encoding="utf-8"))
    lines = [
        "# Telemetry collector spec for the transport layer (the OLT/CMDB-facing contract).",
        "# Owners: NOC-Transport. Last change: 2024-09-30",
        "apiVersion: volga.example/v1",
        "kind: PullSpec",
        "metadata:",
        "  name: interface-counters",
        "spec:",
        "  poll_interval_seconds: 900",
        "  # Each sample aggregates one poll interval. `ts` is the interval START: a sample stamped",
        "  # 06:15:00Z describes [06:15:00Z, 06:30:00Z). Counters are mean rates over that interval.",
        "  sample_timestamp: interval_start",
        "  olt_style_query: true",
        "  metrics: [in_octets_mbps, out_octets_mbps, rx_power_dbm, oper_status]",
        "  thresholds:",
        "    # THIS is where 'traffic zero' is defined for transport ports, not in the NMS alarm policy",
        "    traffic_zero:",
        "      window_minutes: 60",
        f"      condition: 'oper_status == UP and in_octets_mbps < {TRAFFIC_ZERO_MBPS}'",
        "      action: raise TICKET, do not page",
        "  targets:",
    ]
    for d in devices:
        if d["kind"] != "optical_transport":
            continue
        lines += [f"    - ci_id: {d['device_id']}", "      port: 1/1/3", f"      model_family: {d['model']}",
                  f"      generation: {reg['trn_meta'][d['device_id']]['generation']}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    counter_rows = build_counters()
    c.write_ndjson_gz(["telemetry", "interface_counters_trn_15min.ndjson.gz"], counter_rows)
    c.write_text(["telemetry", "spec", "interface_counters.yml"], interface_counters_spec())
    lo = c.instant(dt.datetime(2025, 10, 12, tzinfo=c.UTC))
    hi = c.instant(dt.datetime(2025, 10, 16, tzinfo=c.UTC))
    trn_case = [r for r in counter_rows if r["ne_id"] == "TRN-042" and lo <= r["iid"] <= hi]
    c.write_jsonl(["telemetry", "case_trn042_counters_2025-10-12_2025-10-16.jsonl"], trn_case)
    case = c.TRN042_CASE
    by_iid = {r["iid"]: r for r in trn_case}
    # traffic_zero only counts samples with oper_status UP (the 05:00 bin has the module out: DOWN)
    first_zero = min(r["iid"] for r in trn_case
                     if r["in_octets_mbps"] < TRAFFIC_ZERO_MBPS and r["oper_status"] == "UP")
    partial = c.instant(case["recovered"])
    c.write_json(["telemetry", "_case.json"], {
        "sample_timestamp": "interval_start: a sample stamped ts covers [ts, ts + 15 min)",
        "timeline": {k: c.iso(v) for k, v in case.items() if isinstance(v, dt.datetime)},
        "first_down_bin": c.ts_s(c.instant(case["sfp_removed"])),
        "first_up_zero_traffic_bin": c.ts_s(first_zero),
        "traffic_zero_threshold_met": c.ts_s(first_zero + 4),
        "partial_recovery_bin": c.ts_s(partial),
        "partial_recovery_in_octets_mbps": by_iid[partial]["in_octets_mbps"],
        "first_normal_bin": c.ts_s(partial + 1),
        "first_normal_in_octets_mbps": by_iid[partial + 1]["in_octets_mbps"],
    })
    c.write_csv(["telemetry", "case_trn042_counters_hourly.csv"],
                ["ts", "ne_id", "in_octets_mbps", "out_octets_mbps", "rx_power_dbm", "oper_status"],
                ([r["ts"], r["ne_id"], r["in_octets_mbps"], r["out_octets_mbps"], r["rx_power_dbm"],
                  r["oper_status"]] for r in trn_case if r["iid"] % 4 == 0))
    print(f"telemetry: {len(counter_rows)} counter rows")


if __name__ == "__main__":
    main()
