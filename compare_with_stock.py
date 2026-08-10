#!/usr/bin/env python3
"""
compare_with_stock.py -- diff the StarCraft 64 scenarios against the stock PC
maps installed on this machine.

    python compare_with_stock.py "StarCraft 64 (USA).n64" --stock "I:/Blizzard/StarCraft"

Answers "did the N64 port ship the same maps?" by reading genuine Blizzard
maps -- which are encrypted and PKWARE-imploded, unlike the ones this project
writes -- pulling their scenario.chk, and diffing section by section against
the CHKs taken straight from the ROM.

Retail campaign maps are NOT reachable this way: modern installs keep them in
the CASC store under Data\\, not in the legacy MPQs, so only the melee and
scenario maps shipped under Maps\\ can be compared.

Nothing here is needed to extract or play the maps; it is a fidelity check.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import bz2
import collections
import glob
import hashlib
import os
import re
import struct
import sys
import zlib

from pkware_explode import explode
from extract_sc64_maps import (BoltArchive, chk_sections, load_rom,
                               looks_like_chk, parse_map)

# --------------------------------------------------------------------------
# MPQ reading, for archives we did not write
# --------------------------------------------------------------------------
# Genuine maps use features our own writer never emits: the block entry is
# encrypted, and sectors are PKWARE-imploded. Flag meanings are from
# reference/StormLib/src/StormLib.h.

IMPLODE, COMPRESS = 0x00000100, 0x00000200
ENCRYPTED, FIX_KEY = 0x00010000, 0x00020000
SINGLE_UNIT, SECTOR_CRC, EXISTS = 0x01000000, 0x04000000, 0x80000000


def _build_crypt_table() -> list[int]:
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(0x100):
        for j in range(i, 0x500, 0x100):
            seed = (seed * 125 + 3) % 0x2AAAAB
            high = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            table[j] = high | (seed & 0xFFFF)
    return table


_CRYPT = _build_crypt_table()


def _hash(name: str, kind: int) -> int:
    seed1, seed2 = 0x7FED7FED, 0xEEEEEEEE
    for ch in name.upper().replace("/", "\\").encode("latin1"):
        seed1 = (_CRYPT[(kind << 8) + ch] ^ (seed1 + seed2)) & 0xFFFFFFFF
        seed2 = (ch + seed1 + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return seed1


def _decrypt(data: bytes, key: int) -> bytes:
    count = len(data) // 4
    if not count:
        return data
    values = list(struct.unpack(f"<{count}I", data[:count * 4]))
    seed = 0xEEEEEEEE
    for i in range(count):
        seed = (seed + _CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        plain = (values[i] ^ (key + seed)) & 0xFFFFFFFF
        key = ((((~key) << 0x15) + 0x11111111) | (key >> 0x0B)) & 0xFFFFFFFF
        seed = (plain + seed + (seed << 5) + 3) & 0xFFFFFFFF
        values[i] = plain
    return struct.pack(f"<{count}I", *values) + data[count * 4:]


def _decompress(sector: bytes, imploded: bool) -> bytes:
    # An MPQ_FILE_IMPLODE file carries no compression mask byte -- the whole
    # sector is PKWARE data. Only MPQ_FILE_COMPRESS prefixes a mask.
    if imploded:
        return explode(sector)
    mask, body = sector[0], sector[1:]
    if mask == 0x08:
        return explode(body)
    if mask == 0x02:
        return zlib.decompress(body)
    if mask == 0x10:
        return bz2.decompress(body)
    raise ValueError(f"unsupported compression mask {mask:#04x}")


class StockMpq:
    """Read-only MPQ reader covering what genuine StarCraft maps use."""

    def __init__(self, path: str):
        with open(path, "rb") as fh:
            self.raw = fh.read()
        base = self.raw.find(b"MPQ\x1a")
        if base < 0:
            raise ValueError("no MPQ header")
        self.base = base
        (_hs, _asz, _fv, shift, hash_pos, block_pos, hash_n, block_n) = \
            struct.unpack_from("<IIHHIIII", self.raw, base + 4)
        self.sector_size = 512 << shift
        self.hash_n = hash_n & 0x0FFFFFFF      # protectors inflate this
        self.hash_table = _decrypt(
            self.raw[base + hash_pos:base + hash_pos + self.hash_n * 16],
            _hash("(hash table)", 3))
        self.block_table = _decrypt(
            self.raw[base + block_pos:base + block_pos + block_n * 16],
            _hash("(block table)", 3))

    def _lookup(self, name: str):
        start = _hash(name, 0) & (self.hash_n - 1)
        want_a, want_b = _hash(name, 1), _hash(name, 2)
        for probe in range(self.hash_n):
            slot = (start + probe) & (self.hash_n - 1)
            a, b, _locale, block = struct.unpack_from("<IIIi", self.hash_table, slot * 16)
            if block == -1:
                return None
            if a == want_a and b == want_b:
                return block
        return None

    def read(self, name: str):
        index = self._lookup(name)
        if index is None:
            return None
        offset, packed, size, flags = struct.unpack_from(
            "<IIII", self.block_table, index * 16)
        if not flags & EXISTS or size == 0:
            return None
        pos = self.base + offset

        key = _hash(name.split("\\")[-1], 3)
        if flags & FIX_KEY:
            key = ((key + offset) ^ size) & 0xFFFFFFFF
        imploded = bool(flags & IMPLODE)
        compressed = bool(flags & (IMPLODE | COMPRESS))

        if flags & SINGLE_UNIT:
            data = self.raw[pos:pos + packed]
            if flags & ENCRYPTED:
                data = _decrypt(data, key)
            return _decompress(data, imploded) if compressed and packed < size else data[:size]
        if not compressed:
            return self.raw[pos:pos + size]

        count = (size - 1) // self.sector_size + 1
        entries = count + 1 + (1 if flags & SECTOR_CRC else 0)
        table = self.raw[pos:pos + entries * 4]
        if flags & ENCRYPTED:
            table = _decrypt(table, (key - 1) & 0xFFFFFFFF)
        positions = struct.unpack(f"<{entries}I", table)

        out = bytearray()
        for i in range(count):
            sector = self.raw[pos + positions[i]:pos + positions[i + 1]]
            if flags & ENCRYPTED:
                sector = _decrypt(sector, (key + i) & 0xFFFFFFFF)
            plain = min(self.sector_size, size - len(out))
            out += sector[:plain] if len(sector) >= plain else _decompress(sector, imploded)
        return bytes(out)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

COMPARED = [b"DIM ", b"ERA ", b"MTXM", b"UNIT", b"THG2", b"TRIG",
            b"MBRF", b"STR ", b"OWNR", b"SIDE", b"FORC", b"MRGN"]

# CHK UNIT is a flat array of 36-byte records.
UNIT_FIELDS = [
    ("serial", 0, 4), ("x", 4, 2), ("y", 6, 2), ("type", 8, 2),
    ("relation", 10, 2), ("special", 12, 2), ("valid", 14, 2), ("owner", 16, 1),
    ("hp%", 17, 1), ("shield%", 18, 1), ("energy%", 19, 1), ("resources", 20, 4),
    ("hangar", 24, 2), ("flags", 26, 2), ("unused", 28, 4), ("linked", 32, 4),
]
UNIT_RECORD = 36


def sections(chk: bytes) -> dict:
    latest = {}
    for tag, payload in chk_sections(chk):
        latest[tag] = payload          # later overrides earlier
    return latest


def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_stock(root: str, verbose: bool = False):
    """Read every stock map under root/Maps, skipping our own output."""
    patterns = [os.path.join(root, "Maps", "**", "*.scm"),
                os.path.join(root, "Maps", "**", "*.scx")]
    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern, recursive=True))
    paths = [p for p in paths if f"{os.sep}sc64{os.sep}" not in p]

    maps, failures = {}, []
    for path in sorted(paths):
        try:
            chk = StockMpq(path).read("staredit\\scenario.chk")
            if not chk or not looks_like_chk(chk):
                failures.append(path)
                continue
            info = parse_map(os.path.basename(path), chk)
            maps.setdefault(normalise(info.name), []).append((path, info, chk))
        except Exception as exc:                      # noqa: BLE001
            failures.append(path)
            if verbose:
                print(f"  ! {os.path.basename(path)}: {exc}", file=sys.stderr)
    return maps, failures, len(paths)


def load_sc64(rom_path: str):
    archive = BoltArchive(load_rom(rom_path))
    out = []
    for entry in archive.entries():
        if not entry.path.startswith("008/"):
            continue
        try:
            if archive.read(entry, limit=4)[:4] not in (b"TYPE", b"VER ", b"IVER"):
                continue
            data = archive.read(entry)
        except (ValueError, IndexError):
            continue
        if looks_like_chk(data):
            out.append((entry.path, parse_map(entry.path, data), data))
    return out


def unit_field_diffs(a: bytes, b: bytes) -> collections.Counter:
    """Which UNIT record fields differ, counted across records."""
    tally = collections.Counter()
    if len(a) != len(b):
        return tally
    for off in range(0, len(a), UNIT_RECORD):
        ra, rb = a[off:off + UNIT_RECORD], b[off:off + UNIT_RECORD]
        if ra == rb:
            continue
        for name, start, length in UNIT_FIELDS:
            if ra[start:start + length] != rb[start:start + length]:
                tally[name] += 1
    return tally


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare StarCraft 64 scenarios with the stock PC maps.")
    parser.add_argument("rom", help="StarCraft 64 ROM")
    parser.add_argument("--stock", default=None,
                        help="StarCraft install directory (auto-detected if omitted)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.stock is None:
        from starcraft_install import find_install
        install = find_install()
        if not install:
            print("error: no StarCraft install found; pass --stock DIR "
                  "or set STARCRAFT_DIR", file=sys.stderr)
            return 1
        args.stock = install.root
        print(f"using StarCraft install: {args.stock}")

    if not os.path.isdir(os.path.join(args.stock, "Maps")):
        print(f"error: no Maps folder under {args.stock!r}", file=sys.stderr)
        return 1

    stock, failures, total = load_stock(args.stock, args.verbose)
    read_ok = sum(len(v) for v in stock.values())
    print(f"stock maps: {read_ok} readable of {total}")
    if failures:
        folders = collections.Counter(
            os.path.relpath(os.path.dirname(p), os.path.join(args.stock, "Maps"))
            for p in failures)
        top = ", ".join(f"{k} ({v})" for k, v in folders.most_common(4))
        print(f"  {len(failures)} unreadable, concentrated in: {top}")

    sc64 = load_sc64(args.rom)
    print(f"sc64 scenarios: {len(sc64)}\n")

    header = f"{'sc64 scenario':24} {'stock file':30} {'terrain':>8} {'units':>10} {'triggers':>9}"
    print(header)
    print("-" * len(header))

    matched = 0
    identical_terrain = 0
    field_tally = collections.Counter()
    unmatched = []
    for path, info, chk in sc64:
        candidates = stock.get(normalise(info.name))
        if not candidates:
            unmatched.append(info)
            continue
        matched += 1
        stock_path, stock_info, stock_chk = candidates[0]
        ours, theirs = sections(chk), sections(stock_chk)

        same_terrain = ours.get(b"MTXM") == theirs.get(b"MTXM")
        identical_terrain += 1 if same_terrain else 0
        a, b = ours.get(b"UNIT", b""), theirs.get(b"UNIT", b"")
        field_tally.update(unit_field_diffs(a, b))

        units = "identical" if a == b else f"{len(a)//UNIT_RECORD}/{len(b)//UNIT_RECORD}"
        trig_a, trig_b = ours.get(b"TRIG", b""), theirs.get(b"TRIG", b"")
        triggers = "identical" if trig_a == trig_b else f"{len(trig_a)//2400}/{len(trig_b)//2400}"
        print(f"{info.name[:24]:24} {os.path.basename(stock_path)[:30]:30} "
              f"{'same' if same_terrain else 'differs':>8} {units:>10} {triggers:>9}")

    print(f"\nmatched by scenario name : {matched}")
    print(f"byte-identical terrain   : {identical_terrain} of {matched}")
    if field_tally:
        print("\nUNIT record fields that differ (across all matched pairs):")
        for name, count in field_tally.most_common():
            print(f"  {name:10} {count} records")
        untouched = [n for n, _, _ in UNIT_FIELDS if n not in field_tally]
        print(f"  identical in every record: {', '.join(untouched)}")
    print(f"\nsc64 scenarios with no stock counterpart: {len(unmatched)} "
          f"(campaign missions live in the CASC store, not under Maps\\)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
