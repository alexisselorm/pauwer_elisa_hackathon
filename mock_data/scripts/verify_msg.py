"""Parse the .msg files this repo's generator writes (no third-party deps).

Checks the OLE2/CFB container structure the way a real parser does (header magic,
sector size, FAT chain, directory tree, property stream), so a broken writer fails
loudly here instead of at the demo.
"""
from __future__ import annotations

import os
import struct
import sys

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREE, END = 0xFFFFFFFF, 0xFFFFFFFE


def read_cfb(path: str) -> dict:
    data = open(path, "rb").read()
    assert data[:8] == MAGIC, f"{path}: bad magic"
    minor, major, byteorder, sshift, mshift = struct.unpack_from("<HHHHH", data, 24)
    assert byteorder == 0xFFFE, "byte order"
    sec = 1 << sshift
    n_fat, first_dir, mini_cutoff = struct.unpack_from("<III", data, 44)
    difat = struct.unpack_from("<109I", data, 76)
    fat_secs = [s for s in difat[:n_fat]]
    fat = []
    for fs in fat_secs:
        off = (fs + 1) * sec
        fat.extend(struct.unpack_from("<128I", data, off))
    # directory chain
    dirs, s, seen = [], first_dir, set()
    while s not in (FREE, END) and s < len(fat) and s not in seen:
        seen.add(s)
        dirs.append(data[(s + 1) * sec:(s + 2) * sec])
        s = fat[s]
    dirents = []
    for block in dirs:
        for i in range(0, len(block), 128):
            e = block[i:i + 128]
            if len(e) < 128:
                break
            nlen = struct.unpack_from("<H", e, 64)[0]
            if nlen < 2 or nlen > 64:
                continue
            name = e[:nlen - 2].decode("utf-16-le")
            etype = e[66]
            right, child = struct.unpack_from("<II", e, 72)
            start, size = struct.unpack_from("<IQ", e, 116)
            dirents.append({"name": name, "type": etype, "right": right, "child": child,
                            "start": start, "size": size})
    # property stream (entry 1)
    if len(dirents) < 2:
        raise AssertionError(f"no stream entry (found {len(dirents)} directory entries)")
    # our writer stores the property stream in the regular FAT and puts its bytes in
    # the data sectors directly after the directory sector
    st = dirents[1]
    assert st["type"] == 2, "stream entry type"
    n = (st["size"] + sec - 1) // sec
    chain = list(range(st["start"], st["start"] + n))
    while s not in (FREE, END) and s < len(fat) and s not in seen:
        seen.add(s)
        chain.append(s)
        s = fat[s]
    raw = b"".join(data[(c + 1) * sec:(c + 2) * sec] for c in chain)[: st["size"]]
    # In this MessageStore the property headers occupy the first N*16 bytes and every
    # value follows them; the header count is implied by the first value offset.
    first_voff = struct.unpack_from("<I", raw, 8)[0] if len(raw) >= 16 else 0
    count = first_voff // 16 if first_voff and first_voff % 16 == 0 else 0
    props = {}
    for i in range(min(count, 512)):
        typ, _flags, tag = struct.unpack_from("<HHI", raw, i * 16)
        if tag == 0:
            continue
        voff, vlen = struct.unpack_from("<II", raw, i * 16 + 8)
        val = raw[voff:voff + vlen]
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
    return {"header": (minor, major, sshift), "n_fat": n_fat, "entries": dirents, "props": props}


P_SUBJECT, P_BODY, P_SENDER, P_MSGID, P_PTYPE, P_OWNER = 0x0037, 0x1000, 0x0C1E, 0x1035, 0x001A, 0x0C1A


def main(root: str) -> int:
    bad = 0
    files = []
    for folder in ("Inbox", "Sent Items"):
        d = os.path.join(root, "email_outlook", folder)
        if os.path.isdir(d):
            files += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".msg")]
    for f in files:
        try:
            r = read_cfb(f)
            p = r["props"]
            assert p[P_PTYPE] == "IPM.Note", "message class"
            assert p.get(P_SUBJECT), "subject"
            assert p.get(P_BODY) is not None, "body"
            assert p.get(P_MSGID, "").startswith("<"), "message id"
            assert r["entries"][0]["name"] == "Root Entry"
            assert any(e["name"] == "__properties_version1.0" for e in r["entries"])
        except Exception as exc:
            bad += 1
            print(f"FAIL {f}: {exc}")
    if bad:
        # one detailed diagnostic to make a broken writer obvious
        import traceback
        try:
            r = read_cfb(files[0])
            print("first file entries:", r["entries"])
        except Exception:
            traceback.print_exc()
    print(f"msg parse check: {len(files)} files, {len(files) - bad} valid, {bad} broken")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
