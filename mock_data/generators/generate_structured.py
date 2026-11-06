"""Structured enterprise systems: the "there are many data sources" pile.

  itsm/tickets.jsonl            ServiceNow-style incidents/requests/change (up to ~1.2k)
  itsm/knowledge_articles.jsonl ITSM KB (partly stale, contradicts the newer chat truth)
  itsm/change_calendar.jsonl    approved change records (binds to device_events change windows)
  cmdb/ci_attributes.jsonl      CI attributes as captured from exporters (inconsistent units, aliases)
  cmdb/ci_relationships.jsonl   dependency edge list (EXPORTS from interface-counters.yml)
  jira/issues.jsonl             engineering backlog + post-incident follow-ups
  inventory/spare_parts.csv     serials, spares and last-seen rack
  field_dispatch/drivers.json    driver availability/fragility (REACH/Onni style)
  vendors/incidents.jsonl       supplier problem tickets for TNC model/line-cards
  nms/alarm_policy.yaml         silence rules and thresholds per bank device
  logs/volga_api_audit.jsonl    Volga platform audit (structured, high volume)
  logs/host_metrics.csv         per-host node-exporter style metrics (high volume)
  admin/confluence_pages_index.json  the (small!) enterprise KB index, for completeness scoring
  mes/cnc_telemetry.csv         factory shop-floor data (out-of-scope noise but a real source)
"""
from __future__ import annotations

import datetime as dt
import json
import math

import common as c
import generate_network as gn


def _t(rng, day_lo=0, day_hi=None, h_lo=5, h_hi=20):
    hi = c.n_days() if day_hi is None else day_hi
    d = c.START + dt.timedelta(days=rng.randrange(day_lo, hi),
                               hours=rng.randint(h_lo, h_hi), minutes=rng.randint(0, 59))
    return c.to_iid(d)


