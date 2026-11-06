"""Network layers: as-designed intent, as-built physical, as-running runtime, events.

Topology (Elisa-style access / transport / core):

    access nodes (OLT, aggregation switches, microwave hops, cabinet switches, CPE)
        -> transport nodes (TRN-0xx: ROADM line systems, amplifiers, PON OLT chassis)
        -> core nodes (CR-0xx: IP backbone routers)  [core is fully connected]

Volga's compute sites are attached to core routers. There are three sites:
    FI-HEL-DC1 (Helsinki, primary)   FI-TRE-DC2 (Tampere, secondary)
    FI-OUL-DC3 (Oulu, tertiary)      FI-KEM-DC4 (Kemi, cold storage)
on the "Elisa utility grid" that Volga trains jobs on (see telemetry.py).

Every generated instant ID belongs to the same time grid (see common.py).
"""
from __future__ import annotations

import datetime as dt
import math

import common as c

# ------------------------------------------------------------------ site definitions
SITES = [
    # code, name, city, lat, lon, role, power MW, racks, gpu nodes (8x H100 each)
    ("FI-HEL-DC1", "Helsinki DC1 (Pitäjänmäki)", "Helsinki", 60.2205, 24.8606, "primary", 78.0, 168, 220),
    ("FI-TRE-DC2", "Tampere DC2", "Tampere", 61.4978, 23.7610, "secondary", 46.0, 92, 115),
    ("FI-OUL-DC3", "Oulu DC3", "Oulu", 65.0121, 25.4651, "tertiary", 22.0, 44, 52),
    ("FI-KEM-DC4", "Kemi DC4 (cold storage)", "Kemi", 65.7364, 24.5637, "cold_storage", 9.0, 16, 0),
]
SITE_BY_CODE = {s[0]: s for s in SITES}

# ------------------------------------------------------------------ access locations
ACCESS_CITIES = [
    ("Helsinki", 6), ("Espoo", 3), ("Vantaa", 4), ("Tampere", 4), ("Turku", 3), ("Oulu", 3),
    ("Jyväskylä", 2), ("Kuopio", 2), ("Lahti", 2), ("Rovaniemi", 2), ("Kemi", 2), ("Ivalo", 1),
    ("Joensuu", 2), ("Vaasa", 2), ("Seinäjoki", 1), ("Kajaani", 1), ("Mikkeli", 1), ("Pori", 2),
    ("Kokkola", 1), ("Lappeenranta", 2), ("Hämeenlinna", 1), ("Sodankylä", 1),
]

BANK_DEVICES = ["Zabbix", "Netcool", "Infrared", "Blueeye", "Uama", "Nagios"]


def _node_profile(node_id: str) -> dict:
    """Synthetic-but-stable device profile for a given node ID."""
    rng = c.rng_for("node", node_id)
    kind = "transmission" if node_id.startswith("TRN") else ("core" if node_id.startswith("CR") else "access")
    return {"rng": rng, "kind": kind}


