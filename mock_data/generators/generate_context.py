"""Context layer: the human-to-human channels where the silent metadata lives.

Outputs (all under mock_data/):

  teams/channels.json                       channel + member directory
  teams/messages/{channel}.jsonl            raw message objects (threaded, reactions, attachments)
  teams/exports/{channel}.html              the channel rendered like a Teams "conversation export"
  email_outlook/Inbox/*.msg                 real Outlook .msg (OLE2/CFB) files
  email_outlook/Sent Items/*.msg
  email_outlook/Attachments/...             attachments referenced by the .msg files
  attachments/current/*                     the documents attached to messages (pdf, html, xlsx)
  documents/expert_notes/*.pdf              scanned-looking expert notes (PDF 1.4, Type1 fonts)
  documents/confluence/*.html               KB pages, some stale/conflicting
  knowledge_graph/entities.jsonl            entities any assertion may bind to
  knowledge_graph/assertions.jsonl          GOLD: ContextAssertion records w/ valid_from/valid_to
  knowledge_graph/contradictions.json       the designed conflicts, explained
  knowledge_graph/derived_insights.json     conclusions that need >=2 layers
  knowledge_graph/access_register.jsonl     keys/codes/callers for gated sites
  knowledge_graph/drop_rate_sheet.*         per-channel capture-rate (email vs Teams)

Conventions
  - Instants use the same 0.25h grid as telemetry (common.instant); all text timestamps
    are converted with to_iid so they are comparable to telemetry rows.
  - Every assertion carries source_ref = the concrete raw artefact id (msg id, chat id).
  - "Specialist grammar" (TRE, DC2, "the old units", "tnc2_roadm") is used inconsistently;
    see entity_aliases.csv.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import random
import struct
import zlib

import common as c
import generate_network as gn

D = c.to_iid  # inclusive instant


def _dt(y, m, d, hh=0, mm=0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=c.UTC)


class Ctx:
    def __init__(self):
        self.entities: list[dict] = []
        self.assertions: list[dict] = []
        self.contradictions: list[dict] = []
        self.insights: list[dict] = []
        self.access_rows: list[dict] = []
        self._aseq = 0
        self._nick = self._people()

    # ---------------------------------------------------------------- people
    @staticmethod
    def _people() -> dict[str, dict]:
        """Hand-authored core cast + generated colleagues. Display names are unstable on purpose."""
        people = {
            "veikko": {"name": "Veikko Vikkelä", "upn": "veikko.vikkela@volga.example", "team": "Field Ops",
                       "role": "Field Engineer", "city": "Kemi", "teams_id": "u-1001",
                       "aliases": ["Veikko V.", "Veikko", "vikkela"]},
            "erkki": {"name": "Erkki Expert", "upn": "erkki.expert@volga.example", "team": "NOC-Transport",
                      "role": "Principal Network Expert", "city": "Espoo", "teams_id": "u-1002",
                      "aliases": ["Erkki E.", "Erkki", "erkki.expert"]},
            "aino": {"name": "Aino Korhonen", "upn": "aino.korhonen@volga.example", "team": "NOC-Transport",
                     "role": "Transport Engineer", "city": "Helsinki", "teams_id": "u-1003",
                     "aliases": ["Aino K."]},
            "juhani": {"name": "Juhani Virtanen", "upn": "juhani.virtanen@volga.example", "team": "NOC-Access",
                       "role": "Access Engineer", "city": "Oulu", "teams_id": "u-1004", "aliases": ["Juhani"]},
            "mikko": {"name": "Mikko Mäkinen", "upn": "mikko.makinen@volga.example", "team": "NOC-Core",
                      "role": "Core Engineer", "city": "Espoo", "teams_id": "u-1005", "aliases": ["Mikko"]},
            "liisa": {"name": "Liisa Nieminen", "upn": "liisa.nieminen@volga.example", "team": "Facilities",
                      "role": "Facilities Manager", "city": "Tampere", "teams_id": "u-1006", "aliases": ["Liisa"]},
            "pekka": {"name": "Pekka Hämäläinen", "upn": "pekka.hamalainen@volga.example", "team": "Field Ops",
                      "role": "Senior Field Technician", "city": "Rovaniemi", "teams_id": "u-1007", "aliases": ["Pekka"]},
            "sanna": {"name": "Sanna Laine", "upn": "sanna.laine@volga.example", "team": "SRE",
                      "role": "Site Reliability Engineer", "city": "Helsinki", "teams_id": "u-1008", "aliases": ["Sanna"]},
            "timo": {"name": "Timo Heikkinen", "upn": "timo.heikkinen@volga.example", "team": "Security",
                     "role": "Security Lead", "city": "Espoo", "teams_id": "u-1009", "aliases": ["Timo"]},
            "riikka": {"name": "Riikka Koskinen", "upn": "riikka.koskinen@volga.example", "team": "SRE",
                       "role": "Platform Engineer", "city": "Tampere", "teams_id": "u-1010", "aliases": ["Riikka"]},
            "markku": {"name": "Markku Järvinen", "upn": "markku.jarvinen@volga.example", "team": "NOC-Services",
                       "role": "Service Manager", "city": "Helsinki", "teams_id": "u-1011", "aliases": ["Markku"]},
            "heikki": {"name": "Heikki Lehtonen", "upn": "heikki.lehtonen@volga.example", "team": "Field Ops",
                       "role": "Contractor Coordinator", "city": "Oulu", "teams_id": "u-1012", "aliases": ["Heikki"]},
            "emilia": {"name": "Emilia Lehtinen", "upn": "emilia.lehtinen@volga.example", "team": "NetEng",
                       "role": "Network Architect", "city": "Espoo", "teams_id": "u-1013", "aliases": ["Emilia"]},
            "olga": {"name": "Olga Saarinen", "upn": "olga.saarinen@volga.example", "team": "Energy",
                     "role": "Energy Analytics Lead", "city": "Tampere", "teams_id": "u-1014", "aliases": ["Olga"]},
        }
        rng = c.rng_for("extra_people")
        for i in range(18):
            fn, ln = c.person(rng)
            key = f"gen{i:02d}"
            people[key] = {
                "name": f"{fn} {ln}", "upn": f"{fn.lower()}.{ln.lower()}@volga.example",
                "team": rng.choice(["Field Ops", "NOC-Access", "NOC-Transport", "NOC-Core", "SRE",
                                    "Facilities", "Energy"]),
                "role": rng.choice(["Engineer", "Technician", "Analyst", "Specialist"]),
                "city": rng.choice(list(c.CITY_COORDS)), "teams_id": f"u-{1100 + i}",
                "aliases": [f"{fn} {ln[0]}."],
            }
        return people

    def p(self, key: str) -> dict:
        return self._nick[key]

    # ---------------------------------------------------------------- entities & assertions
    def entity(self, etype: str, eid: str, **attrs) -> str:
        e = {"entity_id": eid, "type": etype, **attrs}
        self.entities.append(e)
        return eid

    def assert_(self, *, subject: str, topic: str, statement: str, valid_from: int, valid_to: int | None,
                source: dict, confidence: str = "high", status: str = "believed_true",
                supersedes: str | None = None, contradicts: list[str] | None = None,
                qualifiers: dict | None = None, layer: str = "context_layer",
                about_entity_kind: str = "node") -> str:
        self._aseq += 1
        aid = f"ASR-{self._aseq:04d}"
        vf = c.ts(valid_from)
        self.assertions.append({
            "assertion_id": aid,
            "subject_entity_id": subject,
            "about_entity_kind": about_entity_kind,
            "layer": layer,
            "topic": topic,
            "statement": statement,
            "qualifiers": qualifiers or {},
            "valid_from": vf.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valid_from_iid": valid_from,
            "valid_to": (c.ts(valid_to).strftime("%Y-%m-%dT%H:%M:%SZ") if valid_to is not None else None),
            "valid_to_iid": valid_to,
            "recorded_at": c.ts_s(source.get("recorded_iid", valid_from)),
            "source_type": source["type"],
            "source_ref": source["ref"],
            "source_detail": source.get("detail", ""),
            "asserted_by": source.get("by", ""),
            "confidence": confidence,
            "status": status,
            "supersedes": supersedes,
            "contradicts": contradicts or [],
        })
        return aid


# ============================================================================ helpers
def mkdir_rel(*parts) -> str:
    return c.ensure_dir(*parts)


def msg_id(rng: random.Random) -> str:
    return f"<{c.rand_id(rng, '', 16, alphabet='0123456789ABCDEF')}.{c.rand_id(rng, '', 12, alphabet='abcdefghijklmnopqrstuvwxyz')}@volga.example>"


# ============================================================================ MSG (CFB/OLE2) writer
def _cfb_dir_entries(stream_name: str, size: int, start_sector: int) -> bytes:
    """Directory with two entries laid out exactly like Outlook:

      0: Root Entry (child -> 1, stream container, size = mini-stream size)
      1: __properties_version1.0 (right sibling of root, in the FAT (not mini) stream
         in real files, but a plain stream works for readers)

    The root's 'child' points at entry 1 so tools that walk the tree find it.
    """
    def entry(name16: str, etype: int, right: int, child: int, start: int, size_: int, clsid=b"\0"*16) -> bytes:
        # CFB directory entry, exactly 128 bytes:
        #   name[64] name_len(2) type(1) color(1) left(4) right(4) child(4) CLSID(16)
        #   state_bits(4) ctime(8) mtime(8) start_sector(4) size(8)
        raw = name16.encode("utf-16-le")
        name = raw.ljust(64, b"\0")
        return (name + struct.pack("<HBB", len(raw) + 2, etype, 1) +
                struct.pack("<III", 0xFFFFFFFF, right, child) +
                clsid + struct.pack("<I", 0) + b"\0" * 8 + b"\0" * 8 +
                struct.pack("<I", start) + struct.pack("<Q", size_))

    root = entry("Root Entry", 5, 0xFFFFFFFF, 1, 0, 0)
    stream = entry(stream_name, 2, 0xFFFFFFFF, 0xFFFFFFFF, start_sector, size)
    return (root + stream).ljust(512, b"\0")


def make_msg(path: str, *, subject: str, sender_name: str, sender_addr: str, to: list[tuple[str, str]],
             cc: list[tuple[str, str]], body: str, html: str | None, when: dt.datetime,
             message_id: str, in_reply_to: str | None, thread_topic: str, attachments: list[tuple[str, bytes]],
             headers: dict[str, str] | None = None) -> str:
    """Write a minimal but valid Outlook .msg (OLE2 compound file, CFB v3).

    Real Outlook writes many properties; we write a subset that mail libraries /
    forensic tools will parse: subject, sender, recipients, dates, body, html, headers.
    """
    import re

    props = []  # (property_tag, type, payload)
    P = lambda tag, typ, payload: props.append((tag, typ, payload))

    def s(payload: str):
        return payload.encode("utf-16-le") + b"\0\0"

    oid = 0x0000
    P(0x001a, 0x001F, s("IPM.Note"))
    P(0x0037, 0x001F, s(subject))
    P(0x0039, 0x0040, struct.pack("<Q", int(when.timestamp() * 10_000_000 + 116444736000000000)))
    P(0x0e03, 0x0003, struct.pack("<I", 1))
    P(0x0e06, 0x0040, struct.pack("<Q", int(when.timestamp() * 10_000_000 + 116444736000000000)))
    P(0x0e07, 0x0003, struct.pack("<I", 0))  # flags
    P(0x0e17, 0x0003, struct.pack("<I", 0))  # importance normal
    P(0x0e08, 0x0003, struct.pack("<I", 0))  # unread
    P(0x0e1d, 0x001F, s(thread_topic or subject))
    P(0x1000, 0x001F, s(body))
    if html:
        P(0x1013, 0x0102, html.encode("utf-8"))
    P(0x1035, 0x001F, s(message_id))
    if in_reply_to:
        P(0x1042, 0x001F, s(in_reply_to))
    hdrs = dict(headers or {})
    hdrs.setdefault("From", f"{sender_name} <{sender_addr}>")
    hdrs.setdefault("To", "; ".join(f"{n} <{a}>" for n, a in to))
    hdrs.setdefault("Subject", subject)
    hdrs.setdefault("Date", when.strftime("%a, %d %b %Y %H:%M:%S +0000"))
    hdrs.setdefault("Message-ID", message_id)
    if in_reply_to:
        hdrs.setdefault("In-Reply-To", in_reply_to)
    P(0x007d, 0x001F, s("\r\n".join(f"{k}: {v}" for k, v in hdrs.items())))
    P(0x0c1a, 0x001F, s(sender_name))
    P(0x0c1e, 0x001F, s(sender_addr))
    P(0x0c1f, 0x001F, s(sender_addr))
    P(0x0e04, 0x001F, s("; ".join(f"{n} <{a}>" for n, a in to)))
    if cc:
        P(0x0e03 + 1, 0x001F, s("; ".join(f"{n} <{a}>" for n, a in cc)))
    P(0x0e1f, 0x000B, struct.pack("<b", 0))  # has attachments
    for i, (fname, _data) in enumerate(attachments):
        P(0x3703, 0x001F, s(fname))
        P(0x3704, 0x001F, s(fname))
        P(0x3705, 0x0003, struct.pack("<I", 1))
        P(0x3707, 0x001F, s(fname))
        P(0x370b, 0x0003, struct.pack("<i", len(_data)))

    def roundup(n, to=8):
        return (n + to - 1) // to * to

    # property stream: property headers then values
    n_props = len(props)
    header_size = n_props * 16
    while header_size % 32:
        header_size += 16
        n_props += 1  # an empty sentinel header, tag 0, is fine
    propstream = bytearray(header_size)
    value_off = header_size
    # Values live after the header area; each value carries its offset/length in its header.
    values = bytearray()
    for i, (tag, typ, payload) in enumerate(props):
        if len(payload) > 4096:
            raise ValueError("payload too large for in-stream property")
        voff = header_size + len(values)
        struct.pack_into("<HHI", propstream, i * 16, typ, 0x00000006 if len(payload) < 8 else 0x00000000, tag)
        struct.pack_into("<II", propstream, i * 16 + 8, voff, len(payload))
        values += payload
        values += b"\0" * (roundup(len(values)) - len(values))
    blob = bytes(propstream) + bytes(values)
    # ---- CFB container: 512-byte sectors. Layout: 0=FAT, 1=directory, 2..=stream data.
    sector = 512
    directory = _cfb_dir_entries("__properties_version1.0", len(blob), 2)
    assert len(directory) == sector, len(directory)
    n_data = max(1, (len(blob) + sector - 1) // sector)
    # Sector layout: 0=FAT,1=directory,2..=data
    fat_entries = [0xFFFFFFFD, 0xFFFFFFFE]
    for i in range(n_data):
        fat_entries.append(i + 3 if i < n_data - 1 else 0xFFFFFFFE)
    while len(fat_entries) < 128:
        fat_entries.append(0xFFFFFFFF)
    fat = b"".join(struct.pack("<I", x) for x in fat_entries[:128])
    # exactly one directory sector (512 B) = four 128-byte entries: root, property stream, 2 unused
    data = blob.ljust(n_data * sector, b"\0")
    header = bytearray(512)
    struct.pack_into("<8s", header, 0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    struct.pack_into("<H", header, 24, 3)          # minor version
    struct.pack_into("<H", header, 26, 3)          # major version 3 => 512-byte sectors
    struct.pack_into("<H", header, 28, 0xFFFE)     # byte order
    struct.pack_into("<H", header, 30, 9)          # sector shift
    struct.pack_into("<H", header, 32, 6)          # mini sector shift
    struct.pack_into("<I", header, 44, 1)          # num FAT sectors
    struct.pack_into("<I", header, 48, 1)          # first dir sector
    struct.pack_into("<I", header, 56, 4096)       # mini stream cutoff
    struct.pack_into("<I", header, 60, 0xFFFFFFFE)  # first miniFAT sector (none)
    struct.pack_into("<I", header, 64, 0)          # num miniFAT sectors
    struct.pack_into("<I", header, 68, 0xFFFFFFFE)  # first DIFAT
    struct.pack_into("<I", header, 72, 0)          # num DIFAT sectors
    struct.pack_into("<I", header, 76, 0)          # DIFAT[0] = FAT sector 0
    for i in range(1, 109):
        struct.pack_into("<I", header, 76 + i * 4, 0xFFFFFFFF)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(bytes(header) + fat.ljust(sector, b"\0") + directory + data)
    return path


# ============================================================================ PDF writer
def _pdf_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: str, title: str, pages_text: list[list[str]], *, subject: str, author: str,
             created: dt.datetime, producer: str = "Canon iR-ADV C5535 III", numbered=False) -> str:
    """Write a small, valid PDF 1.4 with Helvetica text, 66 chars/line, ~50 lines/page."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    def esc(t):
        return _pdf_escape(t.encode("cp1252", "replace").decode("cp1252"))

    n_pages = len(pages_text)
    font_id = 3 + 2 * n_pages + 1  # ids: 1 catalog, 2 pages, 3.. page/content, then font
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    catalog = add(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages = add(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    page_ids = []
    content_ids = []
    for i in range(n_pages):
        pid = add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                  f"/Resources << /Font << /F1 {font_id} 0 R /F2 {font_id + 1} 0 R >> >> "
                  f"/Contents {3 + 2 * i + 1} 0 R >>".encode())
        lines = pages_text[i]
        parts = [f"BT /F2 13 Tf 72 790 Td ({esc(title)}) Tj ET",
                 f"BT /F1 10 Tf 72 760 Td ({esc(subject)}) Tj ET",
                 "BT /F1 9 Tf 12 TL 72 730 Td"]
        for ln in lines:
            parts.append(f"({esc(ln)}) Tj T*")
        parts.append("ET")
        if numbered:
            parts.append(f"BT /F1 8 Tf 72 40 Td (Page {i + 1} of {n_pages} - scanned {created:%Y-%m-%d}) Tj ET")
        stream = "\n".join(parts).encode("cp1252", "replace")
        cid = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(pid)
        content_ids.append(cid)
    assert len(objects) + 1 == font_id, (len(objects), font_id)
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    info_id = add((f"<< /Title ({esc(title)}) /Author ({esc(author)}) "
                   f"/Creator ({esc(producer)}) /Producer ({esc(producer)}) "
                   f"/CreationDate (D:{created:%Y%m%d%H%M%S}Z) >>").encode("cp1252"))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info_id} 0 R "
            f"/ID [<{hashlib.md5(title.encode()).hexdigest()}> <{hashlib.md5(title.encode()).hexdigest()}>] >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


