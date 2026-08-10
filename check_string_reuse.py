#!/usr/bin/env python3
"""
check_string_reuse.py -- regression guard for briefing injection.

    python check_string_reuse.py gamedata/maps

Injecting an MBRF section means allocating new STR entries, which means
knowing which entries are already spoken for. Get that wrong and briefing
dialogue overwrites a live string -- most visibly a custom unit name, which
then shows up as a unit's name in game.

That happened: the allocator located the custom-unit-name u16[228] array by
measuring back from the end of UNIS/UNIx, but that array is NOT the last field
(the weapon-damage and upgrade-bonus arrays follow it), so the read landed 400
or 520 bytes late and missed every real name. 12 live names across 8 maps were
overwritten before it was caught.

This script exists because the validator that should have caught it computed
its reference set by calling the same buggy function -- a circular check that
can only ever agree with itself. So this deliberately hardcodes its own offset
constant (3192, confirmed against a stock Blizzard UMS map) and re-reads the
originals straight from the ROM, sharing no code with the injector.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
import argparse
import glob
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from extract_sc64_maps import (load_rom, BoltArchive, looks_like_chk,
                               parse_map, chk_sections)
from verify_maps import MpqReader

UNIT_NAME_OFFSET = 3192          # verified against a stock Blizzard UMS map

parser = argparse.ArgumentParser(
    description="Check that briefing injection did not overwrite a string "
                "some other CHK section still references.")
parser.add_argument("maps", nargs="?",
                    default=os.path.join(HERE, "gamedata", "maps"),
                    help="directory of generated .scm/.scx files")
parser.add_argument("--rom", help="ROM to compare against "
                                  "(auto-detected if omitted)")
_args = parser.parse_args()
OUT = _args.maps


def secmap(chk):
    d = {}
    for tag, payload in chk_sections(chk):
        d[tag] = payload
    return d


def unit_name_indices(chk):
    out = set()
    for tag, payload in secmap(chk).items():
        if tag not in (b"UNIS", b"UNIx"):
            continue
        if len(payload) >= UNIT_NAME_OFFSET + 228 * 2:
            for i in range(228):
                v = struct.unpack_from("<H", payload, UNIT_NAME_OFFSET + i * 2)[0]
                if v:
                    out.add(v)
    return out


def strings(chk):
    s = secmap(chk).get(b"STR ")
    if not s or len(s) < 2:
        return {}
    n = struct.unpack_from("<H", s, 0)[0]
    out = {}
    for i in range(1, n + 1):
        off = struct.unpack_from("<H", s, 2 + (i - 1) * 2)[0]
        if off >= len(s):
            continue
        end = s.find(b"\x00", off)
        out[i] = s[off:end if end >= 0 else len(s)]
    return out


# originals straight from the ROM
rom_path = _args.rom
if not rom_path:
    from sc64 import find_rom
    rom_path = find_rom(None)
if not rom_path:
    print("error: no ROM found; pass --rom PATH", file=sys.stderr)
    sys.exit(2)
arc = BoltArchive(load_rom(rom_path))
orig = {}
for e in arc.entries():
    if not e.path.startswith("008/"):
        continue
    try:
        if arc.read(e, limit=4)[:4] not in (b"TYPE", b"VER ", b"IVER"):
            continue
        d = arc.read(e)
    except Exception:
        continue
    if looks_like_chk(d):
        orig[e.path] = d

clobbered = []
checked = 0
for path in sorted(glob.glob(os.path.join(OUT, "*.scm")) + glob.glob(os.path.join(OUT, "*.scx"))):
    bolt = os.path.basename(path)[:7].replace("-", "/")
    if bolt not in orig:
        continue
    new = MpqReader(path).read("staredit\\scenario.chk")
    checked += 1
    names = unit_name_indices(orig[bolt])
    before, after = strings(orig[bolt]), strings(new)
    for idx in sorted(names):
        if idx in before and idx in after and before[idx] != after[idx]:
            clobbered.append((bolt, idx, len(before[idx]), len(after[idx])))

print(f"maps checked: {checked}")
print(f"unit-name string indices overwritten: {len(clobbered)}")
for bolt, idx, a, b in clobbered[:20]:
    print(f"   {bolt} index {idx}: {a} -> {b} bytes")
if not clobbered:
    print("CLEAN -- no unit name was overwritten by briefing text")
sys.exit(1 if clobbered else 0)