def build_network():
    rng = c.rng_for("network")

    design_nodes: list[dict] = []
    design_links: list[dict] = []
    physical_devices: list[dict] = []
    physical_segments: list[dict] = []

    def trn_node(n: int, city: str) -> str:
        return f"TRN-{n:03d}"

    # ---------------- core routers
    core_nodes = [f"CR-{i:03d}" for i in range(1, 7)]
    core_cities = ["Helsinki", "Espoo", "Tampere", "Turku", "Oulu", "Rovaniemi"]
    for nid, city in zip(core_nodes, core_cities):
        lat, lon = c.CITY_COORDS[city]
        design_nodes.append({
            "node_id": nid, "layer": "core", "type": "IP backbone router", "vendor": ["Nokia"][0],
            "model": "7750 SR-14s", "site": city, "region": city, "lat": round(lat, 4), "lon": round(lon, 4),
            "design_role": ["Backbone", "Maintenance domain"],
            "role": ["Backbone", "Maintenance domain"], "commissioned": None,
            "design_capacity_gbps": 9600,
        })
        physical_devices.append({
            "device_id": nid, "name": nid, "node_id": nid, "kind": "router", "vendor": "Nokia",
            "model": "7750 SR-14s", "serial": f"NH{safe_serial(rng)}", "chassis": None,
            "firmware": rng.choice(["23.10.R3", "23.10.R4", "24.3.R1"]), "rack": f"R{rng.randint(1, 12):02d}",
            "room": f"{city} core hall", "commissioned": rng.choice(["2019-03-14", "2020-06-01", "2021-09-22"]),
            "warranty_end": "2027-12-31", "status": "in_service", "country": "FI",
        })

    # ---------------- transport (ROADM / inline amplifier / PON OLT chassis)
    trn_counter = 1
    access_counter = 1
    trn_by_city: dict[str, list[str]] = {}
    trn_meta: dict[str, dict] = {}
    # Kemi hosts TRN-042, the device of the canonical Elisa case (see case docs).
    # We deliberately place it first so the ID is stable.
    city_list = ["Kemi"] + [cc for cc, k in ACCESS_CITIES for _ in range(k)]
    # guarantee a transport node per core city + the important access cities
    for city in dict.fromkeys(core_cities + city_list):
        if city not in trn_by_city:
            nid = "TRN-042" if city == "Kemi" else trn_node(trn_counter, city)
            trn_counter += 1 if nid != "TRN-042" else 0
            trn_by_city.setdefault(city, []).append(nid)
            if nid == "TRN-042":
                model = "TNC-1X"  # the old unit from the case
            else:
                model_roll = rng.random()
                model = "TNC-1X" if model_roll < 0.45 else ("TNC-2X" if model_roll < 0.8 else "TNC-3X")
            commissioned = {
                "TNC-1X": "2019-04-18" if nid == "TRN-042" else rng.choice(["2018-05-11", "2019-02-20", "2019-08-30"]),
                "TNC-2X": rng.choice(["2021-04-12", "2022-03-07", "2022-11-18"]),
                "TNC-3X": rng.choice(["2024-01-15", "2024-09-03", "2025-02-26"]),
            }[model]
            lat, lon = c.CITY_COORDS[city]
            design_nodes.append({
                "node_id": nid, "layer": "transport", "type": "optical transport unit",
                "vendor": "Nokia", "model": model, "site": city, "region": city,
                "lat": round(lat + rng.uniform(-0.05, 0.05), 4), "lon": round(lon + rng.uniform(-0.05, 0.05), 4),
                "design_role": ["Transmission", "Maintenance domain"], "role": ["Transmission", "Maintenance domain"],
                "commissioned": None, "design_capacity_gbps": 400 if model == "TNC-3X" else 200,
                "access_capacity_gbps": sum(k * 10 for cc, k in ACCESS_CITIES if cc == city) * 10,
            })
            physical_devices.append({
                "device_id": nid, "name": f"{city} {nid} (ROADM-1)", "node_id": nid, "kind": "optical_transport",
                "vendor": "Nokia", "model": model, "serial": f"NOK{model.replace('-', '')}{safe_serial(rng, 6)}",
                "chassis": f"CH-1X-{rng.randint(1, 6)}", "firmware": {
                    "TNC-1X": "v6.1.4", "TNC-2X": "v8.2.1", "TNC-3X": "v9.0.2"}[model],
                "rack": f"R{rng.randint(1, 3):02d}", "room": f"{city} transport room",
                "commissioned": commissioned, "warranty_end": "2026-12-31" if model == "TNC-1X" else "2028-12-31",
                "status": "in_service", "country": "FI",
            })
            trn_meta[nid] = {"model": model, "city": city, "commissioned": commissioned,
                             "generation": {"TNC-1X": "Gen1", "TNC-2X": "Gen2", "TNC-3X": "Gen3"}[model]}

            # passive segment: the run from the local pop to this node
            seg = f"SEG-{nid.replace('TRN-', '')}"
            physical_segments.append({
                "segment_id": seg, "name": f"{city} local fibre segment", "type": "fibre",
                "end_a": nid, "end_b": "PON-" + nid, "length_km": round(rng.uniform(0.6, 7.5), 2),
                "owner": "Elisa", "cable_ref": f"FIB-{city[:3].upper()}-{rng.randint(100, 999)}",
                "installed": commissioned, "notes": "duct route shared with chain operator" if rng.random() < 0.1 else "",
            })
            # some transport nodes sit at the end of a private road (access knowledge!)
            private_road = city in ("Ivalo", "Sodankylä", "Rovaniemi", "Kajaani", "Kemi") and rng.random() < 0.6
            physical_segments[-1]["private_road"] = private_road
            if private_road:
                physical_segments[-1]["barrier"] = rng.choice(["gate", "boom", "chain"])
                physical_segments[-1]["access_notes"] = "site reachable only via private road; see access register"
    # ---------------- access: per physical location a chain of hops
    access_nodes: list[dict] = []
    hop_links: list[dict] = []
    segment_counter = 1
    access_counter = 1
    for city, count in ACCESS_CITIES:
        pass  # placeholder kept for readability
    for city, count in ACCESS_CITIES:
        lat0, lon0 = c.CITY_COORDS[city]
        trn_id = trn_by_city[city][0]
        for idx in range(count):
            # chained where a cabinet aggregates several hotspots (metro), else star
            metro = city in ("Helsinki", "Espoo", "Vantaa", "Tampere", "Turku")
            uplink = trn_id if (not metro or idx == 0) else f"ACC-{access_counter - 1:03d}"
            nid = f"ACC-{access_counter:03d}"
            access_counter += 1
            dmodel = rng.choice(["7250 IXR-e", "7250 IXR-s", "KMAX-4K", "KMAX-4H"])
            lat = lat0 + rng.uniform(-0.09, 0.09)
            lon = lon0 + rng.uniform(-0.09, 0.09)
            access_nodes.append({
                "node_id": nid, "layer": "access", "type": "aggregation switch",
                "vendor": "Nokia" if dmodel.startswith("7250") else "Mitsubishi", "model": dmodel,
                "site": f"{city} hotspot {idx + 1}", "region": city,
                "lat": round(lat, 4), "lon": round(lon, 4),
                "design_role": ["Access"] + (["Microwave"] if dmodel.startswith("KMAX") else ["Fibre"]),
                "role": ["Access"], "commissioned": None, "design_capacity_gbps": rng.choice([10, 25, 50]),
                "uplink": uplink, "city": city,
            })
            physical_devices.append({
                "device_id": nid, "name": f"{city} hotspot {idx + 1} {nid}", "node_id": nid,
                "kind": "microwave_radio" if dmodel.startswith("KMAX") else "aggregation_switch",
                "vendor": "Nokia" if dmodel.startswith("7250") else "Mitsubishi", "model": dmodel,
                "serial": safe_serial(rng, 9), "chassis": None,
                "firmware": rng.choice(["v8.0.2", "v9.1.1", "v10.0.3", "13.0.R1"]),
                "rack": None, "room": f"{city} hotspot {idx + 1}",
                "commissioned": rng.choice([f"2019-{m:02d}-{d:02d}" for m in range(1, 13) for d in (5, 17, 23)]),
                "warranty_end": "2025-12-31", "status": "in_service", "country": "FI",
                "nearest_uplink": uplink,
            })
            seg = f"SEG-ACC-{segment_counter:04d}"
            segment_counter += 1
            physical_segments.append({
                "segment_id": seg, "name": f"{city} hotspot {idx + 1} drop",
                "type": "microwave" if dmodel.startswith("KMAX") else "fibre",
                "end_a": uplink, "end_b": nid,
                "length_km": round(rng.uniform(3, 18) if dmodel.startswith("KMAX") else rng.uniform(0.2, 3.2), 2),
                "owner": "Elisa", "cable_ref": f"FIB-{city[:3].upper()}-{rng.randint(100, 999)}",
                "installed": physical_devices[-1]["commissioned"],
                "notes": "", "private_road": False, "barrier": None,
            })
            hop_links.append({
                "link_id": f"LINK-{nid}-UP", "layer": "access",
                "kind": "microwave" if dmodel.startswith("KMAX") else "fibre",
                "end_a": uplink, "end_b": nid, "design_capacity_gbps": rng.choice([10, 25]), "redundant": False,
            })

    # ---------------- design links between the layers
    design_links: list[dict] = []
    for nid in core_nodes:
        design_links.append({"link_id": f"LINK-{nid}-TRN-UP", "layer": "core",
                             "end_a": nid, "end_b": "TRN-UP", "design_capacity_gbps": 400, "redundant": True})
    for nid, meta in trn_meta.items():
        core = core_nodes[rng.randrange(len(core_nodes))]
        design_links.append({"link_id": f"LINK-{nid}-{core}", "layer": "transport", "end_a": nid,
                             "end_b": core, "design_capacity_gbps": rng.choice([100, 200, 400]), "redundant": rng.random() < 0.6})
    design_links.extend(hop_links)

    # links to the grid sites (compute nodes attach to their nearest core)
    site_links = []
    for s in SITES:
        code, _, city, *_ = s
        core = "CR-001" if city in ("Helsinki", "Espoo", "Vantaa") else (
            "CR-003" if city == "Tampere" else "CR-005" if city == "Oulu" else "CR-006")
        site_links.append({"link_id": f"LINK-{code}-{core}", "layer": "site", "end_a": code, "end_b": core,
                           "design_capacity_gbps": {"FI-HEL-DC1": 800, "FI-TRE-DC2": 400,
                                                    "FI-OUL-DC3": 200, "FI-KEM-DC4": 100}[code],
                           "redundant": True})
    design_links.extend(site_links)

    # ---------------- retired/deprecated nodes (decommissioned during the window)
    decommissioned = []
    # a few Gen1 nodes get taken out of service in 2026
    gen1 = [n for n, m in trn_meta.items() if m["generation"] == "Gen1" and n != "TRN-042"]
    for dec_nid in gen1[:3]:
        decommissioned.append({"node_id": dec_nid, "decommissioned": f"2026-0{rng.randint(2, 7)}-{rng.randint(10, 28)}",
                               "replacement": "TNC-3X"})

    return {
        "sites": [{"code": s[0], "name": s[1], "city": s[2], "lat": s[3], "lon": s[4], "role": s[5],
                   "grid_power_mw": s[6], "racks": s[7], "gpu_nodes": s[8]} for s in SITES],
        "core_nodes": core_nodes,
        "design_nodes": design_nodes,
        "design_links": design_links,
        "physical_devices": physical_devices,
        "physical_segments": physical_segments,
        "trn_meta": trn_meta,
        "access_nodes": access_nodes,
        "decommissioned": decommissioned,
        "trn_by_city": trn_by_city,
    }