def wrap(text: str, width: int = 78) -> list[str]:
    words = text.split()
    out, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out


# ============================================================================ teams helpers
class Channel:
    def __init__(self, cid: str, name: str, topic: str, members: list[str], team: str = "Network Operations"):
        self.id = cid
        self.name = name
        self.topic = topic
        self.members = members
        self.team = team
        self.messages: list[dict] = []
        self.rng = c.rng_for("teams", cid)

    def thread(self) -> str:
        return f"19:{c.hexid(self.rng, 24)}@thread.v2"

    def msg(self, t: dt.datetime, person: dict, text: str, *, thread: str | None = None,
            replies: dict | None = None, mentions: list[str] | None = None,
            attach: list[dict] | None = None, edited: bool = False, reaction: str | None = None) -> dict:
        iid = D(t)
        m = {
            "message_id": f"{c.rand_id(self.rng, '', 13, alphabet='0123456789')}",
            "conversation_id": thread or self.thread(),
            "channel_id": self.id,
            "created_at": c.ts_s(iid),
            "created_iid": iid,
            "author_name": person["name"],
            "author_email": person["upn"],
            "role": person["role"],
            "text": text,
            "mentions": [self._resolve_mention(x) for x in (mentions or [])],
            "attachments": attach or [],
            "reactions": ([{"reaction": reaction, "count": 1, "users": [person["name"]]}] if reaction else []),
            "edited": edited,
            "topic": self.topic,
        }
        if replies:
            m["message_type"] = "message"
            m["replies"] = [dict(r, reply_to_message_id=m["message_id"]) for r in replies]
        self.messages.append(m)
        return m

    def _resolve_mention(self, key: str) -> dict:
        p = main_ctx.p(key)
        return {"mention_text": f"@{p['name'].replace(' ', '')}", "name": p["name"], "upn": p["upn"]}

    def to_json(self) -> dict:
        return {"channel_id": self.id, "name": self.name, "team": self.team, "topic": self.topic,
                "members": [main_ctx.p(k) for k in self.members], "messages": self.messages}


