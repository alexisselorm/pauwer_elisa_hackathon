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
