"""Shared helpers for the Pauwer/Elisa mock-data generator.

Everything is deterministic: a single global seed drives every source, so two runs
produce byte-identical files.

Time model
  * Event-stream rows (Teams messages, emails, ITSM tickets, Jira issues, vendor cases, alarms,
    device events) carry their EXACT event timestamp, to the second (record time = event time).
    Generators draw minutes/seconds from seeded RNGs so events are distinct and strictly ordered
    inside every conversation.
  * Only telemetry PM samples live on the 15-minute grid: a PM sample is an interval aggregate.
    Its `ts` marks the START of the interval it covers, [ts, ts + 15 min).
  * Rows may additionally carry a `*_iid` integer: the 15-minute slot that CONTAINS the instant
    (floor, `instant()`), so any event can be joined to the PM bin it falls in. It is an alignment
    key only; the ISO timestamp next to it is the truth.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import random
import string
from dataclasses import dataclass
from typing import Iterable

SEED = 20261107
UTC = dt.timezone.utc
SLOT_SECONDS = 15 * 60

# Generator window: everything is generated on whole 15-minute slots.
START = dt.datetime(2025, 10, 1, 0, 0, tzinfo=UTC)
END = dt.datetime(2026, 11, 7, 0, 0, tzinfo=UTC)  # exclusive

# ---------------------------------------------------------------- validity (valid_from / valid_to)
# Every graph-layer record (as-designed, as-built, as-running, context) carries
#   valid_from: ISO-8601 instant from which the record is true in the world
#   valid_to:   ISO-8601 instant at which it stopped being true (exclusive); null = still believed true
# Raw source systems (Teams, email, ITSM, CMDB, Jira) deliberately do NOT: inferring validity from
# them is the pipeline's job. Event-stream rows are point-in-time (record time = event time).
#
# NETWORK_EPOCH is the assumed start of validity for things that exist before the network history
# does and have no date of their own (sites, hardware models, topics). It is an assumption, not data.
NETWORK_EPOCH = "2018-01-01T00:00:00Z"
# The data snapshot is taken at END: a valid_to <= SNAPSHOT_AT is in the past.
SNAPSHOT_AT = END.strftime("%Y-%m-%dT%H:%M:%SZ")


def date_to_instant(d: str | None) -> str | None:
    """'2019-04-18' -> '2019-04-18T00:00:00Z'; full instants pass through; None stays None."""
    if d is None:
        return None
    return d if "T" in d else f"{d}T00:00:00Z"


# ---------------------------------------------------------------- the canonical TRN-042 case
# ONE timeline for the brief's SFP case, shared by every generator (alarms, telemetry, Teams, email,
# ITSM, vendor case). All UTC; 2025-10-14 is still summer time in Finland (EEST = UTC+3), so the
# Teams thread's local 09:14-09:27 (Elisa's teams_chat.json) is 06:14-06:27 UTC.
#   05:00 SFP removed (field work)        05:15 new SFP inserted, link UP     05:30 LOS cleared, optics OK
#   06:14 Teams thread starts (msg 1)     06:15 ITSM ticket: 60 min of zero traffic with oper UP
#   06:22 Erkki's advice (msg 8)          06:23 port disabled / re-enabled    06:24 traffic flows again
#   06:25 "Did that. Traffic is back" (msg 9)                                 08:40 Erkki's recap email
def _case_t(hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime(2025, 10, 14, hh, mm, ss, tzinfo=dt.timezone.utc)


# Elisa's 12 messages at their exact (UTC) times; seconds keep equal minutes strictly ordered.
TRN042_CHAT_TIMES = [
    _case_t(6, 14, 5), _case_t(6, 16, 12), _case_t(6, 16, 41), _case_t(6, 18, 3),
    _case_t(6, 18, 37), _case_t(6, 19, 22), _case_t(6, 21, 30), _case_t(6, 22, 8),
    _case_t(6, 25, 4), _case_t(6, 25, 39), _case_t(6, 26, 15), _case_t(6, 27, 2),
]

TRN042_CASE = {
    "device": "TRN-042",
    "port": "1/1/3",
    "sfp_removed": _case_t(5, 0),
    "sfp_inserted": _case_t(5, 15),
    "los_cleared": _case_t(5, 30),
    "chat_start": TRN042_CHAT_TIMES[0],
    "ticket_opened": _case_t(6, 15),
    "advice": TRN042_CHAT_TIMES[7],          # msg 8: "Disable the interface, wait a little, then enable it"
    "bounce": _case_t(6, 23),                # interface administratively disabled
    "bounce_enabled": _case_t(6, 23, 40),    # ... and enabled again ("wait a little")
    "recovered": _case_t(6, 24),             # forwarding resumes
    "traffic_back_msg": TRN042_CHAT_TIMES[8],  # msg 9: "Did that. Traffic is back"
    "recap_email": _case_t(8, 40),
}


# ITSM knowledge articles. Shared between the ITSM export (generate_structured) and the gold
# context layer (generate_context), which records when each stale article stopped being true.
ITSM_KB_ARTICLES = [
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
]

global_rng = random.Random(SEED)


# --------------------------------------------------------------------------- time
def instant(d: dt.datetime) -> int:
    """Map a datetime to the 15-minute slot that contains it (floor)."""
    d = d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    return int((d - START).total_seconds() // SLOT_SECONDS)


def to_iid(d: dt.datetime) -> int:
    """Ceil mapping onto the slot grid. Only for telemetry window bounds; events use instant()."""
    d = d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    return math.ceil((d - START).total_seconds() / SLOT_SECONDS)


def ts(iid: int) -> dt.datetime:
    return START + dt.timedelta(seconds=iid * SLOT_SECONDS)


def ts_s(iid: int) -> str:
    return ts(iid).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(d: dt.datetime) -> str:
    """Exact UTC instant, to the second: '2025-10-14T06:22:08Z'."""
    d = d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def jitter(rng: random.Random, d: dt.datetime, max_seconds: int = 59) -> dt.datetime:
    """Deterministic sub-minute (or intra-slot) offset so events are distinct, not grid-snapped."""
    return d + dt.timedelta(seconds=rng.randint(0, max_seconds))


def slot_event(rng: random.Random, iid: int) -> dt.datetime:
    """An exact instant somewhere inside slot `iid` (used where a generator picks a slot at random)."""
    return ts(iid) + dt.timedelta(seconds=rng.randint(0, SLOT_SECONDS - 1))


def ts_iso_millis(iid: int) -> str:
    return ts(iid).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def to_local(d: dt.datetime) -> dt.datetime:
    """Exact instant in Finnish local time (EET/EEST via zoneinfo)."""
    try:
        from zoneinfo import ZoneInfo

        return d.astimezone(ZoneInfo("Europe/Helsinki"))
    except Exception:  # pragma: no cover - tzdata missing
        return d + dt.timedelta(hours=2)


def local_dt(iid: int) -> dt.datetime:
    """Slot start in Finnish local time."""
    return to_local(ts(iid))


N_INSTANTS = instant(END)
START_IID = 0
END_IID = N_INSTANTS - 1
FIRST_DAY = instant(START)


def day_of(iid: int) -> int:
    return iid // 96


def date_of(iid: int) -> dt.date:
    return ts(iid).date()


def n_days() -> int:
    return (END - START).days


def rng_for(name: str, *extra) -> random.Random:
    """Independent, stable RNG per source/entity."""
    return random.Random(f"{SEED}:{name}:" + ":".join(str(e) for e in extra))


# --------------------------------------------------------------------------- words
FI_FIRST = ["Veikko", "Erkki", "Aino", "Juhani", "Mikko", "Liisa", "Pekka", "Sanna", "Timo", "Kaisa",
            "Antti", "Riikka", "Jari", "Hanna", "Heikki", "Emilia", "Markku", "Tuomas", "Eija", "Olli",
            "Petra", "Juha", "Laura", "Matti", "Kristiina", "Ville", "Anna", "Sami", "Noora", "Jouni",
            "Reetta", "Kalle", "Minna", "Taneli", "Johanna", "Riku", "Päivi", "Aleksi", "Elina", "Samuli"]
FI_LAST = ["Vikkelä", "Expert", "Korhonen", "Virtanen", "Mäkinen", "Nieminen", "Hämäläinen", "Laine",
           "Heikkinen", "Koskinen", "Järvinen", "Lehtonen", "Lehtinen", "Saarinen", "Salminen", "Heinonen",
           "Niemi", "Heikkilä", "Kinnunen", "Salonen", "Turunen", "Väänänen", "Laitinen", "Rantanen",
           "Karjalainen", "Kettunen", "Miettinen", "Hiltunen", "Hakala", "Koivisto", "Laaksonen", "Toivonen",
           "Nurmi", "Räsänen", "Aalto", "Lahtinen", "Lindholm", "Backman", "Rantala", "Savolainen"]

FI_PLACES = [
    ("Helsinki", "Helsingin alue"), ("Espoo", "Espoon alue"), ("Vantaa", "Vantaan alue"),
    ("Tampere", "Tampereen alue"), ("Turku", "Turun alue"), ("Oulu", "Oulun alue"),
    ("Jyväskylä", "Jyväskylän alue"), ("Kuopio", "Kuopion alue"), ("Lahti", "Lahden alue"),
    ("Rovaniemi", "Rovaniemen alue"), ("Kemi", "Kemin alue"), ("Ivalo", "Inarin alue"),
    ("Joensuu", "Joensuun alue"), ("Vaasa", "Vaasan alue"), ("Seinäjoki", "Seinäjoen alue"),
    ("Kajaani", "Kajaanin alue"), ("Mikkeli", "Mikkelin alue"), ("Pori", "Porin alue"),
    ("Kokkola", "Kokkolan alue"), ("Lappeenranta", "Lappeenrannan alue"), ("Hämeenlinna", "Hämeenlinnan alue"),
    ("Sodankylä", "Sodankylän alue"),
]

# City -> (lat, lon) used to place network nodes.
CITY_COORDS = {
    "Helsinki": (60.17, 24.94), "Espoo": (60.21, 24.66), "Vantaa": (60.29, 25.04),
    "Tampere": (61.50, 23.79), "Turku": (60.45, 22.27), "Oulu": (65.01, 25.47),
    "Jyväskylä": (62.24, 25.75), "Kuopio": (62.89, 27.68), "Lahti": (60.98, 25.66),
    "Rovaniemi": (66.50, 25.73), "Kemi": (65.74, 24.56), "Ivalo": (68.66, 27.54),
    "Joensuu": (62.60, 29.76), "Vaasa": (63.10, 21.62), "Seinäjoki": (62.79, 22.84),
    "Kajaani": (64.22, 27.73), "Mikkeli": (61.69, 27.27), "Pori": (61.48, 21.80),
    "Kokkola": (63.84, 23.13), "Lappeenranta": (61.06, 28.19), "Hämeenlinna": (60.99, 24.46),
    "Sodankylä": (67.42, 26.59),
}


def slugify(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch == " ":
            out.append("_")
    return "".join(out)


# --------------------------------------------------------------------------- paths
def root_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def out_path(*parts: str) -> str:
    p = os.path.join(root_dir(), *parts)
    os.makedirs(os.path.dirname(p) if not p.endswith(os.sep) else p, exist_ok=True)
    return p


def ensure_dir(*parts: str) -> str:
    p = os.path.join(root_dir(), *parts)
    os.makedirs(p, exist_ok=True)
    return p


# --------------------------------------------------------------------------- writers
def write_json(parts: Iterable[str] | str, obj, *, indent: int | None = 2) -> str:  # type: ignore[valid-type]
    rel = (parts,) if isinstance(parts, str) else tuple(parts)
    path = os.path.join(root_dir(), *rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent, default=str)
        fh.write("\n")
    return path


def write_jsonl(parts: Iterable[str] | str, rows: Iterable) -> str:  # type: ignore[valid-type]
    rel = (parts,) if isinstance(parts, str) else tuple(parts)
    path = os.path.join(root_dir(), *rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def write_csv(parts: Iterable[str] | str, header: list[str], rows: Iterable[Iterable]) -> str:  # type: ignore[valid-type]
    rel = (parts,) if isinstance(parts, str) else tuple(parts)
    path = os.path.join(root_dir(), *rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def write_ndjson_gz(parts: Iterable[str] | str, rows: Iterable) -> str:  # type: ignore[valid-type]
    import gzip

    rel = (parts,) if isinstance(parts, str) else tuple(parts)
    path = os.path.join(root_dir(), *rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import io

    # mtime=0 and no file name in the header: byte-identical output on every run
    with open(path, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=9,
                                                 mtime=0) as gz, \
            io.TextIOWrapper(gz, encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    return path


def write_text(parts: Iterable[str] | str, text: str) -> str:  # type: ignore[valid-type]
    rel = (parts,) if isinstance(parts, str) else tuple(parts)
    path = os.path.join(root_dir(), *rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def write_bytes(parts: Iterable[str] | str, data: bytes) -> str:  # type: ignore[valid-type]
    rel = (parts,) if isinstance(parts, str) else tuple(parts)
    path = os.path.join(root_dir(), *rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def jsonable_compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- misc
def rand_id(rng: random.Random, prefix: str, n: int, *, alphabet: str = "0123456789") -> str:
    return prefix + "".join(rng.choice(alphabet) for _ in range(n))


def hexid(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def uid(rng: random.Random) -> str:
    h = hexid(rng, 32)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def person(rng: random.Random) -> tuple[str, str]:
    return rng.choice(FI_FIRST), rng.choice(FI_LAST)


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def ordered_slots(start_iid: int, end_iid: int) -> range:
    return range(start_iid, end_iid + 1)

FINNISH_MONTHS = ["tammikuuta", "helmikuuta", "maaliskuuta", "huhtikuuta", "toukokuuta", "kesäkuuta",
                  "heinäkuuta", "elokuuta", "syyskuuta", "lokakuuta", "marraskuuta", "joulukuuta"]
FINNISH_WEEKDAYS = ["maanantai", "tiistai", "keskiviikko", "torstai", "perjantai", "lauantai", "sunnuntai"]


def fi_long(iid: int) -> str:
    d = local_dt(iid)
    return f"{FINNISH_WEEKDAYS[d.weekday()]} {d.day}. {FINNISH_MONTHS[d.month - 1]} {d.year} klo {d:%H:%M}"


def iso_local(iid: int) -> str:
    return local_dt(iid).strftime("%Y-%m-%dT%H:%M:%S")


def day_range(first_day: int, last_day: int) -> range:
    return range(first_day, last_day + 1)