main_ctx: Ctx


# ============================================================================ the content
def build(meta: dict) -> None:
    global main_ctx
    ctx = main_ctx = Ctx()
    rng = c.rng_for("ctx")
    net = gn.build_network()
    trn_meta = net["trn_meta"]
    access_ids = [n["node_id"] for n in net["access_nodes"]]
    segments = {s["segment_id"]: s for s in net["physical_segments"]}
    case = meta["case"]

    # ---------------------------------------------------------------- entities
    for d in net["physical_devices"]:
        ctx.entity("device", d["device_id"], name=d["name"], kind=d["kind"], vendor=d["vendor"],
                   model=d["model"], city=d["room"], commissioned=d["commissioned"])
    for s in net["physical_segments"]:
        ctx.entity("segment", s["segment_id"], name=s["name"], type=s["type"], end_a=s["end_a"],
                   end_b=s["end_b"], private_road=bool(s.get("private_road")))
    for code, name, city, lat, lon, role, *_ in gn.SITES:
        ctx.entity("site", code, name=name, city=city, role=role, lat=lat, lon=lon)
    for m in ["TNC-1X", "TNC-2X", "TNC-3X", "7250 IXR-e", "7250 IXR-s", "KMAX-4K", "KMAX-4H", "7750 SR-14s"]:
        ctx.entity("hw_model", m, generation={"TNC-1X": "Gen1", "TNC-2X": "Gen2", "TNC-3X": "Gen3"}.get(m))
    for topic in ["forwarding_state_after_sfp_swap", "sfp_replacement", "bess_arbitrage", "gpu_maintenance_spikes",
                  "meter_swap", "mw_throughput", "upgrade_sfp_400g", "dlq_mem_quota", "mtu_change",
                  "route_discard", "maintenance_freeze", "access_keys"]:
        ctx.entity("topic", f"TOPIC:{topic}", label=topic)

    # ---------------------------------------------------------------- source refs
    ch_transport = Channel("chan-noc-transport", "NOC-Transport", "Transport operations, field escalations",
                           ["erkki", "aino", "veikko", "pekka", "mikko", "emilia"], team="Network Operations")
    ch_field = Channel("chan-field-ops", "Field-Ops-FI", "Field engineers on site, travel and access",
                       ["veikko", "pekka", "heikki", "juhani", "liisa"], team="Field Operations")
    ch_sre = Channel("chan-sre-infra", "SRE-Infra", "Datacentre & platform reliability",
                     ["sanna", "riikka", "olga", "liisa", "mikko"], team="SRE")
    ch_access = Channel("chan-access-north", "Access-North", "Rovaniemi / Kemi / Ivalo access network",
                        ["pekka", "heikki", "juhani", "veikko"], team="Network Operations")
    ch_energy = Channel("chan-energy", "Energy-Procurement", "Electricity contracts, Fingrid, BESS",
                        ["olga", "liisa", "markku", "sanna"], team="Energy")
    ch_services = Channel("chan-services", "Service-Ops", "Service manager escalations & handoffs",
                          ["markku", "emilia", "timo", "sanna"], team="Service Operations")
    channels = [ch_transport, ch_field, ch_sre, ch_access, ch_energy, ch_services]
    for ch in channels:
        ch.thread_anchor = ch.thread()

    emails: list[dict] = []

    def email(key: str, t: dt.datetime, subject: str, frm: str, to: list[str], cc: list[str],
              body: str, *, thread_topic: str | None = None, in_reply_to: str | None = None,
              attachments: list[tuple[str, bytes, str]] | None = None, folder: str = "Inbox") -> dict:
        """Register an email and write its .msg + attachments."""
        r = c.rng_for("email", key)
        mid = msg_id(r)
        f = ctx.p(frm)
        to_list = [(ctx.p(k)["name"], ctx.p(k)["upn"]) for k in to]
        cc_list = [(ctx.p(k)["name"], ctx.p(k)["upn"]) for k in cc]
        body_html = ("<html><body>" + body.replace("\n", "<br>\n") + "</body></html>")
        att = []
        for fname, data, rel_dir in (attachments or []):
            c.write_bytes(["attachments", "current", rel_dir, fname], data)
            c.write_bytes(["email_outlook", "Attachments", rel_dir, fname], data)
            att.append((fname, data))
        safe_subject = "".join(ch for ch in subject if ch not in '\\/:*?"<>|')[:90]
        path = os.path.join("email_outlook", folder,
                            f"{c.local_dt(D(t)).strftime('%Y-%m-%d %H%M')} - {safe_subject}.msg")
        make_msg(os.path.join(c.root_dir(), path), subject=subject, sender_name=f["name"],
                 sender_addr=f["upn"], to=to_list, cc=cc_list, body=body,
                 html=body_html, when=c.local_dt(D(t)).replace(tzinfo=None), message_id=mid,
                 in_reply_to=in_reply_to, thread_topic=thread_topic or subject, attachments=att,
                 headers={"X-Mailer": "Microsoft Outlook 16.0", "Importance": "Normal"})
        e = {"key": key, "msg_id": mid, "path": path.replace("\\", "/"), "subject": subject,
             "from": frm, "to": to, "cc": cc, "iid": D(t), "body": body, "in_reply_to": in_reply_to,
             "attachments": [a[0] for a in (attachments or [])], "folder": folder}
        emails.append(e)
        return e

    # ================================================================ 1. THE CANONICAL CASE
    # Taken verbatim-ish from Elisa's example (teams_chat.json + email.txt), re-dated into our
    # season and wired to our TRN-042 (Kemi) instance. Elisa's own timestamps had no date; we
    # use 2025-10-14 09:14 Europe/Helsinki = 06:14 UTC.
    case_t = _dt(2025, 10, 14, 6, 14)
    thread = ch_transport.thread()
    case_messages = [
        ("veikko", 0, "Hi Erkki, I'm at the site now. Changed the SFP on transmission node TRN-042.\nLink is up and alarms cleared, but traffic is still not coming back. Have you seen this before?", []),
        ("erkki", 2, "Yes. Is this one of the older units?", []),
        ("veikko", 2, "Yes, installed around 2019 I think.", []),
        ("erkki", 4, "Ok. Don't trust the green link status on those.\nSometimes after changing the SFP the interface comes up normally, but the forwarding state doesn't recover correctly.", []),
        ("veikko", 4, "So another SFP?", []),
        ("erkki", 5, "No. Usually the SFP is fine.\nCheck whether the traffic counters are actually increasing. If they stay at zero even though the interface is UP, it's probably the issue I've seen before.", []),
        ("veikko", 7, "Yes. Link UP. RX power looks normal. Counters basically zero.", ["a view of the counters once you have them", ]),
        ("erkki", 8, "That's it.\nDisable the interface, wait a little, then enable it again. Don't reboot the whole unit unless that fails.", []),
        ("veikko", 11, "Did that. Traffic is back 👍", []),
        ("erkki", 11, "Good. These old units sometimes behave like this after an optical module replacement.", []),
        ("veikko", 12, "Is this documented somewhere?", []),
        ("erkki", 13, "Not that I know of 😄\nWe've just learned it over the years.", []),
    ]
    case_msg_objs = []
    for key, mins, text, _ in case_messages:
        attach = None
        if key == "veikko" and mins == 7:
            port_png = _tiny_png(b"TRN-042 1/1/3  in 0.0004 Mbps out 0.0003 Mbps  rx -3.7 dBm  oper UP")
            fname = f"TRN-042_{c.local_dt(D(case_t + dt.timedelta(minutes=mins))).strftime('%Y%m%d-%H%M')}_counters.png"
            attach = [{"name": fname, "content_type": "image/png", "size_bytes": len(port_png),
                       "inline": True, "source": "teams"}, {"name": "TRN-042_1-1-3_port.txt",
                                                               "content_type": "text/plain",
                                                               "size_bytes": 96, "inline": False,
                                                               "source": "teams"}]
            c.write_bytes(["attachments", "current", "teams", fname], port_png)
            c.write_bytes(["attachments", "current", "teams", "TRN-042_1-1-3_port.txt"],
                          b"show interface 1/1/3\noper-status: UP\nin-octets: 0.0004 Mbps\nout-octets: 0.0003 Mbps\n"
                          b"rx-power: -3.7 dBm\nsfp: SFP-10G-8421 (replaced 2025-10-14 08:27)\n")
        m = ch_transport.msg(case_t + dt.timedelta(minutes=mins), ctx.p(key), text, thread=thread, attach=attach)
        case_msg_objs.append((key, mins, m))

    # follow-up by the field engineer two days later, after the fix
    fix_t = _dt(2025, 10, 14, 15, 52)
    m_fix = ch_transport.msg(fix_t, ctx.p("veikko"),
                             "Did the disable/enable on 1/1/3. Traffic is back 👍\nCounters climbing. No reboot needed.",
                             thread=thread, reaction="\U0001F44D")
    # and a 'we should write this down' note the next week
    note_t = _dt(2025, 10, 23, 8, 10)
    m_note = ch_transport.msg(note_t, ctx.p("erkki"),
                              "For the record: TRN-042 and other Gen1 units can come up UP with normal optics after an "
                              "SFP swap but not forward. Bounce the port. I'll put it in the runbook when I get a minute.",
                              thread=thread)

    # email version of the same case (the one Elisa shipped as example email)
    e_case = _dt(2025, 10, 14, 8, 40)
    em_case = email("trn042-case", e_case, "RE: TRN-042 traffic not recovering after SFP replacement",
                    "erkki", ["veikko"], ["aino"],
                    "Hi Veikko,\n\nThat sounds like the behaviour we have seen with some of the older transmission units.\n\n"
                    "The confusing part is that everything can look normal after replacing the optical module:\n"
                    "- interface status = UP\n- optical levels = OK\n- alarms = cleared\n\n...but traffic still doesn't recover.\n\n"
                    "Before replacing the SFP again or rebooting the unit, check the traffic counters. If the interface is UP "
                    "but the counters remain at zero, try disabling and enabling the interface once. This has solved the issue "
                    "in several cases I've seen.\n\nI don't think this is written in the normal field instructions. It is one of "
                    "those things we have learned from experience.\n\nRegards,\nErkki", folder="Sent Items")

    # a field engineer's photo-report email attaching the printed expert note
    note_pdf = make_pdf(os.path.join(c.root_dir(), "documents", "expert_notes", "2019-11_transmission_upgrade_notes.pdf"),
                        "Transmission upgrade notes - personal",
                        [["TRN upgrade / Gen1 (TNC-1X) notes - kept in my drawer, so sue me.", ""] +
                         wrap("After an optical module swap on the old units (Gen1, 2019) the port can show UP with "
                              "normal RX and cleared alarms but forward nothing. Seen 4x: TRN-042, TRN-051(B?deleted), "
                              "Kemi, Ivalo side. Bounce the port. Do NOT swap the module again.") +
                         ["", ] + wrap("Newer boxes (Gen2/3 and the 7250 access switches) do not do this in my experience. "
                                       "Upgraded units: 2023-11 rollout to v6.1.4 made it much rarer.") +
                         ["", ] + wrap("Also: the Kemi transport room is behind the private road gate. The new police-code "
                                       "box has the padlock key. Previously Pekka had the key on his keyring. "
                                       "Ring Markku in service ops first if you do not know the site."),
                         wrap("Transmission upgrade notes - personal (page 2)") + [""] +
                         wrap("Gen1 box 2019-04-18 manufactured. Lab note: manually re-inserting the optical module did not "
                              "clear the forwarding issue in 2/4 cases; a port bounce cleared it in 4/4. Possible "
                              "supplier bug in the forwarding table update path.")],
                        subject="Transmission upgrade notes - personal", author="Erkki Expert",
                        created=_dt(2019, 11, 12, 9, 0), numbered=True)
    em_note = email("note-pdf", _dt(2025, 11, 3, 7, 5), "Fwd: old upgrade notes for the TRN boxes",
                    "veikko", ["erkki"], [],
                    "Erkki,\n\nFound this in the drawer at Kemi when cleaning. Is this still valid for the old boxes? "
                    "I scanned it before it goes to recycling.\n\n// Veikko",
                    attachments=[(os.path.basename(note_pdf), open(note_pdf, "rb").read(), "email_outlook")])

    # ---------------------------------------------------------------- assertions from case
    a_fwd = ctx.assert_(
        subject="TRN-042", topic="forwarding_state_after_sfp_swap",
        statement="On Gen1 (2019) transmission units, after an SFP replacement the interface can be UP with normal "
                  "optical power and zero traffic counters; the fix is to disable and re-enable the interface, not "
                  "to replace the SFP again or reboot the node.",
        valid_from=D(case_t), valid_to=None,
        source={"type": "teams_message", "ref": case_msg_objs[7][2]["message_id"],
                "detail": "NOC-Transport thread, message 8/12", "by": "erkki", "recorded_iid": D(case_t)},
        confidence="high", status="believed_true",
        qualifiers={"applies_to_models": ["TNC-1X"], "applies_to_generation": "Gen1",
                    "symptom": ["oper_up", "rx_power_normal", "counters_zero"], "action": "bounce_interface",
                    "avoid": ["replace_sfp", "reboot_node"]})
    a_email = ctx.assert_(
        subject="TRN-042", topic="forwarding_state_after_sfp_swap",
        statement="If the interface is UP but traffic counters remain at zero after an SFP replacement, disable and "
                  "enable the interface once before replacing the SFP or rebooting.",
        valid_from=D(e_case), valid_to=None,
        source={"type": "email", "ref": em_case["msg_id"], "detail": "RE: TRN-042 ...", "by": "erkki",
                "recorded_iid": D(e_case)},
        confidence="high", status="believed_true", contradicts=[a_fwd])
    a_note = ctx.assert_(
        subject="TNC-1X", topic="forwarding_state_after_sfp_swap",
        statement="Gen1 units: port bounce cleared the forwarding issue in 4/4 observed cases; re-inserting the module "
                  "did not (2/4). Supplier bug suspected in the forwarding-table update path.",
        valid_from=D(_dt(2019, 11, 12)), valid_to=None,
        source={"type": "pdf", "ref": "documents/expert_notes/2019-11_transmission_upgrade_notes.pdf",
                "detail": "scanned personal notes by Erkki Expert", "by": "erkki",
                "recorded_iid": D(_dt(2019, 11, 12))},
        confidence="medium", status="believed_true",
        qualifiers={"evidence": "4 of 4 observed cases"})
    # forwarded PDF: knowledge moved to a second place, same truth
    ctx.assert_(
        subject="TNC-1X", topic="forwarding_state_after_sfp_swap",
        statement="Scanned copy of the 2019 Gen1 forwarding notes forwarded to the transport expert list.",
        valid_from=D(_dt(2025, 11, 3, 7, 5)), valid_to=None,
        source={"type": "email", "ref": em_note["msg_id"], "detail": "Fwd: old upgrade notes", "by": "veikko",
                "recorded_iid": D(_dt(2025, 11, 3, 7, 5))},
        confidence="medium", status="believed_true", contradicts=[])

    # ================================================================ 2. ADVICE THAT BECOMES OUTDATED
    # 2a. The 'applies to old units' scope is superseded once TNC-2X units show the same symptom.
    t2 = _dt(2026, 3, 10, 7, 30)
    th_t2 = ch_transport.thread()
    m21 = ch_transport.msg(t2, ctx.p("aino"),
                           "Anyone seen 1/1/3 UP + zero counters on a TNC-2X after SFP swap? Pulkkila node (TRN-009). "
                           "Gen2, not one of the old ones.")
    m22 = ch_transport.msg(t2 + dt.timedelta(minutes=6), ctx.p("erkki"),
                           "Hm. So it's not only Gen1.\nSame bounce worked?")
    m23 = ch_transport.msg(t2 + dt.timedelta(minutes=9), ctx.p("aino"),
                           "Yes, disable/enable and it came back. Ok so the fix is fine but 'only old units' is wrong.")
    a_scope_old = ctx.assert_(
        subject="TNC-1X", topic="forwarding_state_after_sfp_swap",
        statement="Forwarding-state stall after SFP swap applies only to Gen1 (2019 TNC-1X) units.",
        valid_from=D(case_t), valid_to=D(t2),
        source={"type": "teams_message", "ref": m21["message_id"], "detail": "NOC-Transport 2026-03-10",
                "by": "erkki", "recorded_iid": D(t2)},
        confidence="medium", status="superseded", qualifiers={"applies_to_generation": "Gen1"})
    a_scope_new = ctx.assert_(
        subject="TNC-2X", topic="forwarding_state_after_sfp_swap",
        statement="The forwarding-state stall after SFP swap also occurs on Gen2 (TNC-2X) units. Fix (port bounce) "
                  "still applies; the 'old units only' scope is stale.",
        valid_from=D(t2), valid_to=None,
        source={"type": "teams_message", "ref": m23["message_id"], "detail": "NOC-Transport 2026-03-10",
                "by": "aino", "recorded_iid": D(t2)},
        confidence="high", status="believed_true", supersedes=a_scope_old,
        qualifiers={"applies_to_generation": ["Gen1", "Gen2"]})

    # 2b. BESS instruction from 2025 that energy market changed (superseded by market update)
    t_old = _dt(2025, 11, 4, 12, 0)
    th_e = ch_energy.thread()
    m_e1 = ch_energy.msg(t_old, ctx.p("olga"),
                         "Reminder: keep the DC1/DC2 BESS at 80% SOC for the winter because of the Fingrid capacity "
                         "market. Charge cheap hours, do not discharge above 60% depth.")
    a_bess_old = ctx.assert_(
        subject="FI-TRE-DC2", topic="bess_arbitrage",
        statement="Keep BESS at 80% SOC through winter for Fingrid capacity market; max 60% depth of discharge.",
        valid_from=D(t_old), valid_to=D(_dt(2026, 4, 1)),
        source={"type": "teams_message", "ref": m_e1["message_id"], "detail": "Energy-Procurement",
                "by": "olga", "recorded_iid": D(t_old)},
        confidence="high", status="superseded", qualifiers={"soC_target_pct": 80})
    t_new = _dt(2026, 4, 1, 9, 0)
    em_mkt = email("bess-mkt", t_new, "BESS operating mode change from 1 April (aFRR participation)",
                   "olga", ["liisa", "sanna", "markku"], ["riikka"],
                   "Hi all,\n\nFingrid's aFRR products now pay better than capacity alone, so from 1 April the DC2 BESS "
                   "switches from the winter 'hold 80% SOC' mode to aFRR cycling. SOC band 35-70%, depth of "
                   "discharge up to 35% per event, max 6 events/day.\n\nPlease update any dashboards that alert on "
                   "SOC < 50%. Thanks,\nOlga", thread_topic="BESS operating mode")
    a_bess_new = ctx.assert_(
        subject="FI-TRE-DC2", topic="bess_arbitrage",
        statement="From 2026-04 BESS runs aFRR cycling: SOC band 35-70%, DoD <=35% per event, <=6 events/day.",
        valid_from=D(t_new), valid_to=None,
        source={"type": "email", "ref": em_mkt["msg_id"], "detail": "BESS operating mode change from 1 April",
                "by": "olga", "recorded_iid": D(t_new)},
        confidence="high", status="believed_true", supersedes=a_bess_old,
        qualifiers={"soc_min_pct": 35, "soc_max_pct": 70, "dod_max_pct": 35})

    # 2c. Energy-efficiency rule from 2025 GTC email that got silently re-ised (Q3 2026)
    t_gtc = _dt(2025, 12, 2, 10, 0)
    em_gtc = email("gtc", t_gtc, "Updated Terms of Service - energy attribution clause", "markku",
                   ["olga", "liisa", "sanna"], ["timo"],
                   "Hello,\n\nOur customer GTCs have been updated so that the DC PUE reported to customers includes "
                   "fixed overheads (cooling, lighting, losses). Until now we attributed them to the site. Please "
                   "report PUE = total facility power / IT power for customer invoices from January.\n\nMarkku",
                   folder="Sent Items")
    a_gtc = ctx.assert_(
        subject="FI-HEL-DC1", topic="bess_arbitrage",
        statement="Customer PUE reported = total facility power / IT power, including fixed overheads.",
        valid_from=D(t_gtc), valid_to=D(_dt(2026, 8, 15)),
        source={"type": "email", "ref": em_gtc["msg_id"], "detail": "GTC update", "by": "markku",
                "recorded_iid": D(t_gtc)},
        confidence="medium", status="superseded", qualifiers={"reporting": "customer"})

    # 2d. Platform advice about DLQ that expired when the platform version changed
    t_dlq = _dt(2025, 11, 8, 13, 0)
    th_s = ch_sre.thread()
    m_s1 = ch_sre.msg(t_dlq, ctx.p("sanna"),
                      "Kafka DLQ queue is filling on `volga.train.events` again. Bump the consumer memory to 4Gi and "
                      "it drains. We are on 3.4 so that is expected.")
    m_s2 = ch_sre.msg(_dt(2026, 5, 12, 10, 0), ctx.p("riikka"),
                      "PSA: platform is on 4.0 now. The 4Gi bump advice is obsolete - DLQ drains on its own within "
                      "10 min, if it does not, it's a real poison message. Do not raise memory.")
    a_dlq_old = ctx.assert_(
        subject="TOPIC:dlq_mem_quota", topic="dlq_mem_quota",
        statement="Kafka DLQ on volga.train.events drains if consumer memory is raised to 4Gi (platform 3.4).",
        valid_from=D(t_dlq), valid_to=D(_dt(2026, 5, 12)),
        source={"type": "teams_message", "ref": m_s1["message_id"], "detail": "SRE-Infra",
                "by": "sanna", "recorded_iid": D(t_dlq)},
        confidence="high", status="superseded", qualifiers={"platform_version": "3.4"})
    a_dlq_new = ctx.assert_(
        subject="TOPIC:dlq_mem_quota", topic="dlq_mem_quota",
        statement="On platform 4.0+, DLQ drains within 10 min without memory changes; persistent DLQ = poison "
                  "message, do not raise memory.",
        valid_from=D(_dt(2026, 5, 12)), valid_to=None,
        source={"type": "teams_message", "ref": m_s2["message_id"], "detail": "SRE-Infra",
                "by": "riikka", "recorded_iid": D(_dt(2026, 5, 12))},
        confidence="high", status="believed_true", supersedes=a_dlq_old,
        qualifiers={"platform_version": "4.0"})

    # ================================================================ 3. CONFLICTING ADVICE
    # 3a. Two experts disagree on what to do with ALARM_DOWN on a microwave hop
    t_conf = _dt(2026, 6, 2, 11, 0)
    th_c = ch_access.thread()
    m_c1 = ch_access.msg(t_conf, ctx.p("heikki"),
                         "RF alarm is up on the Kajaani hop. I usually just power-cycle the radio (the KMAX-4K). "
                         "Works 9/10.")
    m_c2 = ch_access.msg(t_conf + dt.timedelta(minutes=12), ctx.p("juhani"),
                         "Please do NOT power-cycle KMAX-4K. The radio reboots into a config-recovery state and "
                         "loses the provisioned channel plan unless the config is exported first. Escalate to the "
                         "access engineer or ask for approval.")
    m_c3 = ch_access.msg(t_conf + dt.timedelta(minutes=20), ctx.p("emilia"),
                         "Agreed with Juhani; and anyway the operationally relevant part is the field engineer "
                         "needs to check the dish alignment before any radio is reset. Otherwise we only lose more.")
    a_conf_1 = ctx.assert_(
        subject="KMAX-4K", topic="mw_throughput", statement="For an RF alarm on a KMAX-4K microwave hop, power-cycle "
        "the radio (works 9/10).",
        valid_from=D(t_conf), valid_to=None,
        source={"type": "teams_message", "ref": m_c1["message_id"], "detail": "Access-North",
                "by": "heikki", "recorded_iid": D(t_conf)},
        confidence="low", status="disputed", qualifiers={"step": "power_cycle_radio"})
    a_conf_2 = ctx.assert_(
        subject="KMAX-4K", topic="mw_throughput",
        statement="Do not power-cycle a KMAX-4K: it reboots into config-recovery and loses the channel plan unless "
                  "the config was exported first. Check dish alignment, escalate, or get approval.",
        valid_from=D(t_conf) + 3, valid_to=None,
        source={"type": "teams_message", "ref": m_c2["message_id"], "detail": "Access-North",
                "by": "juhani", "recorded_iid": D(t_conf) + 3},
        confidence="high", status="disputed", qualifiers={"step": "escalate"},
        contradicts=[a_conf_1])
    ctx.contradictions.append({
        "id": "CONFLICT-001", "assertions": [a_conf_1, a_conf_2], "topic": "mw_throughput",
        "summary": "Conflicting instructions for KMAX-4K RF alarm: power-cycle (Heikki) vs. never power-cycle "
                   "(Juhani). Both unresolved as of the data snapshot.",
        "resolution_hint": "Recency and role seniority both favour Juhani (Network Expert / architect endorsement "
                           "by Emilia). Treat the power-cycle as unsafe and escalate.",
        "severity": "high", "kind": "disagreement",
    })

    # 3b. Conflict about MTU, resolved by recency
    t_m1 = _dt(2025, 12, 15, 9, 0)
    t_m2 = _dt(2026, 4, 20, 14, 0)
    m_m1 = ch_transport.msg(t_m1, ctx.p("mikko"), "Core links are 9114 MTU, don't change it, the old TNC handles "
                                                  "9114 only.")
    m_m2 = ch_transport.msg(t_m2, ctx.p("erkki"), "Update on MTU: after the 2026 firmware the core links are 9216 "
                                                  "and the TNC-1X now handles it. Old 9114 advice is obsolete. "
                                                  "Verified on TRN-042 during the SFP case.")
    a_mtu_old = ctx.assert_(
        subject="CR-001", topic="mtu_change", statement="Core links are MTU 9114; do not change it, old TNC only "
        "handles 9114.",
        valid_from=D(t_m1), valid_to=D(t_m2),
        source={"type": "teams_message", "ref": m_m1["message_id"], "detail": "NOC-Transport",
                "by": "mikko", "recorded_iid": D(t_m1)},
        confidence="medium", status="superseded", qualifiers={"mtu": 9114})
    a_mtu_new = ctx.assert_(
        subject="CR-001", topic="mtu_change", statement="Core links are MTU 9216 since the 2026 firmware; TNC-1X "
        "handles it. Earlier 9114 guidance is obsolete.",
        valid_from=D(t_m2), valid_to=None,
        source={"type": "teams_message", "ref": m_m2["message_id"], "detail": "NOC-Transport",
                "by": "erkki", "recorded_iid": D(t_m2)},
        confidence="high", status="believed_true", supersedes=a_mtu_old, qualifiers={"mtu": 9216})

    # 3c. Access-key conflict: caller vs key holder replaced after the road barrier repair
    #   handled later with the access emails

    # ================================================================ 4. MORE SFP / MODULE STORIES (noise + structure)
    extra_sfp = [
        # node, model, date, who, resolution, text
        ("TRN-009", "TNC-2X", _dt(2026, 3, 10, 7, 10), "aino", "bounce", None),
        ("TRN-014", "TNC-1X", _dt(2025, 12, 3, 6, 40), "veikko", "bounce", None),
        ("TRN-006", "TNC-3X", _dt(2026, 1, 22, 11, 20), "pekka", "reseat", None),
        ("TRN-019", "TNC-2X", _dt(2025, 11, 19, 8, 5), "veikko", "bounce", None),
        ("TRN-003", "TNC-3X", _dt(2026, 7, 14, 9, 30), "aino", "replace_again", None),
        ("TRN-042", "TNC-1X", _dt(2026, 9, 2, 7, 45), "veikko", "bounce", None),
    ]
    for nid, model, when, who, resolution, _ in extra_sfp:
        t = nid.replace("TRN-", "")
        if resolution == "bounce":
            text = (f"Same thing again on {nid} ({model}) after SFP swap on 1/1/3. UP, optics fine, counters zero. "
                    f"Bounced the port, traffic back. Copying the known issue.")
            st = "Interface UP + optics normal + counters zero after SFP swap on " + nid + "; port bounce fixed it."
            topic = "forwarding_state_after_sfp_swap"
        elif resolution == "reseat":
            text = (f"{nid} ({model}) SFP was actually loose, not the known issue. Pulled and re-seated the module, "
                    f"traffic came back without a port bounce. Worth checking cabling first.")
            st = "SFP re-seat fixed the counters-zero symptom on " + nid + " (loose module), no bounce needed."
            topic = "sfp_replacement"
        else:
            text = (f"{nid} ({model}) after SFP swap: UP, optics fine, zero counters. Bounce did NOT help this time, "
                    f"we replaced the SFP again and it came back. So the bounce recipe is not guaranteed.")
            st = "On " + nid + " (TNC-3X) the 'bounce the port' recipe failed; replacing the SFP fixed it."
            topic = "forwarding_state_after_sfp_swap"
        m = ch_transport.msg(when, ctx.p(who), text)
        ctx.assert_(
            subject=nid, topic=topic, statement=st, valid_from=D(when), valid_to=None,
            source={"type": "teams_message", "ref": m["message_id"], "detail": f"NOC-Transport {nid}",
                    "by": who, "recorded_iid": D(when)},
            confidence="medium", status="believed_true" if resolution != "replace_again" else "disputed",
            qualifiers={"observed_model": model, "resolution": resolution},
            contradicts=[a_fwd] if resolution == "replace_again" else [])
        if resolution == "replace_again":
            ctx.contradictions.append({
                "id": "CONFLICT-002",
                "assertions": [a_fwd, ctx.assertions[-1]],
                "topic": "forwarding_state_after_sfp_swap",
                "summary": "The bounce recipe failed on a TNC-3X node; SFP replacement was needed. Either the "
                           "known issue does not always apply, or the root cause was a faulty SFP. Unresolved.",
                "resolution_hint": "Model-scoped exception (TNC-3X) vs. a genuinely faulty SFP; needs a follow-up "
                                   "test the data does not contain.",
                "severity": "medium", "kind": "exception_to_rule",
            })

    # ================================================================ 5. TELEMETRY / ENERGY INSIGHTS
    # 5a. Weird spike on the Tampere meter during the first month: metering noise
    t_spike = _dt(2025, 11, 18, 10, 0)
    th_s2 = ch_sre.thread()
    m_sa = ch_sre.msg(t_spike, ctx.p("sanna"),
                      "Customers pinged: the DC2 meter spikes to >2x IT power about once every other week for a few "
                      "hours. Looks like the meter reading or PDU-A IT measurement glitches, does not match real "
                      "load, customer PUE dashboards complain.")
    m_sb = ch_sre.msg(t_spike + dt.timedelta(minutes=35), ctx.p("olga"),
                      "Confirmed it's the PDUs, we're short on readings between the metering point and the PDU "
                      "aggregation. If grid != IT*PUE, flag it as suspect; do not feed it to customer reporting.")
    a_spike = ctx.assert_(
        subject="FI-TRE-DC2", topic="gpu_maintenance_spikes",
        statement="DC2 grid meter occasionally reads >2x IT power for a few hours; this is a metering artefact, not "
                  "a load spike. Do not feed suspect rows to customer PUE reporting.",
        valid_from=D(_dt(2025, 10, 1)), valid_to=None,
        source={"type": "teams_message", "ref": m_sb["message_id"], "detail": "SRE-Infra",
                "by": "olga", "recorded_iid": D(t_spike)},
        confidence="high", status="believed_true", qualifiers={"metric": "grid_power_mw", "quality": "suspect"})
    ctx.insights.append({
        "id": "INSIGHT-001", "title": "DC2 meter spikes are metering artefacts",
        "needs_layers": ["telemetry", "context"],
        "derivation": "Rows where grid_power_mw / it_power_mw > 1.6 must be excluded from customer reporting; "
                      "the context assertion gives the reason and the action.",
        "assertion": a_spike,
    })

    # 5b. Maintenance windows explain 5% utilisation dips in the primary site (needs events layer)
    t_main = _dt(2026, 11, 3, 15, 0)
    m_main1 = ch_sre.msg(t_main, ctx.p("sanna"),
                         "We'll do the GPU firmware push on DC1 next Wednesday; expect utilisation to drop 30 min per "
                         "batch. This messes with our capacity alerts, ignore them.")
    m_main2 = ch_sre.msg(t_main + dt.timedelta(minutes=8), ctx.p("riikka"), "Noted, no customer impact.")
    ctx.insights.append({
        "id": "INSIGHT-002", "title": "Utilisation dips on maintained sites are planned",
        "needs_layers": ["telemetry", "events", "context"],
        "derivation": "Sudden GPU utilisation drops on a site with an open maintenance window (device_events) are "
                      "expected; suppress capacity alerts.",
        "assertion": None,
    })

    # ================================================================ 6. ACCESS KNOWLEDGE (the locked barrier)
    t_barrier = _dt(2025, 10, 20, 9, 5)
    # The Kemi transport segment has a private-road gate, per network data
    kemi_seg = [s for s in net["physical_segments"] if s["end_a"] == "TRN-042"]
    kemi_seg_id = kemi_seg[0]["segment_id"] if kemi_seg else "SEG-042"
    th_a = ch_field.thread()
    m_bar1 = ch_field.msg(t_barrier, ctx.p("veikko"),
                          "Locked barrier at the start of the private road to the Kemi transport room. Nobody told "
                          "me. Pekka, where is the key?")
    m_bar2 = ch_field.msg(t_barrier + dt.timedelta(minutes=14), ctx.p("pekka"),
                          "The padlock key is on my keyring, I'll bring it. It's NOT in the key box, that is for "
                          "another gate.", reaction="\U0001F605")
    m_bar3 = ch_field.msg(t_barrier + dt.timedelta(minutes=25), ctx.p("heikki"),
                          "Note the barrier was repaired summer 2026, and the lock changed to a code lock. "
                          "The new code is with the security lead, not the old key system.")
    a_bar_old = ctx.assert_(
        subject=kemi_seg_id, topic="access_keys",
        statement="Kemi transport room gate is locked with a padlock; key is on Pekka Hämäläinen's keyring "
                  "(not in the key box at the office).",
        valid_from=D(t_barrier), valid_to=D(_dt(2026, 7, 1)),
        source={"type": "teams_message", "ref": m_bar2["message_id"], "detail": "Field-Ops-FI",
                "by": "pekka", "recorded_iid": D(t_barrier + dt.timedelta(minutes=14))},
        confidence="high", status="superseded", about_entity_kind="segment",
        qualifiers={"lock_type": "padlock"})
    ctx.access_rows.append({"segment_id": kemi_seg_id, "site": "Kemi", "barrier": "gate",
                            "valid_from": c.ts_s(D(t_barrier)),
                            "valid_to": c.ts_s(D(_dt(2026, 7, 1))), "lock": "padlock",
                            "holder": "Pekka Hämäläinen", "where": "keyring",
                            "source": m_bar2["message_id"]})

    # an email (the only place the new code lives) - the classic locked-barrier failure
    t_code = _dt(2026, 8, 18, 10, 20)
    em_code = email("kemi-new-lock", t_code, "Kemi site: new code lock installed (action for field ops)",
                    "timo", ["heikki", "pekka", "veikko"], ["markku", "liisa"],
                    "Hello,\n\nThe Kemi transport room gate lock was changed to a code lock during the summer "
                    "barrier repair. The new code is handed out by site security. Please update the access "
                    "register; I have not been able to access the register so this email is the source.\n\n"
                    "Timo", folder="Sent Items")
    # code is written obliquely (not a plain secret in the chat tool) but is in this email
    em_code2 = email("kemi-code", t_code + dt.timedelta(days=2), "RE: Kemi site: new code lock installed",
                     "heikki", ["veikko"], ["pekka"],
                     "Hi,\n\nYes I have the code: it is the same 4-digit as the Kemi door code but with the last "
                     "digit +1. I will also email it to the field team distribution when they are back from leave. "
                     "There is no gate key any more.\n\nHeikki", folder="Inbox")
    a_bar_new = ctx.assert_(
        subject=kemi_seg_id, topic="access_keys",
        statement="Kemi transport room gate is a code lock (installed 2026-08). There is no physical key. The "
                  "code is held by site security / Heikki Lehtonen; the 'Pekka has the key' advice is obsolete.",
        valid_from=D(t_code), valid_to=None,
        source={"type": "email", "ref": em_code["msg_id"], "detail": "Kemi site: new code lock installed",
                "by": "timo", "recorded_iid": D(t_code)},
        confidence="high", status="believed_true", supersedes=a_bar_old, about_entity_kind="segment",
        qualifiers={"lock_type": "code_lock", "code_custodian": "Heikki Lehtonen"})
    ctx.access_rows.append({"segment_id": kemi_seg_id, "site": "Kemi", "barrier": "gate",
                            "valid_from": c.ts_s(D(t_code)), "valid_to": None, "lock": "code_lock",
                            "holder": "Heikki Lehtonen", "where": "email/site security",
                            "source": em_code["msg_id"]})
    ctx.contradictions.append({
        "id": "CONFLICT-003", "assertions": [a_bar_old, a_bar_new], "topic": "access_keys",
        "summary": "Two access instructions disagree: the padlock key is (per Teams) on Pekka's keyring, but the "
                   "gate was replaced by a code lock (per email).",
        "resolution_hint": "The later email wins by recency; the old Teams knowledge is a trap for a field agent "
                           "dispatched to Kemi after 2026-08.",
        "severity": "high", "kind": "outdated_vs_new",
    })

    # a second, ordinary private-road site: Ivalo (no Teams knowledge, only a PDF drawing + email)
    ivalo_seg = [s for s in net["physical_segments"] if s["end_a"] == "TRN-012"]
    if ivalo_seg:
        ivalo_seg_id = ivalo_seg[0]["segment_id"]
        draw_pdf = make_pdf(os.path.join(c.root_dir(), "documents", "expert_notes", "2024-06_ivalo_site_access.pdf"),
                            "Ivalo site access drawing",
                            [["Ivalo transport site (TRN-012) - access plan"] + [""] +
                             wrap("Approach from road 4 by the snowmobile trail sign; 1.4 km private gravel road. "
                                  "The gate at the start of the gravel road is OPEN in summer. In winter the road is "
                                  "snowed in and you must call the local contractor (Rovaniemi field team) to "
                                  "clear it. No barrier code for this gate; it is not locked.")],
                            subject="Ivalo site access drawing", author="Pekka Hämäläinen",
                            created=_dt(2024, 6, 12, 8, 0))
        em_ivalo = email("ivalo-access", _dt(2025, 10, 28, 8, 10), "Ivalo site access",
                         "pekka", ["heikki"], [],
                         "Heikki, for the Ivalo node: the gravel road gate is unlocked but it is snowed in between "
                         "Oct and May. Call the contractor before going, they need a snow-plow. The drawing is "
                         "attached.",
                         attachments=[(os.path.basename(draw_pdf), open(draw_pdf, "rb").read(), "email_outlook")])
        a_ivalo = ctx.assert_(
            subject=ivalo_seg_id, topic="access_keys",
            statement="Ivalo gate is unlocked; the 1.4 km gravel road is snowed in Oct-May and must be cleared by "
                      "the local contractor (call the Rovaniemi field team).",
            valid_from=D(_dt(2024, 6, 12)), valid_to=None,
            source={"type": "email", "ref": em_ivalo["msg_id"], "detail": "Ivalo site access",
                    "by": "pekka", "recorded_iid": D(_dt(2025, 10, 28))},
            confidence="high", status="believed_true", about_entity_kind="segment")
        ctx.access_rows.append({"segment_id": ivalo_seg_id, "site": "Ivalo", "barrier": "none",
                                "valid_from": c.ts_s(D(_dt(2024, 6, 12))), "valid_to": None, "lock": "none",
                                "holder": None, "where": "open", "source": em_ivalo["msg_id"]})

    # other private-road segments: put a line in the access register (no explicit knowledge -> a trap)
    for seg in net["physical_segments"]:
        if seg.get("private_road") and seg["segment_id"] not in {r["segment_id"] for r in ctx.access_rows}:
            ctx.access_rows.append({"segment_id": seg["segment_id"], "site": seg["name"].split()[0],
                                    "barrier": seg.get("barrier") or "gate", "valid_from": None,
                                    "valid_to": None, "lock": "unknown", "holder": None, "where": None,
                                    "source": None, "derived": "as_built flag private_road=true"})

    # ================================================================ 7. SERVICE-HANDOFF KNOWLEDGE
    t_svc = _dt(2026, 2, 17, 12, 30)
    th_sv = ch_services.thread()
    m_sv1 = ch_services.msg(t_svc, ctx.p("markku"),
                            "Reminder for the big-window jobs: if a service is about to end and job is not done, the "
                            "job must be requeued to the next window, not killed. Sales-specific; if killed the "
                            "customer is refunded.")
    m_sv2 = ch_services.msg(t_svc + dt.timedelta(minutes=3), ctx.p("sanna"), "Ok, noted, our alerting should not "
                                                                           "auto-close those.")
    a_svc = ctx.assert_(
        subject="TOPIC:maintenance_freeze", topic="maintenance_freeze",
        statement="During the sales freeze (Dec 20 - Jan 5) no network changes are allowed without a named change "
                  "approval; change-approval requests go to the service manager.",
        valid_from=D(_dt(2025, 12, 20)), valid_to=D(_dt(2026, 1, 5)),
        source={"type": "teams_message", "ref": m_sv1["message_id"], "detail": "Service-Ops",
                "by": "markku", "recorded_iid": D(t_svc)},
        confidence="medium", status="believed_true")

    # ================================================================ 8. HIGH-VOLUME CHATTER
    chatter_rng = c.rng_for("chatter")
    chatter = [
        ("Morning standup done - nothing blocked.", "ch_transport"),
        ("The Vlantis fibre delivery is 2 weeks late, escalate with supplier.", "ch_transport"),
        ("Coffee machine in the 3rd floor is fixed.", "ch_sre"),
        ("Can someone approve my timesheet?", "ch_field"),
        ("New lunch menu at the canteen is decent.", "ch_sre"),
        ("Reminder: laptop swap drop-in on Thursday.", "ch_services"),
        ("Is anyone else seeing high CPU on the APM cluster?", "ch_sre"),
        ("Rack 42 in DC1 has a loud fan, facilities ticket opened.", "ch_sre"),
        ("Road 4 closed at Rovaniemi due to accident, alternate route via 82.", "ch_field"),
        ("Poetry night at the office on Friday, sign-up sheet is open.", "ch_services"),
        ("Access switch firmware v10.0.3 approved for the next change window.", "ch_access"),
        ("Cancelled the demo between 14 and 16, no impact for us.", "ch_services"),
    ]
    by_channel = {"ch_transport": ch_transport, "ch_field": ch_field, "ch_sre": ch_sre,
                  "ch_access": ch_access, "ch_services": ch_services}
    people_keys = list(ctx._nick)
    for i in range(760):
        day = chatter_rng.randrange(0, c.n_days())
        t = c.START + dt.timedelta(days=day, hours=chatter_rng.randint(5, 19),
                                   minutes=chatter_rng.randint(0, 59))
        if chatter_rng.random() < 0.55:
            txt, chname = chatter_rng.choice(chatter)
        else:
            txt, chname = f"generic ops chatter #{i}: {chatter_rng.choice(['all good', 'FYI', 'no action', 'checked', 'closing'])}", chatter_rng.choice(list(by_channel))
        ch = by_channel[chname]
        who = chatter_rng.choice(people_keys)
        ch.msg(t, ctx.p(who), txt)

    # ================================================================ 9. EMAIL NOISE + structured mail
    email_rng = c.rng_for("emailnoise")
    noise_subjects = [
        ("Monthly invoice report", "hello,\n\nthe attached report is for review.\n\nThanks"),
        ("Re: travel booking", "Booked the 8:15 to Oulu, hotel confirmed.\n\nBR"),
        ("Windows security patch reminder", "Please install the patches by Friday.\n\nIT"),
        ("Re: meeting notes", "See the attached notes from Monday."),
        ("Parking permits", "New parking permits are available at the front desk."),
        ("Supplier contract renewal", "The frame agreement with the fibre supplier is up for renewal."),
        ("Re: SFP stock levels", "We have 40x SFP-10G left, ordering more."),
        ("Xmas party", "The Christmas party is on 12 December."),
    ]
    for i in range(140):
        subj, body = email_rng.choice(noise_subjects)
        day = email_rng.randrange(0, c.n_days())
        when = c.START + dt.timedelta(days=day, hours=email_rng.randint(6, 18), minutes=email_rng.randint(0, 59))
        frm, to = email_rng.sample(people_keys, 2)
        email(f"noise{i}", when, f"{subj} ({i})", frm, [to], [], body,
              folder="Inbox" if email_rng.random() < 0.6 else "Sent Items")

    # a structured "meter swap" email that explains a maintenance spike in telemetry
    t_meter = _dt(2026, 8, 20, 8, 0)
    em_meter = email("meter-swap", t_meter, "DC1 power meter swap 2026-09-02 (short spikes expected)",
                     "liisa", ["sanna", "olga"], ["markku"],
                     "Hi,\n\nThe main grid meter for DC1 will be swapped 2026-09-02. Expect a few short reading "
                     "spikes during the swap; they are metering artefacts and are good to ignore in reporting.\n\n"
                     "Liisa")
    a_meter = ctx.assert_(
        subject="FI-HEL-DC1", topic="meter_swap",
        statement="DC1 grid meter swap on 2026-09-02; short power spikes that day are metering artefacts.",
        valid_from=D(_dt(2026, 9, 1)), valid_to=D(_dt(2026, 9, 4)),
        source={"type": "email", "ref": em_meter["msg_id"], "detail": "meter swap notice", "by": "liisa",
                "recorded_iid": D(t_meter)},
        confidence="high", status="believed_true", qualifiers={"metric": "grid_power_mw"})
    ctx.insights.append({
        "id": "INSIGHT-003", "title": "DC1 September spikes are planned meter-swap artefacts",
        "needs_layers": ["telemetry", "context"],
        "derivation": "Telemetry spikes on FI-HEL-DC1 around 2026-09-02 match the meter-swap notice window; "
                      "exclude them instead of alerting.",
        "assertion": a_meter,
    })

    # ================================================================ 10. Confluence pages
    kb = {
        "kb-001_runbook_transmission_sfp.html": dict(
            title="Runbook: transmission SFP replacement", last_edited=_dt(2023, 5, 4),
            body="<h1>Runbook: transmission SFP replacement</h1><p>Steps: 1) order replacement 2) lock port "
                 "3) replace 4) verify optics. If traffic does not return, replace the SFP again.</p>"
                 "<p class='note'>Last reviewed 2023: only covers Gen1 up to TNC-2X.</p>")
        ,
        "kb-002_mtu_policy.html": dict(
            title="Core MTU policy", last_edited=_dt(2025, 12, 15),
            body="<h1>Core MTU policy</h1><p>All core and transport links use MTU 9114. Do not change the MTU "
                 "without an approved change and a lab test.</p>"),
        "kb-003_access_register_table.html": dict(
            title="Site access register (partial)", last_edited=_dt(2025, 6, 1),
            body="<h1>Site access register</h1><table><tr><th>Site</th><th>Barrier</th><th>Holder</th></tr>"
                 "<tr><td>Ivalo</td><td>gate</td><td>contractor (snow)</td></tr>"
                 "<tr><td>Rovaniemi</td><td>boom</td><td>unknown</td></tr></table><p>Kemi: not in register.</p>"),
        "kb-004_bess_operating_modes.html": dict(
            title="BESS operating modes", last_edited=_dt(2025, 11, 10),
            body="<h1>BESS operating modes</h1><p>Winter mode: hold 80% SOC for the Fingrid capacity market.</p>"),
    }
    os.makedirs(os.path.join(c.root_dir(), "documents", "confluence"), exist_ok=True)
    for fname, page in kb.items():
        html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{page['title']}</title></head><body>"
                f"{page['body']}<hr><p class='meta'>Last edited by Confluence user - {page['last_edited']:%Y-%m-%d} - "
                f"page id {c.hexid(c.rng_for('kb', fname), 8)}</p></body></html>")
        c.write_text(["documents", "confluence", fname], html)
        # every KB page gets an assertion? Only the ones that matter; KB is stale here, so
        # mark the MTU one superseded and the BESS one as outdated.
    a_kb_mtu = ctx.assert_(
        subject="CR-001", topic="mtu_change", statement="KB: all core and transport links use MTU 9114.",
        valid_from=D(_dt(2025, 12, 15)), valid_to=D(t_m2),
        source={"type": "confluence", "ref": "documents/confluence/kb-002_mtu_policy.html",
                "detail": "Core MTU policy", "by": "Confluence", "recorded_iid": D(_dt(2025, 12, 15))},
        confidence="low", status="superseded", qualifiers={"mtu": 9114})
    a_kb_bess = ctx.assert_(
        subject="FI-TRE-DC2", topic="bess_arbitrage",
        statement="KB: Winter mode holds BESS at 80% SOC for the Fingrid capacity market.",
        valid_from=D(_dt(2025, 11, 10)), valid_to=D(t_new),
        source={"type": "confluence", "ref": "documents/confluence/kb-004_bess_operating_modes.html",
                "detail": "BESS operating modes", "by": "Confluence", "recorded_iid": D(_dt(2025, 11, 10))},
        confidence="low", status="superseded", qualifiers={"soc_target_pct": 80})

    # ================================================================ 11. write channels
    for ch in channels:
        ch.messages.sort(key=lambda m: m["created_iid"])
        c.write_jsonl(["teams", "messages", f"{ch.id}.jsonl"], ch.messages)
        c.write_json(["teams", "messages", f"{ch.id}.json"], ch.to_json())
        c.write_text(["teams", "exports", f"{ch.id}.html"], _render_channel_html(ch))
    c.write_json(["teams", "channels.json"], [{"channel_id": ch.id, "name": ch.name, "team": ch.team,
                                               "topic": ch.topic,
                                               "members": [{"name": ctx.p(k)["name"], "upn": ctx.p(k)["upn"],
                                                            "role": ctx.p(k)["role"]} for k in ch.members]}
                                              for ch in channels])

    # ================================================================ 12. drop-rate sheet
    n_case_mentions = sum(1 for ch in channels for m in ch.messages
                          if m["conversation_id"] == thread or m.get("conversation_id") == th_t2)
    drop = [
        ["teams", len(ch_transport.messages) + len(ch_field.messages) + len(ch_sre.messages) + len(ch_access.messages) + len(ch_energy.messages) + len(ch_services.messages),
         "reconstructable", "raw message objects carry attachments/reactions/edits; Teams drops nothing in this export"],
        ["email", len(emails), "reconstructable", "all .msg files incl. Sent Items; attachments present"],
        ["phone_call", 0, "dropped_at_source", "calls are not recorded; this channel is invisible to any pipeline"],
        ["in_person", 0, "dropped_at_source", "hallway/lab conversations are not recorded"],
        ["meeting_notes", 2, "reconstructable", "PDF in documents/expert_notes"],
    ]
    c.write_csv(["knowledge_graph", "drop_rate_sheet.csv"],
                ["channel", "captured_items", "capture_status", "note"], drop)

    # KB only 4 pages - a hint for a pipeline 'completeness' report
    return ctx