# --------------------------------------------------------------------------- ITSM
def itsm(net, events, alarms, ctx_files=None):
    rng = c.rng_for("itsm")
    devs = [d["device_id"] for d in net["physical_devices"] if d["kind"] != "router"]
    short = ["TRN", "OPT", "MW", "ACC", "KB", "SVC"]
    rows = []
    inc = 0
    for i in range(620):
        iid = _t(rng)
        kind = rng.choices(["incident", "request", "problem", "change"], [72, 18, 4, 6])[0]
        if kind == "incident":
            inc += 1
            dev = rng.choice(devs)
            cat = rng.choice(["OPTICAL_LOS", "OPTICAL_DEGRADE", "LAN_PORT_DOWN", "POWER", "TEMP",
                              "TRAFFIC_LOW", "SFP_FAULT", "MW_LINK"])
            prio = rng.choices([1, 2, 3, 4], [2, 13, 55, 30])[0]
            short_ = f"{rng.choice(short)}{rng.randint(1000, 9999):04d}"
            state = rng.choice(["Closed", "Resolved", "In Progress", "Awaiting Info"])
            rows.append({
                "number": f"INC{inc:07d}", "type": "incident", "short_description": short_,
                "description": f"{cat} on {dev}", "category": cat, "cmdb_ci": dev,
                "priority": prio, "state": state, "assigned_to": rng.choice(["NOC-Access", "NOC-Transport",
                                                                           "Field Ops", "SRE", "NOC-Core"]),
                "opened_iid": iid, "opened_at": c.ts_s(iid),
                "resolved_iid": None if state in ("In Progress", "Awaiting Info") else iid + rng.randint(1, 200),
                "resolution_notes": "auto-generated noise" if rng.random() < 0.85 else "",
                "related_alarm": rng.choice(alarms)["alarm_id"] if alarms else None,
            })
        elif kind == "request":
            iid = _t(rng)
            rows.append({"number": f"RITM{rng.randint(100000, 999999):06d}", "type": "request",
                         "short_description": rng.choice(["New access for contractor", "Firewall rule",
                                                          "Increase DC rack power", "New SFP order",
                                                          "Laptop", "Site visit approval"]),
                         "cmdb_ci": rng.choice(devs), "priority": rng.choice([3, 4, 5]),
                         "state": "Closed", "opened_iid": iid, "opened_at": c.ts_s(iid),
                         "assigned_to": rng.choice(["IT Service Desk", "Facilities", "Field Ops"])})
        elif kind == "change":
            iid = _t(rng)
            rows.append({"number": f"CHG{rng.randint(10000, 99999):05d}", "type": "change",
                         "short_description": rng.choice(["Firmware upgrade window", "Optical reroute",
                                                          "DC UPS test", "DNS change"]),
                         "cmdb_ci": rng.choice(devs), "priority": 3, "state": "Closed",
                         "opened_iid": iid, "opened_at": c.ts_s(iid),
                         "planned_end": c.ts_s(iid + 12),
                         "change_approval": "approved"})
        else:
            iid = _t(rng)
            rows.append({"number": f"PRB{rng.randint(1000, 3999):04d}", "type": "problem",
                         "short_description": rng.choice(["Recurring LOS on GEN1 nodes",
                                                          "Microwave flapping after wind",
                                                          "Power meter drift"]),
                         "cmdb_ci": rng.choice(devs), "priority": rng.choice([2, 3]), "state": "Open",
                         "opened_iid": iid, "opened_at": c.ts_s(iid)})
    # deterministic canonical tickets tied to the case
    case = c.to_iid(dt.datetime(2025, 10, 14, 7, 0))
    rows.append({"number": "INC00420001", "type": "incident", "short_description": "TRN-042 traffic not recovering",
                 "description": "After SFP replacement on TRN-042 port 1/1/3, interface UP, optics OK, alarms "
                                "cleared, no traffic counters.",
                 "category": "TRAFFIC_LOW", "cmdb_ci": "TRN-042", "priority": 2, "state": "Resolved",
                 "assigned_to": "NOC-Transport", "opened_iid": case, "opened_at": c.ts_s(case),
                 "resolved_iid": case + 394, "resolution_notes": "Admin bounce of 1/1/3 restored traffic.",
                 "related_alarm": "ALM-000002"})
    rows.sort(key=lambda r: r["opened_iid"])
    for row in rows:
        for k in ("resolved_iid",):
            if row.get(k) is not None:
                row[k] = c.ts_s(row[k])

    # ITSM KB: mostly anodyne, but includes one contradicted entry
    kb = [
        {"article_id": "KB0012345", "title": "How to replace an SFP on a transmission node",
         "body": "Power down the port, swap module, verify optics. If traffic does not return, open a vendor "
                 "case.", "valid_from": "2022-01-10", "status": "published"},
        {"article_id": "KB0017001", "title": "TRN-042 known issue: no traffic after SFP replacement",
         "body": "Known issue on TRN-042 only. Workaround: reboot the whole node (we saw it fix it twice).",
         "valid_from": "2024-03-02", "status": "published"},
        {"article_id": "KB0018222", "title": "MTU values for core links", "body": "9114 bytes, see change "
                                                                                "approval for exceptions.",
         "valid_from": "2025-12-15", "status": "published"},
        {"article_id": "KB0018420", "title": "Kemi site access", "body": "Private road, gate. Ask Pekka for "
                                                                        "the key.",
         "valid_from": "2023-06-30", "status": "published"},
        {"article_id": "KB0018501", "title": "BESS winter operating mode", "body": "Hold 80% SOC.",
         "valid_from": "2025-11-10", "status": "published"},
    ]
    # KB conflicts (this is the 'structured systems are stale' lesson):
    kb_conflicts = {
        "KB0017001": "CONFLICT with Teams/email: reboot is the wrong action; bounce the port. Also the issue is "
                     "not TRN-042-only.",
        "KB0018420": "CONFLICT with the 2026 email: there is no key any more; code lock, custodian Heikki.",
        "KB0018501": "CONFLICT with the April 2026 email: aFRR cycling mode, SOC band 35-70%.",
        "KB0018222": "CONFLICT with the 2026 Teams message: MTU is now 9216.",
    }
    for a in kb:
        a["conflict_note"] = kb_conflicts.get(a["article_id"], "")

    calendar = [{"change_id": f"CHG{ev['workorder_id'].split('-')[-1].rjust(5, '0')}",
                 "ci": ev["device_id"], "start": c.ts_s(ev["event_time_iid"]),
                 "end": c.ts_s(ev["end_time_iid"]), "description": ev["description"],
                 "approved": True, "risk": "low"} for ev in events]
    return rows, kb, calendar


