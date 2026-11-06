"""Runtime telemetry: the as-running layer.

All series are on the 15-minute grid (see common.py). Realistic patterns:
  - site grid draw follows GPU training load (beta utilisation);
  - utilisation has a 6-hour "big-window" cadence (Volga trains in 6h blocks)
    plus weekday/weekend structure and a slow growth trend;
  - Finland's heating/cooling affects the DC +1.15 PUE correction;
  - an external weather file (fetched or synthesised) modulates cooling power.

TRN-042 (and only TRN-042) shows the canonical SFP symptom: link UP, optical power
normal, counters ~0 for ~2 days after the SFP replacement, then recovering after the
admin-bounce. Any agent that only looks at "is the link up" sees nothing wrong.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os

import common as c
import generate_network as gn

CASE_SFP_IID = None  # filled in main()


def _weather_by_site():
    """Real FMI temperatures index-aligned to the 15-minute grid, per DC site code."""
    path = os.path.join(c.root_dir(), "external", "fmi", "observations_15min.jsonl")
    if not os.path.exists(path):
        return None
    series: dict[str, dict[int, float]] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("t2m_c") is None:
                continue
            iid = c.instant(dt.datetime.strptime(r["measured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=c.UTC))
            series.setdefault(r["site"], {})[iid] = r["t2m_c"]
    if not series:
        return None
    return series


def build_telemetry():
    reg = json.load(open(os.path.join(c.root_dir(), "network", "registry.json")))
    sites = gn.SITES
    # case timing (from the network generator): SFP replaced 2025-10-14 ~08:0x, bounce at ~15:5x
    sfp_iid = c.to_iid(c.TRN042_CASE["sfp_inserted"])   # traffic stalls from the SFP insert ...
    bounce_iid = c.to_iid(c.TRN042_CASE["bounce"])      # ... until the port bounce (common.py)

    weather = _weather_by_site()
    site_temp: dict[str, list[float]] = {}
    for s in sites:
        code, _, city = s[0], s[1], s[2]
        vals = None
        if weather and code in weather:
            real = weather[code]
            vals = [real.get(iid) for iid in range(c.N_INSTANTS)]
            if sum(v is not None for v in vals) < c.N_INSTANTS * 0.9:
                vals = None
        if not vals:
            # deterministic synthetic fallback: seasonal sinusoid + diurnal + noise
            rng = c.rng_for("wx", code)
            base = {"FI-HEL-DC1": 6.5, "FI-TRE-DC2": 5.0, "FI-OUL-DC3": 2.0, "FI-KEM-DC4": 0.5}[code]
            vals = []
            for iid in range(c.N_INSTANTS):
                doy = c.ts(iid).timetuple().tm_yday
                hourly = 6.2 * math.cos(2 * math.pi * (doy - 200) / 365.25)
                diurnal = 2.5 * math.sin(2 * math.pi * ((c.ts(iid).hour % 24) - 9) / 24)
                vals.append(round(base + hourly + diurnal + rng.gauss(0, 1.2), 1))
        if weather and code in weather:
            last = None
            for i, v in enumerate(vals):
                if v is None:
                    vals[i] = last if last is not None else vals[0]
                else:
                    last = v
        site_temp[code] = vals[: c.N_INSTANTS]

    rows = []
    for s in sites:
        code, name, city, lat, lon, role, grid_mw, racks, gpu_nodes = s
        rng = c.rng_for("telemetry", code)
        temp = site_temp[code]
        last_pmi = 0.0
        for iid in range(c.N_INSTANTS):
            d = c.ts(iid)
            # big-window training cadence: 6h windows, every 2nd window may be skipped on weekends
            weekday = d.weekday()
            hour = d.hour
            window = (hour // 6) % 4
            busy = True
            if gpu_nodes == 0:
                busy = False  # cold storage: no training GPUs
            elif weekday >= 5 and window in (2, 3) and code != "FI-HEL-DC1":
                busy = False
            # permanent slow growth (~1.6%/month) of the training cohort
            months = (d - dt.datetime(2025, 10, 1, tzinfo=c.UTC)).days / 30.44
            growth = 1.0 + 0.016 * months
            base_util = {"primary": 0.72, "secondary": 0.66, "tertiary": 0.58, "cold_storage": 0.0}[role]
            if busy:
                util = min(0.985, base_util * growth + 0.08 * math.sin(iid / 29.0) + rng.gauss(0, 0.02))
                util = max(0.30, util)
            else:
                util = 0.05 + 0.03 * rng.random()
            # cooling correction to PUE from outside temperature
            t = temp[iid]
            pue = 1.13 + max(0.0, (18.0 - t)) * 0.0032 + max(0.0, (t - 24.0)) * 0.0026
            it_mw = gpu_nodes * 0.0085 * util * (racks / max(gpu_nodes, 1) * 0.0 + 1)  # ~8.5 kW/GPU node
            it_mw += racks * 0.0035 * (0.55 if busy else 0.12)
            if role == "cold_storage":
                it_mw = racks * 0.0018 * (0.6 + 0.4 * (t < 5))
            grid = it_mw * pue
            # telemetry contract includes a few known bad episodes
            bad = False
            if code == "FI-TRE-DC2" and 0 <= months <= 1 and iid % 96 == 40 and rng.random() < 0.35:
                bad = True
            if code == "FI-HEL-DC1" and months > 8.5 and iid % 96 in (41, 42) and rng.random() < 0.05:
                bad = True
            if bad:
                grid *= rng.uniform(1.6, 2.6)  # meter/metering error (maintenance spike pattern)
            pmi = last_pmi * 0.97 + grid * 0.03
            last_pmi = pmi
            rows.append({
                "ts": c.ts_s(iid), "iid": iid, "site": code, "device_id": f"{code}-GRID-METER",
                "metric": "grid_power_mw", "value": round(grid, 3),
                "unit": "MW", "quality": "suspect" if bad else "good",
            })
            rows.append({
                "ts": c.ts_s(iid), "iid": iid, "site": code, "device_id": f"{code}-PDU-A",
                "metric": "it_power_mw", "value": round(it_mw, 3), "unit": "MW", "quality": "good",
            })
            rows.append({
                "ts": c.ts_s(iid), "iid": iid, "site": code, "device_id": f"{code}-GPU-FLEET",
                "metric": "gpu_utilisation_pct", "value": round(util * 100, 1), "unit": "%", "quality": "good",
            })
            rows.append({
                "ts": c.ts_s(iid), "iid": iid, "site": code, "device_id": f"{code}-COOLER",
                "metric": "pue", "value": round(pue, 3), "unit": "ratio", "quality": "good",
            })
            rows.append({
                "ts": c.ts_s(iid), "iid": iid, "site": code, "device_id": f"{code}-WEATHER-STATION",
                "metric": "outside_temp_c", "value": round(t, 1), "unit": "degC", "quality": "good",
            })
            rows.append({
                "ts": c.ts_s(iid), "iid": iid, "site": code, "device_id": f"{code}-BESS",
                "metric": "bess_soc_pct", "value": round(50 + 40 * math.sin(2 * math.pi * (hour / 24)) + rng.gauss(0, 2), 1),
                "unit": "%", "quality": "good",
            })

    rows.sort(key=lambda r: r["iid"])
    site_rows = rows

    return_rows = sum(1 for r in site_rows if r["quality"] == "suspect")
    # ---------------- interface counters (the "PM data" that reveals TRN-042)
    reg_trn = reg["transport"]
    counter_rows = []
    crng = c.rng_for("counters")
    for nid in reg_trn:
        base = 2.5 if nid != "TRN-042" else 2.5
        for iid in range(c.N_INSTANTS):
            hour = c.ts(iid).hour
            import_mbps = base + 0.7 * math.sin(2 * math.pi * (hour - 6) / 24) + crng.gauss(0, 0.05)
            tx = base + 0.6 * math.sin(2 * math.pi * (hour - 6) / 24) + crng.gauss(0, 0.05)
            if nid == "TRN-042":
                if sfp_iid <= iid < bounce_iid:
                    import_mbps = 0.0004 + abs(crng.gauss(0, 0.0001))  # traffic doesn't recover
                    tx = 0.0003 + abs(crng.gauss(0, 0.0001))
                elif iid == bounce_iid:
                    import_mbps = 0.02
                else:
                    import_mbps = max(import_mbps, 0.0)
            counter_rows.append({
                "ts": c.ts_s(iid), "iid": iid, "ne_id": nid, "port": "1/1/3",
                "in_octets_mbps": round(import_mbps, 4), "out_octets_mbps": round(tx, 4),
                "rx_power_dbm": round(-4.0 + (0.4 if (nid == "TRN-042" and sfp_iid <= iid < sfp_iid + 8) else 0)
                                      + crng.gauss(0, 0.15), 2),
                "oper_status": "UP",
            })
    return rows, counter_rows, {"sfp_iid": sfp_iid, "bounce_iid": bounce_iid}


def main() -> None:
    site_rows, counter_rows, case = build_telemetry()
    c.write_ndjson_gz(["telemetry", "site_telemetry_15min.ndjson.gz"], site_rows)
    c.write_ndjson_gz(["telemetry", "interface_counters_trn_15min.ndjson.gz"], counter_rows)
    # small CSV extract so the data is inspectable without gzip
    c.write_csv(["telemetry", "site_telemetry_daily.csv"],
                ["date", "site", "grid_power_mwh", "it_power_mwh", "pue_avg", "gpu_util_avg_pct", "outside_temp_c_avg"],
                _daily(
                    [r for r in site_rows if r["metric"] in
                     ("grid_power_mw", "it_power_mw", "pue", "gpu_utilisation_pct", "outside_temp_c")]
                ))
    c.write_json(["telemetry", "case_trn042_counters_2025-10-12_2025-10-16.jsonl"], None) if False else None
    lo = c.to_iid(dt.datetime(2025, 10, 12, tzinfo=c.UTC))
    hi = c.to_iid(dt.datetime(2025, 10, 16, tzinfo=c.UTC))
    trn_case = [r for r in counter_rows if r["ne_id"] == "TRN-042" and lo <= r["iid"] <= hi]
    c.write_jsonl(["telemetry", "case_trn042_counters_2025-10-12_2025-10-16.jsonl"], trn_case)
    c.write_json(["telemetry", "_case.json"], {k: c.ts_s(v) for k, v in case.items()})
    c.write_csv(["telemetry", "case_trn042_counters_hourly.csv"],
                ["ts", "ne_id", "in_octets_mbps", "out_octets_mbps", "rx_power_dbm", "oper_status"],
                ([r["ts"], r["ne_id"], r["in_octets_mbps"], r["out_octets_mbps"], r["rx_power_dbm"],
                  r["oper_status"]] for r in trn_case if r["iid"] % 4 == 0))
    print(f"telemetry: {len(site_rows)} site rows, {len(counter_rows)} counter rows")


def _daily(rows):
    by: dict[tuple, dict] = {}
    for r in rows:
        day = r["ts"][:10]
        k = (day, r["site"])
        d = by.setdefault(k, {"grid_power_mw": [], "it_power_mw": [], "pue": [], "gpu_utilisation_pct": [], "outside_temp_c": []})
        if r["quality"] != "suspect" or r["metric"] != "grid_power_mw":
            d[r["metric"]].append(r["value"])
    out = []
    for (day, site), d in sorted(by.items()):
        mean = lambda xs: (sum(xs) / len(xs)) if xs else 0.0
        out.append([day, site, round(mean(d["grid_power_mw"]) * 0.25, 3), round(mean(d["it_power_mw"]) * 0.25, 3),
                    round(mean(d["pue"]), 3), round(mean(d["gpu_utilisation_pct"]), 1), round(mean(d["outside_temp_c"]), 1)])
    return out


if __name__ == "__main__":
    main()