def _render_channel_html(ch: "Channel") -> str:
    rows = []
    for m in ch.messages:
        replies = "".join(
            f"<div class='reply'><b>{r['author_name']}</b> <span class='t'>{r['created_at']}</span><br>"
            f"{r['text'].replace(chr(10), '<br>')}</div>" for r in m.get("replies", []))
        att = "".join(f"<div class='att'>[attachment] {a['name']} ({a['content_type']})</div>"
                      for a in m.get("attachments", []))
        rows.append(f"<div class='msg'><b>{m['author_name']}</b> <span class='t'>{m['created_at']}</span>"
                    f"{' <span class=edited>edited</span>' if m['edited'] else ''}<br>{m['text'].replace(chr(10), '<br>')}"
                    f"{att}{replies}</div>")
    return ("<!doctype html><html><head><meta charset='utf-8'><title>" + ch.name + " export</title>"
            "<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:900px;margin:2em auto}"
            ".msg{border-bottom:1px solid #ddd;padding:8px 0}.t{color:#777;font-size:11px}"
            ".reply{margin-left:2em;border-left:2px solid #ccc;padding-left:8px;color:#333}"
            ".att{color:#0a66c2;font-size:12px}.edited{color:#a30;font-size:10px}</style></head><body>"
            f"<h2>{ch.team} / {ch.name}</h2><p>{ch.topic}</p>" + "".join(rows) + "</body></html>")