# --------------------------------------------------------------------------- CMDB
def cmdb(net):
    rng = c.rng_for("cmdb")
    devs = net["physical_devices"]
    attrs = []
    for d in devs:
        dev = d["device_id"]
        row = {
            "ci_id": dev, "ci_class": {"router": "cmdb_ci_ip_router", "optical_transport": "cmdb_ci_transport_node",
                                       "aggregation_switch": "cmdb_ci_netgear",
                                       "microwave_radio": "cmdb_ci_microwave"}[d["kind"]],
            "name": d["name"], "model_id": d["model"], "vendor": d["vendor"], "serial_number": d["serial"],
            "firmware_version": d["firmware"], "install_date": d["commissioned"], "location": d["room"],
            "owner": rng.choice(["NOC-Transport", "NOC-Access", "NOC-Core"]),
            "u_position": d.get("rack"),
            "attachments": [],
        }
        if d["kind"] == "optical_transport":
            row["attachments"].append({
                "file_name": "interface-counters.yml",
                "content_type": "text/yaml",
                "sys_attachment": f"{c.hexid(rng, 32)}",
                "payload": "apiVersion: v1\nkind: TelemetryCollector\nspec:\n"
                           f"  ci_id: {dev}\n  poll_interval_seconds: 900\n"
                           "  metrics:\n    - in_octets_mbps\n    - out_octets_mbps\n    - rx_power_dbm\n"
                           "  thresholds:\n    traffic_zero: {window_minutes: 60}\n"
                           "  owner_team: NOC-Transport\n  enabled: true\n",
            })
        if d["kind"] == "microwave_radio":
            row["attachments"].append({
                "file_name": "radio_config_export.txt", "content_type": "text/plain",
                "payload": f"radio={d['model']} mode=ethernet channel_plan_id={c.hexid(rng, 6)} "
                           "channel_width_mhz=56 modulation=qam256\n",
            })
        attrs.append(row)

    edges = []
    # physical containment
    for d in devs:
        if d.get("nearest_uplink"):
            edges.append({"source": d["nearest_uplink"], "target": d["device_id"], "type": "contains",
                          "discovered_by": "Site Discovery"})
    for nid, meta in net["trn_meta"].items():
        edges.append({"source": "CR-001" if meta["city"] in ("Helsinki", "Espoo") else "CR-003",
                      "target": nid, "type": "connects_to", "discovered_by": "service_mapping"})
    for s in gn.SITES:
        edges.append({"source": s[0], "target": "CR-001" if s[2] in ("Helsinki", "Espoo") else
                      ("CR-003" if s[2] == "Tampere" else "CR-005"), "type": "powered_by",
                      "discovered_by": "manual"})
    return attrs, edges