def safe_serial(rng, n: int = 8) -> str:
    return "".join(rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(n))


# ------------------------------------------------------------------ runtime topology
def _role_of(dev: dict) -> list[str]:
    kind = dev["kind"]
    if kind == "router":
        return ["Backbone", "Maintenance domain"]
    if kind == "optical_transport":
        return ["Transmission", "Maintenance domain"]
    if kind == "aggregation_switch":
        return ["Access"]
    return ["Microwave"]


def build_runtime(net: dict):
    """Build runtime node/link objects, including the 2019-planned -> 2025-reality drift."""
    rng = c.rng_for("runtime")
    devices = net["physical_devices"]
    dev_by_id = {d["device_id"]: d for d in devices}
    trn_meta = net["trn_meta"]

    current_nodes = []
    retired_nodes = []
    for d in devices:
        status = "active"
        for dec in net["decommissioned"]:
            if dec["node_id"] == d["device_id"]:
                status = "decommissioning"
                retired_nodes.append({"node_id": d["device_id"], "retired_at": dec["decommissioned"]})
        current_nodes.append({
            "node_id": d["device_id"], "status": status, "model": d["model"], "vendor": d["vendor"],
            "firmware": d["firmware"], "role": _role_of(d), "site": d["room"],
            "commissioned": d["commissioned"],
            "generation": trn_meta.get(d["device_id"], {}).get("generation")
            if d["device_id"] in trn_meta else None,
        })

    def hw_slug(model: str) -> str:
        return model.replace("-", "").replace(" ", "").lower()

    # --- physical-node snapshots: start with the 2019 plan, get superseded over time
    snapshots = []
    nid = 0

    def add_snap(valid_from: str, entity: dict, model: str, supersedes: str | None, is_frozen=False):
        nonlocal nid
        nid += 1
        snapshots.append({
            "physical_node_id": f"PHY-{nid:05d}",
            "valid_from": valid_from, "valid_to": None, "supersedes": supersedes,
            "physical_name": entity["node_id"],
            "hardware_class": entity.get("kind", "node"),
            "hardware_model": model,
            "firmware": None,
            "software_config_ref": f"SWCFG-{entity['node_id']}-{valid_from[:7].replace('-', '')}",
            "owner_team": "NOC-Transport" if entity.get("kind") == "optical_transport" else "NOC-Access",
            "confidence": "as_built" if supersedes is None else "as_running",
        })

    # Hardware-model slugs are free text in the real data. The 2019 as-planned records
    # use a "tnc2_roadm"-style internal slug while the as-running records use the vendor
    # model string. Anything resolving these to one entity must normalise (or have been
    # taught the alias). Volga SRE knows this because an email says so (see docs).
    plan_variants = {
        "TNC-1X": ["tnc1_roadm", "tnc-1x_roadm"],
        "TNC-2X": ["tnc2_roadm", "tnc-2x_roadm"],
        "TNC-3X": ["tnc3_roadm", "tnc-3x_roadm"],
    }
    supersession_schedule = {}
    for did, meta in trn_meta.items():
        default_slug = hw_slug(meta["model"])
        # a node's first physical record is valid from the day it was commissioned, not before
        commissioned = c.date_to_instant(meta["commissioned"])
        add_snap(commissioned, {"node_id": did, "kind": "optical_transport"}, default_slug, None, True)
        if meta["model"] in plan_variants and rng.random() < 0.55:
            # a record can only be superseded after it started being valid
            candidates = [t for t in ["2023-04-03T08:00:00Z", "2024-06-17T09:30:00Z", "2025-11-20T08:00:00Z",
                                      "2026-02-09T07:15:00Z", "2026-05-27T10:00:00Z"] if t > commissioned]
            changed = rng.choice(candidates)
            alternates = [s for s in plan_variants[meta["model"]] if s != default_slug]
            first = snapshots[-1]
            first["valid_to"] = changed  # closed by its successor
            add_snap(changed, {"node_id": did, "kind": "optical_transport"}, rng.choice(alternates), first["physical_node_id"])
            supersession_schedule[did] = changed
    # decommissioned nodes: their last physical record ends on the decommission date
    decom = {d["node_id"]: c.date_to_instant(d["decommissioned"]) for d in net["decommissioned"]}
    for snap in snapshots:
        if snap["valid_to"] is None and snap["physical_name"] in decom:
            snap["valid_to"] = decom[snap["physical_name"]]

    # ---------------- links
    link_rng = c.rng_for("links")
    current_links = []
    retired_links = []
    for l in net["design_links"]:
        if l["layer"] in ("access", "transport", "core"):
            current_links.append({
                "link_id": l["link_id"], "end_a": l["end_a"], "end_b": l["end_b"],
                "design_capacity_gbps": l["design_capacity_gbps"],
                "capacity_gbps": l["design_capacity_gbps"],
                "mtu": 9216, "status": "up", "redundant": l["redundant"],
                "media": "fibre" if l["layer"] != "access" or "MW" not in l["link_id"] else "microwave",
            })
    # add one retired link (TRN-012 uplink was microwave between 2019 and 2024). It is replaced by
    # TRN-012's current transport uplink, whatever core node that design link goes to.
    trn012_up = next((l["link_id"] for l in current_links if l["end_a"] == "TRN-012"), None)
    retired_links.append({"link_id": "LINK-TRN-012-MW-UP", "end_a": "TRN-012", "end_b": "CR-006",
                          "capacity_gbps": 10, "media": "microwave", "retired_at": "2024-04-11",
                          "superseded_by": trn012_up})

    return {
        "current_nodes": current_nodes,
        "physical_node_snapshots": snapshots,
        "supersession_schedule": supersession_schedule,
        "current_links": current_links,
        "retired_links": retired_links,
    }