def _tiny_png(text: bytes) -> bytes:
    """A 1x1 PNG is not useful as 'screenshot evidence'; build a small 96x24 black/white text strip."""
    w, h = 96, 24
    rows = []
    # crude 5x7 font free approach: draw a scaled barcode-like pattern deterministic from text
    digest = hashlib.sha256(text).digest()
    bits = "".join(f"{b:08b}" for b in digest)
    for y in range(h):
        row = bytearray()
        for x in range(w):
            idx = (y * w + x) % len(bits)
            px = 0 if bits[idx] == "1" else 255
            row += bytes([px, px, px])
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_outputs(ctx: "Ctx") -> None:
    c.write_jsonl(["knowledge_graph", "entities.jsonl"], _dedupe(ctx.entities))
    c.write_jsonl(["knowledge_graph", "assertions.jsonl"], ctx.assertions)
    c.write_json(["knowledge_graph", "contradictions.json"], ctx.contradictions)
    c.write_json(["knowledge_graph", "derived_insights.json"], ctx.insights)
    c.write_jsonl(["knowledge_graph", "access_register.jsonl"], ctx.access_rows)
    c.write_json(["knowledge_graph", "assertion_schema.json"], _schema())


def _dedupe(rows):
    seen = set()
    out = []
    for r in rows:
        if r["entity_id"] in seen:
            continue
        seen.add(r["entity_id"])
        out.append(r)
    return out


