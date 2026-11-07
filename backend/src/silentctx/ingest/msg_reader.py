"""Outlook `.msg` (OLE2/CFB) reader for the email backfill (BE-2.2). Standard library only.

Ported from `mock_data/scripts/verify_msg.py`, which stays standalone so the generator's
checks never depend on the backend. It reads the container the way a real parser does
(header magic, FAT chain, directory, property stream) and returns the MAPI properties.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FREE, _END = 0xFFFFFFFF, 0xFFFFFFFE

# MAPI property ids.
PR_MESSAGE_CLASS = 0x001A
PR_SUBJECT = 0x0037
PR_CLIENT_SUBMIT_TIME = 0x0039
PR_TRANSPORT_MESSAGE_HEADERS = 0x007D
PR_SENDER_NAME = 0x0C1A
PR_SENDER_EMAIL = 0x0C1F
PR_BODY = 0x1000
PR_INTERNET_MESSAGE_ID = 0x1035
PR_IN_REPLY_TO_ID = 0x1042

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


class MsgError(ValueError):
    """Not a readable .msg file."""


class MsgFields(BaseModel):
    message_class: str
    subject: str
    body: str
    message_id: str
    sent_at: datetime  # PR_CLIENT_SUBMIT_TIME, UTC
    sender_name: str | None = None
    sender_email: str | None = None
    in_reply_to: str | None = None
    references: str | None = None


def filetime_to_datetime(value: int) -> datetime:
    """Windows FILETIME (100 ns ticks since 1601-01-01 UTC)."""
    return _FILETIME_EPOCH + timedelta(microseconds=value // 10)


def read_properties(path: Path) -> dict[int, Any]:
    """The MAPI property stream of a .msg written as one stream after the directory."""
    data = path.read_bytes()
    if data[:8] != MAGIC:
        raise MsgError(f"{path.name}: not an OLE2/CFB file")
    (byteorder,) = struct.unpack_from("<H", data, 28)
    (sshift,) = struct.unpack_from("<H", data, 30)
    if byteorder != 0xFFFE:
        raise MsgError(f"{path.name}: bad byte order")
    sec = 1 << sshift
    n_fat, first_dir = struct.unpack_from("<II", data, 44)
    difat = struct.unpack_from("<109I", data, 76)
    fat: list[int] = []
    for fs in difat[:n_fat]:
        fat.extend(struct.unpack_from(f"<{sec // 4}I", data, (fs + 1) * sec))
    blocks, s, seen = [], first_dir, set()
    while s not in (_FREE, _END) and s < len(fat) and s not in seen:
        seen.add(s)
        blocks.append(data[(s + 1) * sec : (s + 2) * sec])
        s = fat[s]
    entries = []
    for block in blocks:
        for i in range(0, len(block) - 127, 128):
            e = block[i : i + 128]
            (nlen,) = struct.unpack_from("<H", e, 64)
            if not 2 <= nlen <= 64:
                continue
            start, size = struct.unpack_from("<IQ", e, 116)
            entries.append({"name": e[: nlen - 2].decode("utf-16-le"), "type": e[66],
                            "start": start, "size": size})  # fmt: skip
    if len(entries) < 2 or entries[1]["type"] != 2:
        raise MsgError(f"{path.name}: no property stream")
    stream = entries[1]
    n = (stream["size"] + sec - 1) // sec
    chain = range(stream["start"], stream["start"] + n)
    raw = b"".join(data[(c + 1) * sec : (c + 2) * sec] for c in chain)[: stream["size"]]
    # Property headers take the first N*16 bytes; N follows from the first value offset.
    first = struct.unpack_from("<I", raw, 8)[0] if len(raw) >= 16 else 0
    count = first // 16 if first and first % 16 == 0 else 0
    props: dict[int, Any] = {}
    for i in range(min(count, 512)):
        typ, _flags, tag = struct.unpack_from("<HHI", raw, i * 16)
        if tag == 0:
            continue
        voff, vlen = struct.unpack_from("<II", raw, i * 16 + 8)
        val = raw[voff : voff + vlen]
        if typ == 0x1F:
            props[tag] = val.decode("utf-16-le").rstrip("\0")
        elif typ == 0x1E:
            props[tag] = val.decode("cp1252").rstrip("\0")
        elif typ == 0x03:
            props[tag] = struct.unpack("<I", val)[0]
        elif typ == 0x40:
            props[tag] = struct.unpack("<Q", val)[0]
        else:
            props[tag] = val
    return props


def _header(headers: str, name: str) -> str | None:
    prefix = name.casefold() + ":"
    for line in headers.splitlines():
        if line.casefold().startswith(prefix):
            return line[len(prefix) :].strip() or None
    return None


def read_msg(path: Path) -> MsgFields:
    p = read_properties(path)
    if PR_CLIENT_SUBMIT_TIME not in p or PR_INTERNET_MESSAGE_ID not in p:
        raise MsgError(f"{path.name}: no submit time or Message-ID")
    headers = str(p.get(PR_TRANSPORT_MESSAGE_HEADERS, ""))
    return MsgFields(
        message_class=str(p.get(PR_MESSAGE_CLASS, "")),
        subject=str(p.get(PR_SUBJECT, "")),
        body=str(p.get(PR_BODY, "")),
        message_id=str(p[PR_INTERNET_MESSAGE_ID]),
        sent_at=filetime_to_datetime(int(p[PR_CLIENT_SUBMIT_TIME])),
        sender_name=p.get(PR_SENDER_NAME),
        sender_email=p.get(PR_SENDER_EMAIL),
        in_reply_to=p.get(PR_IN_REPLY_TO_ID) or _header(headers, "In-Reply-To"),
        references=_header(headers, "References"),
    )