# --------------------------------------------------------------------------- Jira
def jira(net):
    rng = c.rng_for("jira")
    rows = []
    projects = ["NETOPS", "OBS", "IMPL", "SRE", "VOLGA"]
    summaries = [
        "Add signal-based alert for UP+zero counters on transport ports",
        "Normalise hardware_class slugs (tnc2_roadm vs TNC-2X)",
        "Automate post-SFP-swap verification",
        "Silence noisy OPTICAL_DEGRADE alarms",
        "Model private-road access in CMDB",
        "Migrate BESS dashboards to aFRR SOC band",
        "Data quality: PUE outliers in customer reporting",
        "Spare-parts forecast for Gen1 units",
        "KMAX-4K config export before reset",
        "Teams export connector for context capture",
        "Weekly report for silent-metadata coverage",
        "Deduplicate alarm storms",
        "Switch MTU from 9114 to 9216 on core",
        "Deprecate KB0012345",
        "Add 'valid_to' to CMDB attributes",
        "Route-dispatch: factor in gate access time",
    ]
    for i in range(260):
        iid = _t(rng)
        st = rng.choices(["Open", "In Progress", "Done", "Won't Do"], [35, 20, 35, 10])[0]
        rows.append({
            "key": f"{rng.choice(projects)}-{rng.randint(100, 4999)}", "summary": rng.choice(summaries),
            "type": rng.choice(["Task", "Bug", "Story"]), "status": st,
            "priority": rng.choices(["Highest", "High", "Medium", "Low"], [5, 20, 55, 20])[0],
            "reporter": rng.choice(["NOC-Transport", "SRE", "NOC-Access"]),
            "created_iid": iid, "created": c.ts_s(iid),
            "labels": rng.sample(["context-capture", "telemetry", "cmdb", "silent-metadata", "alarms", "energy"],
                                 k=rng.randint(0, 3)),
        })
    # canonical: the post-case follow-up that was never done (the knowledge never got documented)
    t = c.to_iid(dt.datetime(2025, 10, 23, 8, 15))
    rows.append({"key": "NETOPS-4821", "summary": "Document TRN-042 forwarding bug workaround in runbook",
                 "type": "Task", "status": "In Progress", "priority": "Medium", "reporter": "Erkki Expert",
                 "assignee": "Erkki Expert", "created": c.ts_s(t), "created_iid": t, "updated": c.ts_s(t + 500),
                 "labels": ["silent-metadata", "runbook"],
                 "description": "Erkki, 2025-10-23: 'I'll put it in the runbook when I get a minute.' Still open. "
                                "The fix is only in Teams + one email."})
    rows.append({"key": "IMPL-1088", "summary": "Kemi site access register entry missing",
                 "type": "Task", "status": "Open", "priority": "High", "reporter": "Timo Heikkinen",
                 "created": c.ts_s(c.to_iid(dt.datetime(2026, 8, 18, 10, 25))),
                 "created_iid": c.to_iid(dt.datetime(2026, 8, 18, 10, 25)),
                 "labels": ["access", "silent-metadata"],
                 "description": "Code lock replaced the padlock in 2026-08 but the access register (Confluence) was "
                                "not updated. The code lives in an email."})
    rows.sort(key=lambda r: r["created_iid"])
    return rows


# --------------------------------------------------------------------------- inventory / dispatch / vendors
def inventory(net):
    rng = c.rng_for("inv")
    rows = []
    for d in net["physical_devices"]:
        rows.append([d["serial"], d["device_id"], d["model"], d["vendor"], "installed", d["room"],
                     d["commissioned"], "in_warranty" if d["warranty_end"] > "2026-11-06" else "out_of_warranty"])
    part_models = ["SFP-10G-8421", "SFP-25G-2102", "SFP-100G-3300", "KMAX-4K-RF", "PSU-1200", "CH-1X-2"]
    for i in range(360):
        rows.append([f"SP{c.rand_id(rng, '', 8, alphabet='0123456789ABCDEF')}", f"SPARE-{i:05d}",
                     rng.choice(part_models), rng.choice(["Nokia", "Mitsubishi"]),
                     rng.choice(["spare_in_stock", "spare_in_stock", "consumed", "on_order"]),
                     rng.choice(["Kemi warehouse", "Rovaniemi depot", "Helsinki central", "Tampere central"]),
                     f"2024-{rng.randint(1, 12):02d}-{rng.randint(10, 28)}",
                     rng.choice(["new", "refurb"])])
    return rows


def drivers():
    rng = c.rng_for("drivers")
    riders = []
    names = ["Onni Aalto", "Kalle Virtanen", "Sami Lahtinen", "Tuomas Mäkinen", "Ville Nieminen",
             "Reino Kettunen", "Pentti Karjalainen", "Olli Hakala", "Juha Rantanen", "Matti Lindholm",
             "Jouko Savolainen", "Arto Nurmi", "Harri Räsänen", "Ilpo Aho", "Timo Backman", "Pekka Rantala",
             "Vesa Kinnunen", "Esa Turunen", "Salla Väänänen", "Nina Huhtala"]
    for i, n in enumerate(names):
        riders.append({
            "driver_id": f"DRV-{i + 1:03d}", "name": n,
            "districts": rng.sample(["North-FI", "East-FI", "West-FI", "South-FI", "Central-FI"], k=rng.randint(1, 2)),
            "seat_capacity": rng.choice([6, 8, 12, 16]),
            "cargo_capacity_kg": rng.choice([120, 160, 220]),
            "has_boomlift_ticket": rng.random() < 0.4,
            "assigned_to": rng.choice(["Onni", "REACH"]),
            "availability_this_month": round(rng.uniform(0.45, 0.98), 2),
            "vulnerability": rng.choice(["none", "contract_ending_soon", "cities_adjusting_fees", "low_volume_flag"]),
        })
    return {"tenant": "volga.example", "source": "REACH-Freelancer + Onni export (synthetic)", "riders": riders}


