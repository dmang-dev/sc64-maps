"""Read and edit the melee Scenario list in a StarCraft 64 ROM.

The Scenario tab of the mission-select screen is StarCraft 64's melee mode.
Its list is driven by three structures in the static segment:

    0x0D15F4   label strings, NUL terminated
    0x0D16BC   eleven big-endian pointers to those strings
    0x0D16E8   ten 2-byte records, {u8 map_id, u8 opponents}

`map_id + 60` is the map index, and `map_id + 68` the BOLT file number in
directory 008. 60 is Challenger, the first melee map. See docs/FORMAT.md §9 for
how that decode was established and confirmed on hardware.

Editing those records is what makes a custom melee lineup possible: an entry
can be pointed at any map from index 60 to 95, so a scenario injected into an
unused slot becomes selectable without displacing one the stock list shows.

Two things this handles that are easy to get wrong:

  * The table is at file offset 857,832, INSIDE the CIC boot checksum window.
    Every edit here needs the header repaired or the ROM will not boot at all.
    That is done automatically.
  * Injecting a map and repointing an entry are separate steps. Use
    `--rom` to stack this on top of a ROM that already has a map injected,
    rather than starting from the stock cartridge each time.

    python patch_scenario.py rom.z64 --list
    python patch_scenario.py rom.z64 --entry 2 --map-id 0x19 --opponents 3 \
        -o out.z64
"""

from __future__ import annotations

import argparse
import struct
import sys

import n64crc
from extract_sc64_maps import (BoltArchive, load_rom, looks_like_chk,
                               parse_map)

# File offsets in the deswapped (z64) image.
LABEL_PTRS = 0x0D16BC
N_LABEL_PTRS = 11
RECORDS = 0x0D16E8
N_RECORDS = 10

# map_id is relative to the first melee map rather than absolute.
MELEE_BASE = 60

# Derived from the pointers themselves: 0x800D09F4 is the "Setup Custom"
# string, which lives at file 0x0D15F4. Matches the community's static-segment
# rule, file = RAM - 0x80000000 + 0xC00.
RAM_TO_FILE = 0x0D15F4 - 0x800D09F4


def cstr(rom: bytes, off: int, limit: int = 64) -> str:
    end = off
    while end < off + limit and end < len(rom) and rom[end]:
        end += 1
    return rom[off:end].decode("ascii", "replace")


def map_names(rom: bytes) -> dict[int, str]:
    """Scenario name of every map in the cartridge, by map index."""
    arc = BoltArchive(rom)
    names: dict[int, str] = {}
    for e in arc.entries():
        if not e.path.startswith("008/"):
            continue
        n = int(e.path.split("/")[1], 16)
        if not (8 <= n <= 0x67):
            continue
        try:
            chk = arc.read(e)
        except Exception:
            continue
        if looks_like_chk(chk):
            names[n - 8] = parse_map(e.path, chk).name
    return names


def read_table(rom: bytes):
    """[(label, map_id, opponents, map_index)] for the ten records."""
    labels = []
    for i in range(N_LABEL_PTRS):
        ptr = struct.unpack_from(">I", rom, LABEL_PTRS + i * 4)[0]
        labels.append(cstr(rom, ptr + RAM_TO_FILE))
    out = []
    for i in range(N_RECORDS):
        mid, opp = rom[RECORDS + i * 2], rom[RECORDS + i * 2 + 1]
        out.append((labels[i + 1], mid, opp, mid + MELEE_BASE))
    return labels[0], out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("rom", help="ROM to read, or to patch with --entry")
    ap.add_argument("--list", action="store_true",
                    help="show the table and exit")
    ap.add_argument("--entry", type=int,
                    help="list position to repoint; 1..10 (0 is Setup Custom)")
    ap.add_argument("--map-id", type=lambda s: int(s, 0),
                    help=f"new map id; map index = id + {MELEE_BASE}")
    ap.add_argument("--map-index", type=lambda s: int(s, 0),
                    help="new map index, given directly instead of --map-id")
    ap.add_argument("--opponents", type=lambda s: int(s, 0))
    ap.add_argument("-o", "--out", help="output ROM")
    a = ap.parse_args(argv)

    rom = bytearray(load_rom(a.rom))
    names = map_names(bytes(rom))
    first, table = read_table(bytes(rom))

    if a.list or a.entry is None:
        print(f"{'#':>2}  {'map_id':>6} {'opp':>3} {'index':>5} {'BOLT':>8}  "
              f"{'label':22} scenario")
        print(f"{0:>2}  {'-':>6} {'-':>3} {'-':>5} {'-':>8}  {first:22} -")
        for i, (label, mid, opp, idx) in enumerate(table, 1):
            real = names.get(idx)
            print(f"{i:>2}  {mid:#06x} {opp:>3} {idx:>5} "
                  f"{'008/%03X' % (idx + 8):>8}  {label.strip():22} "
                  f"{real if real else '(no such map)'}")
        if a.entry is None and not a.list:
            print("\nnothing to do; pass --entry to edit")
        return 0

    if not 1 <= a.entry <= N_RECORDS:
        sys.exit(f"--entry must be 1..{N_RECORDS}; 0 is Setup Custom and has "
                 f"no record")
    if a.map_id is None and a.map_index is None and a.opponents is None:
        sys.exit("nothing to change: pass --map-id/--map-index and/or "
                 "--opponents")
    if not a.out:
        sys.exit("--out is required when editing")

    mid = a.map_id
    if a.map_index is not None:
        mid = a.map_index - MELEE_BASE
        if not 0 <= mid <= 0xFF:
            sys.exit(f"map index {a.map_index} is not reachable from this "
                     f"table; expressible range is {MELEE_BASE}.."
                     f"{MELEE_BASE + 255}")

    rec = RECORDS + (a.entry - 1) * 2
    old_id, old_opp = rom[rec], rom[rec + 1]
    new_id = old_id if mid is None else mid & 0xFF
    new_opp = old_opp if a.opponents is None else a.opponents & 0xFF

    print(f"entry {a.entry}: {table[a.entry - 1][0].strip()!r}")
    print(f"  map_id    {old_id:#04x} -> {new_id:#04x}  "
          f"(index {old_id + MELEE_BASE} -> {new_id + MELEE_BASE}, "
          f"{names.get(new_id + MELEE_BASE, '(no such map)')})")
    print(f"  opponents {old_opp} -> {new_opp}")
    if new_id + MELEE_BASE not in names:
        print("  warning: that index holds no readable scenario")

    rom[rec] = new_id
    rom[rec + 1] = new_opp

    # Mandatory: this table is inside the boot checksum window.
    variant = n64crc.detect(load_rom(a.rom))
    if variant is None:
        sys.exit("error: cannot identify the CIC variant -- if this ROM was "
                 "already patched inside 0x1000..0x101000, start from a clean "
                 "one")
    c1, c2 = n64crc.fix(rom, variant)
    print(f"  boot checksum (CIC {variant}) repaired -> {c1:#010x} {c2:#010x}")

    with open(a.out, "wb") as fh:
        fh.write(rom)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
