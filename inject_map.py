"""Swap a PC map into a StarCraft 64 ROM, stored uncompressed.

The trick that makes this tractable: BOLT entries carry a FLAG_UNCOMPRESSED bit
(0x08), so a replacement does not need an LZSS encoder -- store the CHK raw and
set the flag. Cost is size, which the ROM's ~313 KiB of tail padding absorbs
for one map.

Nothing is written in place. The original data stays where it is; the new CHK
is appended into the tail padding and the directory entry is repointed at it.
The N64 CIC boot checksum covers only 0x1000..0x101000, and BOLT lives at
0x12CA10, so none of this disturbs it.

Usage:
    python inject_map.py --target 008/049 --map "path/to/map.scx" -o out.z64
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

from extract_sc64_maps import (BOLT_ENTRY_SIZE, BOLT_HEADER_SIZE, BoltArchive,
                               load_rom, looks_like_chk, parse_map)
from verify_maps import MpqReader
from sc64 import find_rom

FLAG_UNCOMPRESSED = 0x08
ALIGN = 16


def dir_entry_offset(arc: BoltArchive, path: str) -> int:
    """BOLT-relative offset of the 16-byte entry record for `path`."""
    parts = path.split("/")
    table = BOLT_HEADER_SIZE
    count = arc.num_entries or 256
    for depth, part in enumerate(parts):
        index = int(part, 16)
        if index >= count:
            raise ValueError(f"{path}: index {part} beyond {count} entries")
        rec = table + index * BOLT_ENTRY_SIZE
        if depth == len(parts) - 1:
            return rec
        entry = arc._entry(part, rec)
        if entry.file_hash != 0:
            raise ValueError(f"{path}: {part} is a file, not a directory")
        table, count = entry.offset, (entry.file_type or 256)
    raise ValueError(path)


def tail_free_start(rom: bytes) -> int:
    """Absolute offset where the ROM's trailing padding begins."""
    pos = len(rom)
    while pos > 0 and rom[pos - 1] in (0x00, 0xFF):
        pos -= 1
    return pos


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Swap a PC map into an SC64 ROM.")
    ap.add_argument("--rom", default=None,
                    help="ROM to patch; found automatically if omitted")
    ap.add_argument("--target", required=True, help="BOLT path to replace, e.g. 008/049")
    ap.add_argument("--map", required=True, help=".scm/.scx whose CHK to inject")
    ap.add_argument("-o", "--out", required=True, help="output .z64")
    ap.add_argument("--compress", action="store_true",
                    help="LZSS-compress the payload instead of storing it raw. "
                         "Needs the bolt-lzss package (pip install bolt-lzss).")
    ap.add_argument("--level", type=int, default=2,
                    help="bolt-lzss level: 0 store, 1 greedy, 2 lazy, 3 optimal")
    args = ap.parse_args(argv)

    rom_path = find_rom(args.rom)
    if rom_path is None:
        print('no ROM found; pass --rom', file=sys.stderr)
        return 1
    rom = bytearray(load_rom(rom_path))
    arc = BoltArchive(bytes(rom))
    print(f"ROM {len(rom):,} bytes, BOLT at {arc.base:#x}")

    # Most competitive ladder maps set MPQ_FILE_ENCRYPTED on the scenario --
    # 115 of the 119 here do -- and MpqReader refuses those outright. ladder
    # .read_chk falls back to the key derivation already in mpq_keycrack.
    try:
        from pc_maps import read_chk
        chk = read_chk(args.map)
    except Exception:
        chk = MpqReader(args.map).read("staredit\\scenario.chk")
    if not chk or not looks_like_chk(chk):
        print(f"error: no usable scenario in {args.map!r}", file=sys.stderr)
        return 1
    info = parse_map(os.path.basename(args.map), chk)
    print(f"payload : {os.path.basename(args.map)}  {len(chk):,} bytes  "
          f"{info.width}x{info.height} {info.tileset_name} VER={info.version}")

    rec = dir_entry_offset(arc, args.target)
    old = arc._entry(args.target, rec)
    old_chk = arc.read(old)
    old_info = parse_map(args.target, old_chk) if looks_like_chk(old_chk) else None
    print(f"target  : {args.target} entry at BOLT+{rec:#x} "
          f"(abs {arc.base + rec:#x})")
    print(f"          was {old.size:,} bytes plain, flags {old.flags:#04x}"
          + (f", {old_info.name}" if old_info else ""))

    # Storing raw is simple but expensive: the ROM's tail padding is only
    # ~313 KiB, and a single 128x128 scenario can be 370 KiB on its own.
    # Compressing costs nothing at runtime -- the engine already decompresses
    # every stock entry -- and the cartridge's own maps store at 0.06..0.16,
    # so the ceiling stops binding.
    if args.compress:
        try:
            import bolt_lzss
        except ImportError:
            print("error: --compress needs bolt-lzss (pip install bolt-lzss)",
                  file=sys.stderr)
            return 1
        payload = bolt_lzss.encode(chk, args.level)
        # Never write a stream that does not decode back to the input.
        if bolt_lzss.decode(payload, len(chk)) != chk:
            print("error: compression round trip failed, refusing to write",
                  file=sys.stderr)
            return 1
        new_flags = old.flags & ~FLAG_UNCOMPRESSED
        print(f"packed  : {len(chk):,} -> {len(payload):,} bytes "
              f"({len(payload) / len(chk):.3f}) at level {args.level}")
    else:
        payload = chk
        new_flags = old.flags | FLAG_UNCOMPRESSED

    free = tail_free_start(bytes(rom))
    dest = (free + ALIGN - 1) & ~(ALIGN - 1)
    room = len(rom) - dest
    print(f"free    : padding starts {free:#x}, writing at {dest:#x}, "
          f"{room:,} bytes available")
    if len(payload) > room:
        print(f"error: payload needs {len(payload):,} bytes, only {room:,} free"
              + ("" if args.compress else " -- try --compress"),
              file=sys.stderr)
        return 1

    # Append the payload, then repoint the entry at it. The size field is the
    # DECOMPRESSED length either way; the stored length is implied by the
    # stream itself.
    rom[dest:dest + len(payload)] = payload
    abs_rec = arc.base + rec
    rom[abs_rec] = new_flags & 0xFF
    struct.pack_into(">I", rom, abs_rec + 4, len(chk))          # size
    struct.pack_into(">I", rom, abs_rec + 8, dest - arc.base)   # offset
    print(f"patched : flags {old.flags:#04x} -> {new_flags:#04x}, "
          f"size {len(chk):,}, stored {len(payload):,}, "
          f"offset BOLT+{dest - arc.base:#x}")

    with open(args.out, "wb") as fh:
        fh.write(rom)

    # Read it back through the normal archive walk -- the real test.
    back = BoltArchive(bytes(rom))
    entry = next(e for e in back.entries() if e.path == args.target)
    got = back.read(entry)
    ok = got == chk and looks_like_chk(got)
    print(f"\nread-back via BoltArchive: {len(got):,} bytes, "
          f"identical={got == chk}, valid CHK={looks_like_chk(got)}")
    if ok:
        new_info = parse_map(args.target, got)
        print(f"  now reads as: {new_info.width}x{new_info.height} "
              f"{new_info.tileset_name} VER={new_info.version} "
              f"'{new_info.name}'")
    # Everything else must be untouched.
    others = sum(1 for e in back.entries() if e.path != args.target)
    print(f"  other entries still walkable: {others}")
    print(f"\nwrote {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