# ------------------------------------------------------------------ events
def build_device_events(net: dict):
    """Maintenance / change / decommission events per device (the as-observed stream)."""
    rng = c.rng_for("events")
    rows = []
    devs = net["physical_devices"]
    trn_meta = net["trn_meta"]
    for i, d in enumerate(devs):
        did = d["device_id"]
        if did in trn_meta and trn_meta[did]["generation"] == "Gen1":
            # Gen1s were upgraded at some point
            if rng.random() < 0.4:
                base = c.to_iid(dt.datetime(2023, rng.randint(1, 12), rng.randint(1, 27), 6, 0, tzinfo=c.UTC))
                rows.append(_evt(rng, did, "firmware_upgrade", base - 25, base + 10, "maintenance.completed",
                                 f"Firmware upgrade v5.9.2 -> v6.1.4 on {did}", "NOC-Transport",
                                 {"from": "v5.9.2", "to": "v6.1.4"}))
        # field visits: SFP replacements, module swaps
        if d["kind"] == "optical_transport" and d["model"] == "TNC-1X" and rng.random() < 0.5:
            base = c.to_iid(dt.datetime(2026, rng.randint(2, 10), rng.randint(1, 27), rng.randint(7, 15), 0, tzinfo=c.UTC))
            port = f"1/1/{rng.randint(1, 8)}"
            rows.append(_evt(rng, did, "field_visit", base - 40, base + 30, "field.maintenance.completed",
                             f"SFP replacement on {did} port {port}", "Field Ops", {"port": port,
                                                                                       "sfp": f"SFP-10G-{rng.randint(1000, 9999)}"}))
        if d["kind"] == "microwave_radio" and rng.random() < 0.45:
            base = c.to_iid(dt.datetime(2026, rng.randint(1, 10), rng.randint(1, 27), rng.randint(6, 18), 0, tzinfo=c.UTC))
            rows.append(_evt(rng, did, "field_visit", base - 30, base + 25, "field.maintenance.completed",
                             f"Antenna alignment / dish check on {did}", "Field Ops", {"work": "alignment"}))
        if d["kind"] == "aggregation_switch" and rng.random() < 0.30:
            base = c.to_iid(dt.datetime(2026, rng.randint(1, 10), rng.randint(1, 27), rng.randint(8, 17), 0, tzinfo=c.UTC))
            rows.append(_evt(rng, did, "field_visit", base - 20, base + 20, "field.maintenance.completed",
                             f"Replaced PSU in {did} cabinet", "Field Ops", {"work": "psu_replace"}))
        if d["kind"] == "router" and rng.random() < 0.6:
            base = c.to_iid(dt.datetime(2026, rng.randint(1, 10), rng.randint(1, 27), 1, 0, tzinfo=c.UTC))
            rows.append(_evt(rng, did, "change_window", base - 10, base + 60, "change.completed",
                             f"Planned maintenance window {did}", "NOC-Core", {"type": "software"}))
    for dec in net["decommissioned"]:
        iid = c.to_iid(dt.datetime.fromisoformat(dec["decommissioned"]).replace(tzinfo=c.UTC))
        rows.append(_evt(rng, dec["node_id"], "decommission", iid - 96, iid + 288, "decommission.completed",
                         f"{dec['node_id']} decommissioned, replaced by {dec['replacement']}",
                         "NOC-Transport", {"replacement": dec["replacement"]}))
    rows.sort(key=lambda r: r["event_time_iid"])
    for i, r in enumerate(rows):
        r["event_id"] = f"EVT-{262000 + i:06d}"
        r["record_time"] = r["event_time"] = c.ts_s(r["event_time_iid"])
        r["end_time"] = c.ts_s(r["end_time_iid"])
    return rows


