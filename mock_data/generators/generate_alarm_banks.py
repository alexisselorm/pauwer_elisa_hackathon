"""Telecom-style collection formats for the NMS / alarm banks.

  nms/zabbix/events_host{1..4}.ndjson          Zabbix problem/event JSON (hostid, triggerid, ...)
  nms/netcool/alarm{1..3}.json                 Netcool / IRIS gateway events with a recursive-tree encoding
                                              of probable-cause (legacy format, sparse)
  nms/infrared/events.log                      syslog lines (RFC 3164 style, Zulu-time in message)
  nms/blueeye/traps.csv                        SNMP-trap export (csv)
  nms/uama/alarms.jsonl                        small modern service style JSON lines
  nms/nagios/service_status.yaml               Nagios-style service state per host
  email_outlook/mail_export_index.json         folder/message index (what an Exchange export looks like)
  telemetry/spec/interface_counters.yml        the OLT-facing telemetry collector spec + data contract
"""
from __future__ import annotations

import json
import os

import common as c
import generate_network as gn

BANKS = gn.BANK_DEVICES


def zabbix(rows, rng):
    out = []
    for i, r in enumerate(rows):
        sev = {"minor": 1, "warning": 2, "major": 4, "info": 0, "critical": 5}.get(r["severity"], 1)
        out.append({
            "eventid": 7100000 + i, "clock": c.ts(r["raised_iid"]).strftime("%s"),
            **{"r_eventid": (7600000 + i) if r["cleared_iid"] is not None else 0},
            "name": r["text"], "acknowledged": 0, "severity": sev, "value": 1 if r["state"] == "active" else 0,
            "hostid": 10001 + hash(r["ne_id"]) % 78, "host_name": r["ne_id"],
            "triggerid": 20000 + i % 300, "objectid": 30000 + i % 301,
            "tags": [{"tag": "probable_cause", "value": r["probable_cause"]},
                     {"tag": "bank_id", "value": "Zabbix"}],
        })
    files = {}
    for k in range(4):
        files[k] = [r for r in out if (int(r["eventid"]) % 4) == k]
    return files


def iristree(r: dict) -> dict:
    """A small recursive probable-cause tree, the way IRIS/Netcool encodes it (sparse)."""
    root = {"field": "EventId", "value": r["alarm_id"], "children": []}
    cause = {"field": "ProbableCause", "value": r["probable_cause"], "children": []}
    if r["probable_cause"] in ("LOS", "OPTICAL_DEGRADE"):
        cause["children"].append({"field": "Port", "value": "1/1/3", "children": []})
        cause["children"].append({"field": "Module", "value": "SFP", "children": []})
    root["children"].append(cause)
    if r["ne_id"].startswith("TRN"):
        root["children"].append({"field": "NeType", "value": "Transmission", "children": []})
    return root


def syslog(rows):
    month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    lines = []
    for r in rows[:260]:
        t = c.ts(r["raised_iid"])
        lvl = {"info": 6, "warning": 4, "minor": 4, "major": 3, "critical": 2}.get(r["severity"], 4)
        msg = (f"<{100 + lvl}>{month[t.month - 1]} {t.day:2d} {c.local_dt(r['raised_iid']):%H:%M:%S} "
               f"{r['ne_id'].lower()} alarm: {r['probable_cause']} state={'clear' if r['state'] == 'cleared' else 'raised'}"
               f" text=\"{r['text']}\"")
        lines.append(msg)
    return "\n".join(lines) + "\n"


def traps(rows):
    return [[r["alarm_id"], r["ne_id"], r["probable_cause"], r["severity"],
             c.ts_s(r["raised_iid"]), "INFRA-INFRARED"] for r in rows]


def nagios():
    out = ["# Nagios service status export (synthetic)"]
    for nid in sorted({b for b in BANKS}):
        out.append(f"{nid.lower()}:host:")
        for svc, st in [("PING", "OK"), ("SSH", "OK"), ("SNMP", "OK"), ("ALARMS", "OK")]:
            out.append(f"  {svc}: {st}")
    return "\n".join(out) + "\n"


def uama(rows):
    return [{"id": r["alarm_id"], "ne": r["ne_id"], "code": r["probable_cause"],
             "sev": r["severity"], "ts": c.ts_s(r["raised_iid"])} for r in rows[::7]]


def mail_index(root: str) -> list[dict]:
    out = []
    for folder in ("Inbox", "Sent Items"):
        d = os.path.join(root, "email_outlook", folder)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.endswith(".msg"):
                continue
            stat = os.stat(os.path.join(d, f))
            out.append({"folder": folder, "file": f, "size_bytes": stat.st_size,
                        "type": "IPM.Note", "format": "Outlook MSG (OLE2/CFB)"})
    return out


def interface_counters_spec(net):
    trn = [d for d in net["physical_devices"] if d["kind"] == "optical_transport"]
    lines = [
        "# Telemetry collector spec for the transport layer (the OLT/CMDB-facing contract).",
        "# Owners: NOC-Transport. Last change: 2024-09-30",
        "apiVersion: volga.example/v1",
        "kind: PullSpec",
        "metadata:",
        "  name: interface-counters",
        "spec:",
        "  poll_interval_seconds: 900",
        "  olt_style_query: true",
        "  metrics: [in_octets_mbps, out_octets_mbps, rx_power_dbm, oper_status]",
        "  thresholds:",
        "    # THIS is where 'traffic zero' is defined for transport ports, not in the NMS banks",
        "    traffic_zero:",
        "      window_minutes: 60",
        "      condition: 'oper_status == UP and in_octets_mbps < 0.01'",
        "      action: raise TICKET, do not page",
        "  targets:",
    ]
    for d in trn:
        lines.append(f"    - ci_id: {d['device_id']}")
        lines.append(f"      port: 1/1/3")
        lines.append(f"      model_family: {d['model']}")
        lines.append(f"      generation: {net['trn_meta'][d['device_id']]['generation']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    root = c.root_dir()
    rng = c.rng_for("banks")
    with open(os.path.join(root, "network", "alarms", "alarms.jsonl"), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    net = gn.build_network()

    zb = zabbix(rows, rng)
    for k, rs in zb.items():
        c.write_jsonl(["nms", "zabbix", f"events_host{k + 1}.ndjson"], rs)

    nc_all = []
    for i, r in enumerate(rows):
        nc_all.append({
            "Identifier": r["alarm_id"], "Node": r["ne_id"], "Summary": r["text"],
            "Severity": {"info": 3, "warning": 4, "minor": 3, "major": 1, "critical": 1}.get(r["severity"], 3),
            "FirstOccurrence": c.ts_s(r["raised_iid"]), "EventTree": iristree(r), "Tally": 1,
            "Bank": "Netcool",
        })
    for k in range(3):
        c.write_jsonl(["nms", "netcool", f"alarm{k + 1}.json"], nc_all[k::3])

    c.write_text(["nms", "infrared", "events.log"], syslog(rows))
    c.write_csv(["nms", "blueeye", "traps.csv"], ["alarm_id", "ne", "cause", "severity", "ts", "bank"], traps(rows))
    c.write_text(["nms", "nagios", "service_status.yaml"], nagios())
    c.write_jsonl(["nms", "uama", "alarms.jsonl"], uama(rows))
    c.write_json(["email_outlook", "mail_export_index.json"], mail_index(root))
    c.write_text(["telemetry", "spec", "interface_counters.yml"], interface_counters_spec(net))
    print(f"banks: {len(rows)} alarms spread over {len(BANKS)} banks")


if __name__ == "__main__":
    main()