def vendor_incidents(net):
    rng = c.rng_for("vendor")
    trn = list(net["trn_meta"])
    rows = []
    for i in range(90):
        iid = _t(rng, h_lo=0, h_hi=22)
        rows.append({"vendor": "Nokia", "case_id": f"NSD-{rng.randint(1000000, 9999999)}",
                     "product": rng.choice(["TNC-1X", "TNC-2X", "TNC-3X", "7750 SR-14s"]),
                     "feature": rng.choice(["line card", "SFP handling", "forwarding plane", "power supply"]),
                     "silent_call_home_count": rng.randint(0, 40),
                     "affected_serial": rng.choice(net["physical_devices"])["serial"],
                     "node_id": rng.choice(trn), "opened": c.ts_s(iid),
                     "status": rng.choice(["open", "resolved", "closed"]), "severity": rng.choice(["S3", "S3", "S2"]),
                     "summary": "SFP swap -> forwarding table not updated (intermittent)"})
    # the one that matters: a silent call-home on TRN-042 around the SFP swap
    iid = c.to_iid(dt.datetime(2025, 10, 14, 16, 20))
    rows.append({"vendor": "Nokia", "case_id": "NSD-7041982", "product": "TNC-1X",
                 "feature": "forwarding plane", "silent_call_home_count": 14,
                 "affected_serial": [d["serial"] for d in net["physical_devices"] if d["device_id"] == "TRN-042"][0],
                 "node_id": "TRN-042", "opened": c.ts_s(iid), "status": "resolved", "severity": "S3",
                 "summary": "After optical module replacement, firmware failed to update the forwarding state "
                            "table; interface stays UP with zero forwarded frames. Port re-initialisation fixes it.",
                 "fix": "disable/enable interface; vendor bug fixed in v6.2.1"})
    rows.sort(key=lambda r: r["opened"])
    return rows


def nms_policy() -> str:
    out = ["# NMS alarm policy (Zabbix / Netcool / Infrared / Blueeye / Uama / Nagios parity)",
           'policy_version: "2026-03-11"', "banks:"]
    for b in gn.BANK_DEVICES:
        out += [f"  - device: {b}", '    silence_windows:', '      - "02:00-03:00 Europe/Helsinki"',
                "    mute_rules:", '      - when: "change_approval in {approved, scheduled}"', "        suppress: true"]
    out += ["thresholds:", "  traffic_zero:",
            "    # NOT configured here for transport nodes - see the interface-counters.yml attachment",
            "    enabled: false",
            "  optical_degrade:", "    rx_power_dbm: {min: -9.0, max: 0.0}",
            "  temperature:", "    inlet_c: {min: 5, max: 30}", ""]
    return "\n".join(out)


def logs_volga(net):
    rng = c.rng_for("volga_logs")
    hosts = [f"{s[0].lower()}-gpu-{i:03d}" for s in gn.SITES for i in range(1, 9)]
    users = ["volga-orchestrator", "ml-trainer", "batch-scheduler", "sre-oncall", "ci-runner", "s3-syncer"]
    rows = []
    idx = 0
    for day in range(c.n_days()):
        for h in range(24):
            for _ in range(5):
                idx += 1
                t = c.ts(c.to_iid(c.START + dt.timedelta(days=day, hours=h, minutes=rng.randint(0, 59))))
                rows.append({
                    "ts": c.ts_s(c.to_iid(t)), "level": rng.choices(["info", "warn", "error"], [88, 9, 3])[0],
                    "service": rng.choice(["train-orchestrator", "gpu-scheduler", "artifact-store",
                                           "kafka-consumer", "auth-gateway"]),
                    "host": rng.choice(hosts), "user": rng.choice(users),
                    "msg": rng.choice(["job started", "job completed", "checkpoint uploaded",
                                       "gpu alloc retry", "heartbeat", "token refreshed"]),
                    "latency_ms": rng.randint(2, 900),
                    "audit": False,
                    "severity_num": 6,
                })
    # security-adjacent audit events (structured but sparse) - noise for the linkage problem
    for i in range(1200):
        iid = rng.randrange(c.N_INSTANTS)
        rows.append({"ts": c.ts_s(iid), "level": "warn", "service": "auth-gateway",
                     "host": "auth-01", "user": rng.choice(["svc-backup", "operator", "unknown"]),
                     "msg": "login from new IP", "latency_ms": 0, "audit": True,
                     "severity_num": 4})
    rows.sort(key=lambda r: r["ts"])
    return rows