def _evt(rng, device_id, etype, start, end, outcome, desc, team, payload) -> dict:
    start = max(start, 0)
    end = min(max(end, start + 1), c.END_IID)
    return {
        "event_id": None, "device_id": device_id, "type": etype, "workorder_id": c.rand_id(rng, "WO-", 7),
        "event_time_iid": start, "end_time_iid": end, "outcome": outcome, "description": desc,
        "team": team, "payload": payload,
    }


def build_alarms(net: dict, events: list[dict]):
    """NMS alarm stream. Includes the exact TRN-042 SFP case from the Elisa brief plus many others.

    Returns (rows, case_meta) where case_meta describes where the TRN-042 incident lives.
    """
    rng = c.rng_for("alarms")
    rows = []
    alarm_no = 0
    candidates = [d["device_id"] for d in net["physical_devices"] if d["kind"] in
                  ("optical_transport", "microwave_radio", "aggregation_switch", "router")]

    def emit(device, typ, sev, ts_iid, text, prob=None, counters=None, closed=None, cause=""):
        nonlocal alarm_no
        alarm_no += 1
        rows.append({
            "alarm_id": f"ALM-{alarm_no:06d}", "ne_id": device, "type": typ, "severity": sev,
            "probable_cause": prob or "", "text": text, "raised_iid": ts_iid,
            "cleared_iid": closed, "state": "cleared" if closed is not None else "active",
            "group": "TRN-042case" if device == "TRN-042" and "SFP" in text else "",
            "counters": counters or {},
        })

    # ---- the canonical TRN-042 case (brief + example email), Oct 2025
    case_day = dt.datetime(2025, 10, 14, tzinfo=c.UTC)
    base_iid = c.instant(case_day) + 36  # 09:00 local (EET = UTC+2)
    # pre-existing low-severity alarm
    emit("TRN-042", "LOS", "minor", base_iid - 480, "Loss of signal on port 1/1/3 (pre-existing, flapping)", "LOS")
    # scheduled SFP replacement earlier that morning (field work order)
    emit("TRN-042", "MODULE_REMOVED", "warning", base_iid - 60,
         "Optical module removed on port 1/1/3 (planned SFP replacement)", "MODULE")
    emit("TRN-042", "MODULE_INSERTED", "info", base_iid - 52,
         "Optical module inserted on port 1/1/3 (SFP-10G-8421)", "MODULE")
    emit("TRN-042", "LINK_UP", "info", base_iid - 50, "Interface 1/1/3 change: DOWN -> UP", "LINK")
    emit("TRN-042", "ALARM_CLEAR", "minor", base_iid - 44, "Loss of signal cleared on port 1/1/3", "LOS",
         closed=base_iid - 44)
    emit("TRN-042", "OPTICAL_POWER_OK", "info", base_iid - 40,
         "RX optical power 1/1/3 within range: -3.1 dBm (expected -8..0 dBm)", "OPTICAL")
    # stalled traffic: counters zero, no alarms, link UP -> this is the silent symptom
    # (nothing to alarm on! that is the whole point)
    # the re-bounce resolves it
    emit("TRN-042", "IF_DISABLED", "warning", base_iid + 408, "Interface 1/1/3 administratively disabled", "ADMIN")
    emit("TRN-042", "IF_ENABLED", "info", base_iid + 412, "Interface 1/1/3 administratively enabled", "ADMIN")
    emit("TRN-042", "LINK_UP", "info", base_iid + 416, "Interface 1/1/3 change: DOWN -> UP", "LINK")
    # traffic resumes after bounce -> counters start rising again

    # ---- background alarm noise
    n_bg = 420
    for _ in range(n_bg):
        dev = rng.choice(candidates)
        day = rng.randrange(c.N_INSTANTS // 96)
        iid = (day * 96) + rng.randrange(96)
        kind_roll = rng.random()
        if kind_roll < 0.45:
            typ, sev, text, cause = "LOS", rng.choice(["minor", "major"]), "Loss of signal", "LOS"
            dur = rng.randint(2, 40)
        elif kind_roll < 0.7:
            typ, sev, text, cause = "OPTICAL_DEGRADE", rng.choice(["minor", "warning"]), \
                "Optical power degraded", "OPTICAL_DEGRADE"
            dur = rng.randint(4, 60)
        elif kind_roll < 0.85:
            typ, sev, text, cause = "TEMP_HIGH", "warning", "Inlet temperature high", "TEMP_HIGH"
            dur = rng.randint(2, 12)
        else:
            typ, sev, text, cause = "ADMIN_DOWN", "warning", "Interface administratively down", "ADMIN"
            dur = rng.randint(1, 6)
        if iid + dur > c.END_IID:
            dur = 1
        emit(dev, typ, sev, iid, f"{text} on {dev}", cause, closed=iid + dur)

    rows.sort(key=lambda r: r["raised_iid"])
    for r in rows:
        r["raised"] = c.ts_s(r["raised_iid"])
        r["cleared"] = c.ts_s(r["cleared_iid"]) if r["cleared_iid"] is not None else None
    return rows, {"case_base_iid": base_iid, "sfp_replace_iid": base_iid - 52,
                  "bounce_iid": base_iid + 408, "device": "TRN-042"}


def apply_validity(net: dict, rt: dict) -> None:
    """Stamp valid_from / valid_to on every graph-layer record (see common.py for the convention).

    Runs after all random draws so it never changes the rest of the generated data.
    """
    decom = {d["node_id"]: c.date_to_instant(d["decommissioned"]) for d in net["decommissioned"]}
    dev_from = {d["device_id"]: c.date_to_instant(d["commissioned"]) for d in net["physical_devices"]}
    for code in (s["code"] for s in net["sites"]):
        dev_from.setdefault(code, c.NETWORK_EPOCH)

    def stamp(row: dict, valid_from: str | None, valid_to: str | None) -> None:
        row["valid_from"] = valid_from or c.NETWORK_EPOCH
        row["valid_to"] = valid_to

    def link_window(end_a: str, end_b: str) -> tuple[str, str | None]:
        # a link exists once both ends exist, and ends when either end is decommissioned
        starts = [dev_from.get(e, c.NETWORK_EPOCH) for e in (end_a, end_b)]
        ends = [decom[e] for e in (end_a, end_b) if e in decom]
        return max(starts), (min(ends) if ends else None)

    for s in net["sites"]:
        stamp(s, c.NETWORK_EPOCH, None)
    for d in net["physical_devices"]:
        stamp(d, dev_from[d["device_id"]], decom.get(d["device_id"]))
    for n in net["design_nodes"]:
        stamp(n, dev_from.get(n["node_id"]), decom.get(n["node_id"]))
    for seg in net["physical_segments"]:
        _, until = link_window(seg["end_a"], seg["end_b"])
        stamp(seg, c.date_to_instant(seg.get("installed")) or dev_from.get(seg["end_a"]), until)
    for n in rt["current_nodes"]:
        stamp(n, dev_from[n["node_id"]], decom.get(n["node_id"]))
    retired = {l["superseded_by"]: c.date_to_instant(l["retired_at"]) for l in rt["retired_links"]}
    for l in rt["current_links"]:
        start, until = link_window(l["end_a"], l["end_b"])
        stamp(l, max(start, retired.get(l["link_id"], start)), until)
    for l in rt["retired_links"]:
        start, _ = link_window(l["end_a"], l["end_b"])
        stamp(l, start, c.date_to_instant(l["retired_at"]))


def main() -> None:
    net = build_network()
    rt = build_runtime(net)
    events = build_device_events(net)
    alarms, case = build_alarms(net, events)
    apply_validity(net, rt)

    c.write_json(["network", "as_designed", "nodes_designed.json"], net["design_nodes"])
    cols = ["link_id", "layer", "end_a", "end_b", "design_capacity_gbps", "redundant"]
    c.write_csv(["network", "as_designed", "links_designed.csv"], cols,
                ([l[k] for k in cols] for l in net["design_links"]))
    c.write_csv(["network", "as_designed", "intent_bundles_intraday.csv"],
                ["bundle_id", "name", "owner", "description", "traffic_class", "latency_budget_ms", "valid_from"],
                [
                    ["INT-001", "volga_train_frontend", "Volga ML Platform", "Training job frontend / dataset streaming",
                     "gold", 12, "2024-01-01"],
                    ["INT-002", "volga_train_collcomm", "Volga ML Platform", "All-reduce / all-gather between GPU islands",
                     "platinum", 5, "2024-01-01"],
                    ["INT-003", "volga_checkpoint_egress", "Volga ML Platform", "Checkpoint + trace export to object store",
                     "silver", 40, "2024-01-01"],
                    ["INT-004", "volga_batch_inference", "Volga Inference", "Nightly batch inference over customer corpora",
                     "silver", 30, "2025-03-01"],
                    ["INT-005", "volga_backup_replication", "Volga SRE", "DC-to-DC backup/checkpoint replication",
                     "bronze", 120, "2025-06-01"],
                ])

    c.write_json(["network", "as_built", "devices.json"], net["physical_devices"])
    c.write_json(["network", "as_built", "segments.json"], net["physical_segments"])
    c.write_json(["network", "as_built", "sites.json"], net["sites"])

    c.write_json(["network", "as_running", "nodes_current.json"], rt["current_nodes"])
    c.write_json(["network", "as_running", "physical_node_snapshots.json"], rt["physical_node_snapshots"])
    c.write_json(["network", "as_running", "links_current.json"], rt["current_links"])
    c.write_json(["network", "as_running", "links_retired.json"], rt["retired_links"])
    c.write_json(["network", "as_running", "topology_snapshots_index.json"],
                 [{"snapshot": "2025-10-01", "node_count": len(rt["current_nodes"]),
                   "link_count": len(rt["current_links"]), "note": "topology is a point-in-time graph; see physical_node_snapshots"},
                  {"snapshot": "2026-11-06", "node_count": len(rt["current_nodes"]),
                   "link_count": len(rt["current_links"])}])

    c.write_jsonl(["network", "events", "device_events.jsonl"],
                  iter(events))

    c.write_jsonl(["network", "alarms", "alarms.jsonl"], alarms)
    c.write_json(["network", "alarms", "trn042_case.json"], {
        "note": "Trace of the canonical Elisa case. Ten days around the incident.",
        "device": case["device"], "sfp_replaced": c.ts_s(case["sfp_replace_iid"]),
        "traffic_stalled_until": c.ts_s(case["bounce_iid"]),
        "resolved": c.ts_s(case["bounce_iid"] + 8),
        "timeline": [a for a in alarms if a["ne_id"] == "TRN-042" and a["raised_iid"] >= case["case_base_iid"] - 600],
    })

    # convenience: node registry that other generators (context layer) will import
    c.write_json(["network", "registry.json"], {
        "access": [n["node_id"] for n in net["access_nodes"]],
        "transport": list(net["trn_meta"].keys()),
        "core": net["core_nodes"],
        "sites": [s["code"] for s in net["sites"]],
        "trn_meta": net["trn_meta"],
        "case": {"device": "TRN-042", "sfp_replaced": c.ts_s(case["sfp_replace_iid"]),
                 "resolved": c.ts_s(case["bounce_iid"] + 8)},
        "segments": net["physical_segments"],
    })
    print(f"network: {len(net['design_nodes'])} design nodes, {len(net['physical_devices'])} devices, "
          f"{len(rt['current_links'])} links, {len(events)} events, {len(alarms)} alarms")


if __name__ == "__main__":
    main()