def _schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ContextAssertion",
        "type": "object",
        "required": ["assertion_id", "subject_entity_id", "topic", "statement", "valid_from", "valid_to",
                     "source_type", "source_ref", "confidence", "status"],
        "properties": {
            "assertion_id": {"type": "string"},
            "subject_entity_id": {"type": "string", "description": "bound graph entity (see entities.jsonl)"},
            "topic": {"type": "string"},
            "statement": {"type": "string"},
            "valid_from": {"type": "string", "format": "date-time"},
            "valid_to": {"type": ["string", "null"], "format": "date-time",
                         "description": "null = still believed true"},
            "competing_versions": {"type": "array"},
            "source_type": {"enum": ["teams_message", "email", "pdf", "confluence", "xlsx", "jira", "itsm"]},
            "source_ref": {"type": "string"},
            "confidence": {"enum": ["low", "medium", "high"]},
            "status": {"enum": ["believed_true", "superseded", "disputed", "expired", "unverified"]},
            "supersedes": {"type": "string"},
            "contradicts": {"type": "array", "items": {"type": "string"}},
        },
    }


def main() -> None:
    with open(os.path.join(c.root_dir(), "network", "registry.json"), encoding="utf-8") as fh:
        reg = json.load(fh)
    meta = {"case": reg["case"]}
    ctx = build(meta)
    write_outputs(ctx)
    print(f"context: {len(ctx.assertions)} assertions, {len(ctx.entities)} entities, "
          f"{len(ctx.contradictions)} contradictions, {len(ctx.insights)} insights")


if __name__ == "__main__":
    main()
