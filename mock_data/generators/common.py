"""Shared helpers for the Pauwer/Elisa mock-data generator.

Everything is deterministic: a single global seed drives every source, so two runs
produce byte-identical files (except eml fetch timestamp / msg zip dates).

Time is handled as *instant IDs*: an ISO-8601 instant is mapped to a 15-minute slot,
and the integer slot number is what all generated rows carry. This makes it trivial
to keep the network layers, the context layer and the event stream consistent, and
lets a consumer recover the exact timestamp with `ts(iid)`.
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
# ITSM, vendor case). All UTC; 2025-10-14 is still summer time in Finland (EEST = UTC+3).
#   08:00 local  SFP removed            08:15  new SFP inserted, link UP, optics OK, alarms clear
#   09:14-09:27  Teams thread (Elisa's example timings) - the expert advises a port bounce
#   09:15        ITSM ticket: traffic zero for 60 min (telemetry spec threshold)
#   09:30        port disabled/enabled -> traffic resumes        09:45  counters back to normal
#   11:40        Erkki's recap email
# Times are snapped to the 15-minute slot grid the way the rest of the data is (to_iid = ceil).
def _case_t(hh: int, mm: int) -> dt.datetime:
    return dt.datetime(2025, 10, 14, hh, mm, tzinfo=dt.timezone.utc)


TRN042_CASE = {
    "device": "TRN-042",
    "port": "1/1/3",
    "sfp_removed": _case_t(5, 0),
    "sfp_inserted": _case_t(5, 15),
    "los_cleared": _case_t(5, 30),
    "chat_start": _case_t(6, 14),
    "ticket_opened": _case_t(6, 15),
    "bounce": _case_t(6, 30),
    "recovered": _case_t(6, 45),
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
    {"article_id": "KB0018501", "title": "BESS winter operating mode", "body": "Hold 80% SOC.",
     "valid_from": "2025-11-10", "status": "published"},
]

global_rng = random.Random(SEED)


# --------------------------------------------------------------------------- time
def instant(d: dt.datetime) -> int:
    """Map a datetime to its 15-minute slot ID (floor)."""
    d = d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    return int((d - START).total_seconds() // SLOT_SECONDS)


def to_iid(d: dt.datetime) -> int:
    """Inclusive instant-id mapping: a timestamp is an event AT that time, so a 00:00
    timestamp belongs to the slot that starts at it (i.e. ceil, not floor)."""
    d = d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    return math.ceil((d - START).total_seconds() / SLOT_SECONDS)


def ts(iid: int) -> dt.datetime:
    return START + dt.timedelta(seconds=iid * SLOT_SECONDS)


def ts_s(iid: int) -> str:
    return ts(iid).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_iso_millis(iid: int) -> str:
    return ts(iid).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def local_dt(iid: int) -> dt.datetime:
    """Instant in Finnish local time (EET/EEST via zoneinfo)."""
    try:
        from zoneinfo import ZoneInfo

        return ts(iid).astimezone(ZoneInfo("Europe/Helsinki"))
    except Exception:  # pragma: no cover - tzdata missing
        return ts(iid) + dt.timedelta(hours=2)


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

# City -> (lat, lon) used to map to the nearest FMI observation station.
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
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as fh:
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
