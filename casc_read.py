#!/usr/bin/env python3
"""
casc_read.py -- read files out of a local Blizzard CASC storage.

    python casc_read.py "I:/Blizzard/StarCraft" --info

CASC replaced MPQ in Blizzard's modern products. StarCraft: Remastered keeps
its campaign maps here rather than in the legacy MPQs, and every game after
StarCraft II uses it, so this is the way in to anything newer.

Structure, and the chain needed to reach one file:

    .build.info            pipe-separated, gives the build config's MD5
    Data/config/xx/yy/hash  build config: names the encoding and root files
    Data/indices/*.idx     maps an encoding key -> (archive, offset, size)
    Data/data/data.NNN     the archives themselves, holding BLTE-framed blobs

BLTE is the container each blob is wrapped in; its frames may be stored,
zlib-compressed, LZ4, recursive, or Salsa20-encrypted.

Structures follow the vendored reference/CascLib (MIT) rather than guesswork.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib


# --------------------------------------------------------------------------
# .build.info and the config files
# --------------------------------------------------------------------------

def parse_build_info(root: str) -> list[dict]:
    """Parse .build.info -- a pipe-separated table with a typed header row."""
    path = os.path.join(root, ".build.info")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = [l.rstrip("\n") for l in fh if l.strip()]
    if not lines:
        raise ValueError(".build.info is empty")

    # Header cells look like "Build Key!HEX:16"; keep the name only.
    names = [c.split("!")[0].strip() for c in lines[0].split("|")]
    rows = []
    for line in lines[1:]:
        cells = line.split("|")
        rows.append({n: (cells[i] if i < len(cells) else "")
                     for i, n in enumerate(names)})
    return rows


def config_path(root: str, key: str) -> str:
    """Config files are stored under Data/config/<first byte>/<second byte>/."""
    return os.path.join(root, "Data", "config", key[0:2], key[2:4], key)


def parse_config(path: str) -> dict:
    """Build/CDN config: '# comment' lines plus 'name = value...' pairs."""
    out = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            name, _, value = line.partition("=")
            out[name.strip()] = value.split()
    return out


# --------------------------------------------------------------------------
# BLTE
# --------------------------------------------------------------------------

BLTE_MAGIC = b"BLTE"


def blte_decode(data: bytes, key_lookup=None) -> bytes:
    """Decode a BLTE blob into its plain contents.

    Frame modes: 'N' stored, 'Z' zlib, '4' LZ4, 'F' recursive BLTE,
    'E' encrypted (Salsa20 -- needs a key we usually do not have).
    """
    if data[:4] != BLTE_MAGIC:
        raise ValueError(f"not BLTE (magic {data[:4]!r})")
    header_size = struct.unpack_from(">I", data, 4)[0]

    if header_size == 0:
        # Single implicit frame covering the rest of the blob.
        return _blte_frame(data[8:], key_lookup)

    flags, count_hi, count_lo = struct.unpack_from(">BBH", data, 8)
    chunk_count = (count_hi << 16) | count_lo
    out = bytearray()
    pos = 12
    offset = header_size
    for _ in range(chunk_count):
        packed, plain = struct.unpack_from(">II", data, pos)
        pos += 24                       # 4 packed + 4 plain + 16-byte checksum
        frame = data[offset:offset + packed]
        offset += packed
        out += _blte_frame(frame, key_lookup)
    return bytes(out)


def _blte_frame(frame: bytes, key_lookup) -> bytes:
    mode = frame[:1]
    body = frame[1:]
    if mode == b"N":
        return body
    if mode == b"Z":
        return zlib.decompress(body)
    if mode == b"F":
        return blte_decode(body, key_lookup)
    if mode == b"4":
        raise NotImplementedError("LZ4 frame")
    if mode == b"E":
        raise NotImplementedError(
            "encrypted frame (Salsa20); the key is not in the local storage")
    raise ValueError(f"unknown BLTE frame mode {mode!r}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def describe(root: str) -> int:
    rows = parse_build_info(root)
    print(f"{'.build.info':<16} {len(rows)} build(s)")
    for row in rows:
        active = row.get("Active", "?")
        print(f"  product={row.get('Product','?')} branch={row.get('Branch','?')} "
              f"active={active} version={row.get('Version','?')}")
        for field in ("Build Key", "CDN Key", "Install Key"):
            print(f"    {field:12} {row.get(field, '')}")

    row = next((r for r in rows if r.get("Active") == "1"), rows[0])
    build_key = row.get("Build Key", "")
    path = config_path(root, build_key)
    print(f"\nbuild config: {os.path.relpath(path, root)}")
    if not os.path.exists(path):
        print("  MISSING -- storage may be incomplete")
        return 1
    cfg = parse_config(path)
    for k in sorted(cfg):
        v = cfg[k]
        print(f"  {k:24} {' '.join(v)[:110]}")

    # Note the layout: the .idx bucket files sit next to the archives in
    # Data/data, while Data/indices holds CDN-style .index files.
    data_dir = os.path.join(root, "Data", "data")
    idx_dir = os.path.join(root, "Data", "indices")
    archives = sorted(f for f in os.listdir(data_dir) if f.startswith("data."))
    idx = sorted(f for f in os.listdir(data_dir) if f.endswith(".idx"))
    index = sorted(f for f in os.listdir(idx_dir)) if os.path.isdir(idx_dir) else []
    print(f"\narchives    : {len(archives)}  {archives[:3]}")
    print(f".idx buckets: {len(idx)}  {idx[:3]}")
    print(f".index files: {len(index)}  {index[:2]}")
    total = sum(os.path.getsize(os.path.join(data_dir, a)) for a in archives)
    print(f"archive size: {total / 2**30:.2f} GiB")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read a local CASC storage.")
    parser.add_argument("root", help="game install directory (holds .build.info)")
    parser.add_argument("--info", action="store_true",
                        help="describe the storage and exit")
    args = parser.parse_args(argv)

    if not os.path.exists(os.path.join(args.root, ".build.info")):
        print(f"error: no .build.info in {args.root!r}", file=sys.stderr)
        return 1
    return describe(args.root)


if __name__ == "__main__":
    sys.exit(main())