def host_metrics():
    rng = c.rng_for("hostm")
    hosts = [f"{s[0].lower()}-gpu-{i:02d}" for s in gn.SITES for i in range(1, 4)]
    rows = []
    for day in range(c.n_days()):
        for i, host in enumerate(hosts):
            base = 40 + 10 * math.sin(day / 30 + i)
            for hh in range(0, 24, 2):
                t = c.ts(c.to_iid(c.START + dt.timedelta(days=day, hours=hh)))
                rows.append([t, host, round(base + rng.gauss(0, 5), 1), round(rng.uniform(20, 95), 1),
                             round(rng.uniform(0.5, 9.5), 1)])
    return rows


def mes():
    rng = c.rng_for("mes")
    machines = [f"CNC-{i:02d}" for i in range(1, 13)]
    rows = []
    for day in range(c.n_days()):
        for m in machines:
            for hh in range(0, 24, 3):
                t = c.ts(c.to_iid(c.START + dt.timedelta(days=day, hours=hh)))
                rows.append([t, m, rng.choice(["RUN", "IDLE", "SETUP", "STOP"]), rng.randint(10, 90),
                             rng.randint(0, 3)])
    return rows


def main() -> None:
    net = gn.build_network()
    # reuse the existing event/alarm outputs
    with open(c.root_dir() + "/network/events/device_events.jsonl", encoding="utf-8") as fh:
        events = [json.loads(l) for l in fh]
    with open(c.root_dir() + "/network/alarms/alarms.jsonl", encoding="utf-8") as fh:
        alarms = [json.loads(l) for l in fh]

    tickets, kb, calendar = itsm(net, events, alarms)
    c.write_jsonl(["itsm", "tickets.jsonl"], tickets)
    c.write_jsonl(["itsm", "knowledge_articles.jsonl"], kb)
    c.write_jsonl(["itsm", "change_calendar.jsonl"], calendar)

    attrs, edges = cmdb(net)
    c.write_jsonl(["cmdb", "ci_attributes.jsonl"], attrs)
    c.write_jsonl(["cmdb", "ci_relationships.jsonl"], edges)

    iss = jira(net)
    c.write_jsonl(["jira", "issues.jsonl"], iss)

    c.write_csv(["inventory", "spare_parts.csv"],
                ["serial", "asset_id", "model", "vendor", "status", "location", "date", "condition"], inventory(net))
    c.write_json(["field_dispatch", "drivers.json"], drivers())
    c.write_jsonl(["vendors", "incidents.jsonl"], vendor_incidents(net))
    c.write_text(["nms", "alarm_policy.yaml"], nms_policy())
    c.write_jsonl(["logs", "volga_api_audit.jsonl"], logs_volga(net))
    c.write_csv(["logs", "host_metrics.csv"], ["ts", "host", "cpu_pct", "mem_pct", "disk_io_mbps"],
                host_metrics())
    c.write_csv(["mes", "cnc_telemetry.csv"], ["ts", "machine", "state", "cycle_time_s", "scrap_count"], mes())
    c.write_json(["admin", "confluence_pages_index.json"], {
        "source": "Confluence REST export (index only)", "pages": 41, "pages_with_content": 4,
        "note": "Only 4 of 41 indexed pages have bodies in this export; the KB is largely empty/stale.",
    })
    print(f"structured: {len(tickets)} tickets, {len(attrs)} CIs, {len(edges)} edges, {len(iss)} jira issues")


if __name__ == "__main__":
    main()
