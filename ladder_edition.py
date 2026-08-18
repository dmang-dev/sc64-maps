"""Build a StarCraft 64 ROM whose melee list is the 2017 Frontier League maps.

Everything this needs is now proven separately:

  * PC ladder maps load and play in the melee slots (campaign slots apply
    campaign mission-end logic and resolve to an instant Victory; melee slots
    do not).
  * bolt-lzss 0.2.0 produces streams the engine accepts, so payloads compress
    to roughly a fifth and the ROM's ~313 KiB of tail padding stops binding.
  * The Scenario list is ten {map_id, opponents} records at 0x0D16E8, with
    map index = map_id + 60, and that table is patchable.
  * That table is inside the boot checksum window, so the header must be
    repaired or the ROM will not boot at all.

The one design decision worth stating: injection targets map indices 85-95,
which no Scenario list entry references. The stock melee maps at 60-84 are left
untouched, so this ADDS a ladder lineup rather than destroying the cartridge's
own. Nothing is overwritten that the game otherwise shows you.

No ROM is distributed by this script and none can be -- it reads a cartridge
you supply and writes a patched copy locally.
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path


import bolt_lzss
import n64crc
from extract_sc64_maps import (BOLT_ENTRY_SIZE, BOLT_HEADER_SIZE, BoltArchive,
                               chk_sections, load_rom, looks_like_chk,
                               parse_map)
from inject_map import dir_entry_offset, tail_free_start
from pc_maps import read_chk
from sc64 import find_rom

FLAG_UNCOMPRESSED = 0x08
ALIGN = 16
MELEE_BASE = 60
RECORDS = 0x0D16E8
FREE_SLOTS = list(range(85, 96))        # indices no list entry points at

# With --expand the cartridge is doubled to 64 MiB and every payload goes in
# the new half, so the ~313 KiB of tail padding stops being the budget and the
# whole melee range becomes usable. Verified on the engine: a map whose stream
# sits at file 0x3000000, 16 MiB past the original end, loads and plays. The
# N64 cartridge window runs to roughly 64 MiB, BOLT offsets are u32 relative to
# a base at 0x12CA10, and the boot checksum only covers 0x1000..0x101000, so
# nothing about growing the image disturbs what is already there.
#
# 60 is the first melee map. Indices above 95 are NOT usable: they map to BOLT
# entries 008/068 and beyond, which hold other data, and the selector's window
# is contiguous from 60.
MELEE_SLOTS = list(range(60, 96))

# The two-player melee map selector walks a bounded range starting at map index
# 60. Its length is an immediate in the menu setup code:
#
#     RAM 0x800D9F78 / file 0x0DAB78    addiu a2, zero, 27
#
# 27 covers indices 60..86, so a map installed at 87 or beyond exists, loads
# through the Scenario list, and is simply unreachable in a 1v1 game. Widening
# the immediate to (last_index - 60 + 1) brings the whole lineup into the
# selector. Confirmed by patching it and watching the list grow.
#
# This is inside the boot checksum window, so the header must be repaired --
# which this script does anyway for the Scenario table.
LIST_LEN_OFFSET = 0x0DAB78
LIST_LEN_EXPECT = 0x2406001B          # addiu a2, zero, 0x1b


# Every section tag StarCraft defines. Anything else in a scenario is padding
# a protector added: competitive maps are routinely spammed with sections
# carrying random four-byte tags, which PC StarCraft ignores and plays anyway.
#
# The console does not. Measured across the 2017 Frontier League set, the maps
# that fail to load on the N64 are exactly the ones carrying a lot of junk --
# 24, 28 and 36 junk sections, against 0 or 1 for every map that works. It is
# a count problem rather than a size one; the junk is only ~1 KiB of bytes.
KNOWN_TAGS = {
    b"TYPE", b"VER ", b"IVER", b"IVE2", b"VCOD", b"IOWN", b"OWNR", b"ERA ",
    b"DIM ", b"SIDE", b"MTXM", b"PUNI", b"UPGR", b"PTEC", b"UNIT", b"ISOM",
    b"TILE", b"DD2 ", b"THG2", b"MASK", b"STR ", b"UPRP", b"UPUS", b"MRGN",
    b"TRIG", b"MBRF", b"SPRP", b"FORC", b"WAV ", b"UNIS", b"UPGS", b"TECS",
    b"SWNM", b"COLR", b"PUPx", b"PTEx", b"UNIx", b"UPGx", b"TECx", b"CRGB",
    b"STRx",
}


def collapse_duplicates(chk: bytes) -> tuple[bytes, int]:
    """Resolve repeated sections the way StarCraft does, into one each.

    A CHK may carry the same tag more than once, and StarCraft applies them in
    order with each overwriting from the START of that section's data. Map
    protectors abuse it: the three ladder maps that run with no terrain each
    carry THREE MTXM sections, and all four that work carry exactly one.

    The console cannot cope. Its MTXM handler (0x8002DADC, reached from the
    version-205 dispatch table) copies the section to one fixed tile buffer,
    byte-swaps it in place, and re-runs the terrain rebuild -- every time it is
    called. A second MTXM therefore re-swaps and rebuilds over a buffer that is
    already converted, and the result is no terrain at all while the rest of
    the map loads and plays.

    Applying the overrides here means the console sees one already-resolved
    section and lands on the same tiles StarCraft would have drawn.
    """
    order: list[bytes] = []
    merged: dict[bytes, bytearray] = {}
    dupes = 0
    for tag, payload in chk_sections(chk):
        if tag not in merged:
            order.append(tag)
            merged[tag] = bytearray(payload)
            continue
        dupes += 1
        buf = merged[tag]
        if len(payload) > len(buf):
            buf.extend(b"\0" * (len(payload) - len(buf)))
        buf[:len(payload)] = payload           # later wins, from offset 0
    out = bytearray()
    for tag in order:
        out += tag + struct.pack("<i", len(merged[tag])) + bytes(merged[tag])
    return bytes(out), dupes


def strip_junk(chk: bytes) -> tuple[bytes, int]:
    """Drop sections StarCraft does not define. Returns (chk, n_dropped).

    Order is preserved and duplicates are kept, because a CHK's later section
    of a given tag legitimately overrides an earlier one -- collapsing those
    would change the map rather than clean it.
    """
    out = bytearray()
    dropped = 0
    for tag, payload in chk_sections(chk):
        if tag in KNOWN_TAGS:
            out += tag + struct.pack("<i", len(payload)) + payload
        else:
            dropped += 1
    return bytes(out), dropped

ap = argparse.ArgumentParser()
ap.add_argument("--rom", default=None,
                    help="ROM to patch; found automatically if omitted")
ap.add_argument("-o", "--out", default="sc64_ladder_edition.z64")
ap.add_argument("--level", type=int, default=3)
ap.add_argument("--maps", required=True,
                help="directory of .scm/.scx maps to install")
ap.add_argument("--recursive", action="store_true",
                help="search --maps recursively and drop duplicate scenarios; "
                     "the seasons in a ladder folder repeat the pool heavily")
ap.add_argument("--expand", action="store_true",
                help="double the ROM to 64 MiB and use the whole melee range, "
                     "replacing the cartridge's own melee maps")
a = ap.parse_args()

rom_path = find_rom(a.rom)
if rom_path is None:
    sys.exit('no ROM found; pass --rom')
rom = bytearray(load_rom(rom_path))
variant = n64crc.detect(bytes(rom))
if variant is None:
    sys.exit("error: unrecognised ROM -- checksum matches no CIC variant")

slots = MELEE_SLOTS if a.expand else FREE_SLOTS
if a.expand:
    rom.extend(bytes(len(rom)))
    print(f"expanded to {len(rom):,} bytes ({len(rom) // 2**20} MiB)")

sources = sorted(Path(a.maps).glob("**/*.sc*" if a.recursive else "*.sc*"))
if a.recursive:
    # Deduplicate on the NORMALISED scenario, not the file: the same map
    # reappears season after season with different protector padding, so
    # hashing the raw file would keep all of them.
    seen, unique = set(), []
    for src in sources:
        try:
            key = hashlib.sha256(collapse_duplicates(strip_junk(read_chk(src))[0])[0]).hexdigest()
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(src)
    print(f"{len(sources)} files -> {len(unique)} unique scenarios")
    sources = unique
if not sources:
    sys.exit(f"no maps found in {a.maps}")
sources = sources[:len(slots)]

print(f"ROM {Path(rom_path).name}  CIC {variant}")
print(f"installing {len(sources)} maps into indices "
      f"{slots[0]}..{slots[len(sources) - 1]}\n")

installed = []
for src, idx in zip(sources, slots):
    chk = read_chk(src)
    chk, dropped = strip_junk(chk)
    chk, dupes = collapse_duplicates(chk)
    info = parse_map(src.name, chk)
    sec = {t: p for t, p in chk_sections(chk)}
    humans = sum(1 for b in sec.get(b"OWNR", b"") if b == 6)

    packed = bolt_lzss.encode(chk, a.level)
    if bolt_lzss.decode(packed, len(chk)) != chk:
        sys.exit(f"error: {src.name} failed its compression round trip")

    # Re-read the archive each time: the previous write moved where the tail
    # padding begins, and the next payload has to land after it.
    arc = BoltArchive(bytes(rom))
    slot = f"008/{idx + 8:03X}"
    rec = dir_entry_offset(arc, slot)
    old = arc._entry(slot, rec)
    dest = (tail_free_start(bytes(rom)) + ALIGN - 1) & ~(ALIGN - 1)
    if dest + len(packed) > len(rom):
        sys.exit(f"error: out of tail padding at {src.name}")

    rom[dest:dest + len(packed)] = packed
    abs_rec = arc.base + rec
    rom[abs_rec] = old.flags & ~FLAG_UNCOMPRESSED       # compressed
    struct.pack_into(">I", rom, abs_rec + 4, len(chk))  # decompressed size
    struct.pack_into(">I", rom, abs_rec + 8, dest - arc.base)

    installed.append((src.name, idx, info, humans, len(chk), len(packed)))
    print(f"  {src.name[:30]:30} -> index {idx} ({slot})  "
          f"{info.width}x{info.height} {info.tileset_name:14} "
          f"{len(chk):8,} -> {len(packed):7,} ({len(packed)/len(chk):.3f})"
          + (f"  [-{dropped} junk]" if dropped else "")
          + (f"  [-{dupes} dup]" if dupes else ""))

# Repoint the list. Entry 0 is Setup Custom and has no record; entries run 1..10
# but the tenth is dead data the game never renders, so 9 are usable.
print()
for n, (name, idx, info, humans, _, _) in enumerate(installed[:9], start=1):
    rec = RECORDS + (n - 1) * 2
    opponents = max(1, min(humans - 1, 4))
    rom[rec] = (idx - MELEE_BASE) & 0xFF
    rom[rec + 1] = opponents
    print(f"  list entry {n}: -> index {idx}  1v{opponents}  {info.name[:34]}")

# Widen the two-player selector so every installed map is reachable in 1v1.
last_index = installed[-1][1] if installed else 86
want_len = max(0x1B, last_index - MELEE_BASE + 1)
have = struct.unpack_from(">I", rom, LIST_LEN_OFFSET)[0]
if have != LIST_LEN_EXPECT:
    print(f"  warning: {LIST_LEN_OFFSET:#08x} is {have:#010x}, expected "
          f"{LIST_LEN_EXPECT:#010x} -- leaving the 1v1 list length alone")
else:
    struct.pack_into(">I", rom, LIST_LEN_OFFSET,
                     (have & 0xFFFF0000) | want_len)
    print(f"  1v1 map list: {have & 0xFFFF} -> {want_len} entries "
          f"(indices {MELEE_BASE}..{MELEE_BASE + want_len - 1})")

c1, c2 = n64crc.fix(rom, variant)
print(f"\nboot checksum repaired -> {c1:#010x} {c2:#010x}")

out = Path(a.out)
out.write_bytes(rom)

# Read every installed map back through the ordinary archive walk.
back = BoltArchive(bytes(rom))
bad = 0
for name, idx, info, _, plain, _ in installed:
    slot = f"008/{idx + 8:03X}"
    got = back.read(next(e for e in back.entries() if e.path == slot))
    if len(got) != plain or not looks_like_chk(got):
        # Protected maps are not looks_like_chk clean; check the sections.
        tags = {t for t, _ in chk_sections(got)}
        if len(got) != plain or not {b"VER ", b"DIM ", b"MTXM"} <= tags:
            print(f"  READ-BACK FAILED: {name}")
            bad += 1

print(f"read-back through BoltArchive: {len(installed) - bad}/{len(installed)} ok")
print(f"other entries still walkable  : "
      f"{sum(1 for e in back.entries() if not e.path.startswith('008/0'))}")
print(f"\nwrote {out}  ({out.stat().st_size:,} bytes)")
