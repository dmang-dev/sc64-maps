#!/usr/bin/env python3
"""
extract_sc64_maps.py -- extract the StarCraft 64 scenarios from a ROM you own
and repackage them as PC-StarCraft-playable .scm / .scx map files.

    python extract_sc64_maps.py "StarCraft 64 (USA).n64" -o maps/

No third-party packages required (standard library only).

This tool ships NO game data. It reads a StarCraft 64 ROM that you supply and
writes maps derived from it. Those maps are Blizzard's copyrighted work -- keep
them for yourself, do not redistribute them.

Pipeline
--------
  ROM (.z64/.v64/.n64)
    -> normalise byte order to big-endian z64
    -> locate the embedded BOLT archive (Mass Media's container format)
    -> walk the BOLT directory tree, LZSS-decompress each entry
    -> keep entries that are valid StarCraft CHK scenarios
    -> wrap each CHK in a minimal MPQ archive as staredit\\scenario.chk
    -> .scm (StarCraft) or .scx (Brood War) depending on the map's own version

The BOLT container walk and the N64 LZSS decompressor are a Python rewrite of
Adam Heinermann's BOLTextract (GPL-3.0); see reference/ for the original C++.
Everything else (CHK parsing, MPQ writer) is original to this project.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# ROM byte order
# --------------------------------------------------------------------------
# N64 dumps come in three interleavings of the same bytes. Everything below
# assumes native big-endian (z64), so normalise up front.

Z64_MAGIC = bytes.fromhex("80371240")  # big endian, native
V64_MAGIC = bytes.fromhex("37804012")  # byte-swapped pairs
N64_MAGIC = bytes.fromhex("40123780")  # 32-bit little endian


def load_rom(path: str) -> bytes:
    """Read a ROM and return it in z64 (big-endian) order.

    The file extension is ignored -- plenty of dumps are mislabelled, including
    the common "StarCraft 64 (USA).n64" which actually holds v64 data.
    """
    with open(path, "rb") as fh:
        raw = fh.read()

    if len(raw) < 0x40:
        raise ValueError(f"{path}: too small to be an N64 ROM")

    magic = raw[:4]
    if magic == Z64_MAGIC:
        data = raw
    elif magic == V64_MAGIC:
        buf = bytearray(raw)
        buf[0::2], buf[1::2] = raw[1::2], raw[0::2]
        data = bytes(buf)
    elif magic == N64_MAGIC:
        buf = bytearray(len(raw))
        buf[0::4], buf[1::4], buf[2::4], buf[3::4] = (
            raw[3::4], raw[2::4], raw[1::4], raw[0::4],
        )
        data = bytes(buf)
    else:
        raise ValueError(
            f"{path}: not an N64 ROM (header {magic.hex()}); expected one of "
            f"{Z64_MAGIC.hex()} / {V64_MAGIC.hex()} / {N64_MAGIC.hex()}"
        )

    return data


def rom_internal_name(data: bytes) -> str:
    return data[0x20:0x34].decode("ascii", "replace").strip()


def rom_cart_id(data: bytes) -> str:
    return data[0x3B:0x3F].decode("ascii", "replace")


# --------------------------------------------------------------------------
# BOLT archive
# --------------------------------------------------------------------------
# BOLT is Mass Media's container format. Layout (big endian on N64):
#
#   header  'BOLT', hh, mm, ss, ms, month, day, year-1900, num_entries, u32 end
#   entry   u8 flags, u8 unk1, u8 unk2, u8 file_type,
#           u32 uncompressed_size, u32 data_offset, u32 file_hash
#
# All offsets are relative to the start of the 'BOLT' magic, not the ROM.
# An entry with file_hash == 0 is a directory: file_type is its child count
# (0 means 256) and data_offset points at the child entry array.

BOLT_HEADER_SIZE = 16
BOLT_ENTRY_SIZE = 16
FLAG_UNCOMPRESSED = 0x08


@dataclass
class BoltEntry:
    path: str
    flags: int
    file_type: int
    size: int
    offset: int
    file_hash: int


class BoltArchive:
    def __init__(self, rom: bytes):
        self.rom = rom
        start = rom.find(b"BOLT")
        if start < 0:
            start = rom.find(b"bolt")
        if start < 0:
            raise ValueError(
                "no BOLT archive found in this ROM -- is it really StarCraft 64?"
            )
        self.base = start

        hdr = rom[start:start + BOLT_HEADER_SIZE]
        (self.hour, self.minute, self.second, self.millis,
         self.month, self.day, year, self.num_entries) = hdr[4:12]
        self.year = 1900 + year
        self.end_offset = struct.unpack_from(">I", hdr, 12)[0]

    @property
    def build_stamp(self) -> str:
        return (f"{self.year:04d}-{self.month:02d}-{self.day:02d} "
                f"{self.hour:02d}:{self.minute:02d}:{self.second:02d}")

    def _entry(self, path: str, offset: int) -> BoltEntry:
        b = self.rom[self.base + offset:self.base + offset + BOLT_ENTRY_SIZE]
        flags, _unk1, _unk2, file_type = b[0], b[1], b[2], b[3]
        size, data_off, file_hash = struct.unpack_from(">III", b, 4)
        return BoltEntry(path, flags, file_type, size, data_off, file_hash)

    def entries(self):
        """Yield every leaf (file) entry, depth-first."""
        count = self.num_entries or 256
        yield from self._walk("", BOLT_HEADER_SIZE, count)

    def _walk(self, prefix: str, table_offset: int, count: int, depth: int = 0):
        if depth > 8:  # the real archive is 2 deep; this is a corruption guard
            return
        if count == 0:
            count = 256
        for i in range(count):
            entry = self._entry(f"{prefix}{i:03X}", table_offset + i * BOLT_ENTRY_SIZE)
            if entry.file_hash == 0:
                yield from self._walk(
                    entry.path + "/", entry.offset, entry.file_type, depth + 1
                )
            else:
                yield entry

    # -- decompression ----------------------------------------------------
    def read(self, entry: BoltEntry, limit: int | None = None) -> bytes:
        """Return the entry's contents, decompressing if needed.

        `limit` stops early once that many bytes are available -- used to sniff
        a file's magic without paying for a full decompress.
        """
        if entry.flags & FLAG_UNCOMPRESSED:
            start = self.base + entry.offset
            n = entry.size if limit is None else min(entry.size, limit)
            return self.rom[start:start + n]
        want = entry.size if limit is None else min(entry.size, limit)
        return self._decompress(entry.offset, entry.size, want)

    def _decompress(self, offset: int, expected: int, want: int) -> bytes:
        """LZSS variant used by the N64/GBA-era BOLT archives.

        Control byte layout:
          0xxxxxxx  back-reference; low nibble extends the offset, high nibble
                    the run length, both biased by the number of control bytes
                    consumed since the last emit
          10xxxxxx  literal run of (ext_run << 4 | low nibble) + 1 bytes
          11xxxxxx  extend the pending offset accumulator by 6 bits
          101xxxxx  extend the pending run accumulator by 5 bits
          1001xxxx  extend both accumulators by 2 bits each

        Ported from BOLTextract's n64.cpp (GPL-3.0).
        """
        rom = self.rom
        pos = self.base + offset
        out = bytearray()
        op_count = 0
        ext_offset = 0
        ext_run = 0

        while len(out) < want:
            byte = rom[pos]
            pos += 1
            op_count += 1

            if byte & 0x80:
                if byte & 0x40:            # extend offset
                    ext_offset = (ext_offset << 6) | (byte & 0x3F)
                elif byte & 0x20:          # extend run length
                    ext_run = (ext_run << 5) | (byte & 0x1F)
                elif byte & 0x10:          # extend both
                    ext_run = (ext_run << 2) | (byte & 0b0011)
                    ext_offset = (ext_offset << 2) | ((byte & 0b1100) >> 2)
                else:                      # literal run
                    run = ((ext_run << 4) | (byte & 0xF)) + 1
                    out += rom[pos:pos + run]
                    pos += run
                    op_count = ext_offset = ext_run = 0
            else:                          # back-reference
                if not out:
                    raise ValueError(
                        f"BOLT+{offset:#x}: back-reference before any output"
                    )
                rel = ((ext_offset << 4) | (byte & 0xF)) + 1
                run = ((ext_run << 3) | (byte >> 4)) + op_count + 1
                if rel > len(out):
                    raise ValueError(
                        f"BOLT+{offset:#x}: back-reference {rel} exceeds "
                        f"{len(out)} bytes of output"
                    )
                for _ in range(run):
                    out.append(out[-rel])
                op_count = ext_offset = ext_run = 0

        if want == expected and len(out) != expected:
            raise ValueError(
                f"BOLT+{offset:#x}: got {len(out)} bytes, expected {expected}"
            )
        return bytes(out)


# --------------------------------------------------------------------------
# CHK scenario
# --------------------------------------------------------------------------
# A CHK is a flat sequence of  [4-byte tag][int32 little-endian size][data].
# StarCraft 64 stores these byte-for-byte identical to the PC format, which is
# why the maps drop straight into the PC game once they are back inside an MPQ.

CHK_FIRST_TAGS = (b"TYPE", b"VER ", b"IVER")

TILESETS = {
    0: "Badlands", 1: "Space Platform", 2: "Installation", 3: "Ashworld",
    4: "Jungle", 5: "Desert", 6: "Arctic", 7: "Twilight",
}


def chk_sections(data: bytes):
    """Yield (tag, payload). Assumes `data` already passed looks_like_chk."""
    pos = 0
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        size = struct.unpack_from("<i", data, pos + 4)[0]
        yield tag, data[pos + 8:pos + 8 + size]
        pos += 8 + size


def looks_like_chk(data: bytes) -> bool:
    """True if `data` is exactly a well-formed chain of CHK sections."""
    if len(data) < 8 or data[:4] not in CHK_FIRST_TAGS:
        return False
    pos = 0
    count = 0
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        if not all(0x20 <= c < 0x7F for c in tag):
            return False
        size = struct.unpack_from("<i", data, pos + 4)[0]
        if size < 0:
            return False
        pos += 8 + size
        count += 1
        if pos > len(data):
            return False
    return pos == len(data) and count >= 10


@dataclass
class MapInfo:
    bolt_path: str
    name: str = ""
    description: str = ""
    width: int = 0
    height: int = 0
    tileset: int = -1
    version: int = 0
    type_tag: str = ""
    players: int = 0
    size: int = 0
    tags: set = field(default_factory=set)

    @property
    def is_broodwar(self) -> bool:
        # VER 205+ is Brood War; the TYPE tag agrees ('RAWB' vs 'RAWS').
        return self.version >= 205 or self.type_tag == "RAWB"

    @property
    def extension(self) -> str:
        return ".scx" if self.is_broodwar else ".scm"

    @property
    def tileset_name(self) -> str:
        return TILESETS.get(self.tileset & 7, "?")

    @property
    def edition(self) -> str:
        if self.version >= 205:
            return "Brood War"
        if self.version >= 63:
            return "StarCraft (hybrid)"
        return "StarCraft"


def _chk_string(strtab: bytes | None, index: int) -> str:
    """Look up a 1-based index in a CHK STR section."""
    if not strtab or index == 0 or len(strtab) < 2:
        return ""
    count = struct.unpack_from("<H", strtab, 0)[0]
    if index > count or 2 + index * 2 > len(strtab):
        return ""
    offset = struct.unpack_from("<H", strtab, 2 + (index - 1) * 2)[0]
    if offset >= len(strtab):
        return ""
    end = strtab.find(b"\x00", offset)
    raw = strtab[offset:end if end >= 0 else len(strtab)]
    return raw.decode("cp1252", "replace").strip()


def parse_map(bolt_path: str, data: bytes) -> MapInfo:
    info = MapInfo(bolt_path=bolt_path, size=len(data))
    latest: dict[bytes, bytes] = {}
    for tag, payload in chk_sections(data):
        info.tags.add(tag.decode("latin1"))
        latest[tag] = payload  # later sections override earlier ones

    if b"VER " in latest and len(latest[b"VER "]) >= 2:
        info.version = struct.unpack_from("<H", latest[b"VER "], 0)[0]
    if b"TYPE" in latest:
        info.type_tag = latest[b"TYPE"][:4].decode("latin1", "replace")
    if b"DIM " in latest and len(latest[b"DIM "]) >= 4:
        info.width, info.height = struct.unpack_from("<HH", latest[b"DIM "], 0)
    if b"ERA " in latest and len(latest[b"ERA "]) >= 2:
        info.tileset = struct.unpack_from("<H", latest[b"ERA "], 0)[0]
    if b"OWNR" in latest:
        # 3 = rescuable, 5 = computer, 6 = human, 7 = neutral
        info.players = sum(1 for slot in latest[b"OWNR"][:8] if slot in (5, 6))

    strtab = latest.get(b"STR ")
    if b"SPRP" in latest and len(latest[b"SPRP"]) >= 4:
        name_idx, desc_idx = struct.unpack_from("<HH", latest[b"SPRP"], 0)
        info.name = _chk_string(strtab, name_idx)
        info.description = _chk_string(strtab, desc_idx)
    return info


# --------------------------------------------------------------------------
# Minimal MPQ writer
# --------------------------------------------------------------------------
# StarCraft maps are MPQ archives whose only required member is
# "staredit\scenario.chk". We write format version 0, unencrypted.
#
# Files are laid out the way every real map does it: a sector offset table
# followed by fixed-size sectors, with the MPQ_FILE_COMPRESS flag set. The
# sectors themselves are stored verbatim. That is legal and universally
# supported -- a reader compares each sector's stored length against its
# uncompressed length and only runs the decompressor when the stored form is
# actually shorter, which is the same path Storm takes for any sector that
# failed to compress. Doing it this way avoids shipping a compressor while
# still exercising the code path that every map in the wild uses.

MPQ_MAGIC = b"MPQ\x1a"
MPQ_HEADER_SIZE = 32
HASH_ENTRY_SIZE = 16
BLOCK_ENTRY_SIZE = 16
FILE_COMPRESS = 0x00000200
FILE_EXISTS = 0x80000000
HASH_ENTRY_FREE = 0xFFFFFFFF
SECTOR_SIZE_SHIFT = 3          # 512 << 3 == 4096, what StarEdit uses

HASH_TABLE_OFFSET, HASH_NAME_A, HASH_NAME_B, HASH_FILE_KEY = 0, 1, 2, 3


def _build_crypt_table() -> list[int]:
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(0x100):
        index = i
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            temp1 = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            temp2 = seed & 0xFFFF
            table[index] = temp1 | temp2
            index += 0x100
    return table


_CRYPT = _build_crypt_table()


def _hash_string(name: str, kind: int) -> int:
    seed1, seed2 = 0x7FED7FED, 0xEEEEEEEE
    for ch in name.upper():
        value = ord(ch)
        seed1 = _CRYPT[(kind << 8) + value] ^ ((seed1 + seed2) & 0xFFFFFFFF)
        seed2 = (value + seed1 + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return seed1


def _encrypt(words: list[int], key: int) -> bytes:
    seed = 0xEEEEEEEE
    out = bytearray()
    for value in words:
        seed = (seed + _CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        out += struct.pack("<I", value ^ ((key + seed) & 0xFFFFFFFF))
        key = (((~key & 0xFFFFFFFF) << 0x15) + 0x11111111 | (key >> 0x0B)) & 0xFFFFFFFF
        seed = (value + seed + (seed << 5) + 3) & 0xFFFFFFFF
    return bytes(out)


def _sectorize(data: bytes, sector_size: int) -> bytes:
    """Lay a file out as [sector offset table][sector 0][sector 1]...

    Offsets are relative to the start of the file's data. Sectors are stored
    verbatim, so each entry's length equals the amount of plain data it holds
    and no reader will try to decompress it.
    """
    sectors = [data[i:i + sector_size] for i in range(0, len(data), sector_size)]
    if not sectors:
        sectors = [b""]
    positions = [4 * (len(sectors) + 1)]
    for sector in sectors:
        positions.append(positions[-1] + len(sector))
    # StormLib treats a file as corrupt if any sector's *stored* length exceeds
    # the archive's sector size (SBaseCommon.cpp:1345-1351). Storing sectors
    # verbatim lands exactly on that limit with nothing to spare, so a future
    # change that prefixes a compression byte to an incompressible sector would
    # push it one byte over and silently produce unreadable maps.
    oversized = [i for i, s in enumerate(sectors) if len(s) > sector_size]
    if oversized:
        raise ValueError(
            f"sector {oversized[0]} is {len(sectors[oversized[0]])} bytes, "
            f"over the {sector_size}-byte limit StormLib enforces")

    table = struct.pack(f"<{len(positions)}I", *positions)
    return table + b"".join(sectors)


def build_mpq(files: dict[str, bytes], hash_table_size: int = 16) -> bytes:
    """Pack `files` (archive path -> bytes) into an MPQ v1 archive."""
    if hash_table_size & (hash_table_size - 1):
        raise ValueError("hash table size must be a power of two")
    if len(files) > hash_table_size // 2:
        raise ValueError("hash table too small for this many files")

    sector_size = 512 << SECTOR_SIZE_SHIFT
    block_table = []
    payload = bytearray()
    offset = MPQ_HEADER_SIZE
    for content in files.values():
        stored = _sectorize(content, sector_size)
        block_table.append((offset, len(stored), len(content),
                            FILE_EXISTS | FILE_COMPRESS))
        payload += stored
        offset += len(stored)

    # Hash table: linear probing from the name's table-offset hash.
    hash_table = [[HASH_ENTRY_FREE] * 4 for _ in range(hash_table_size)]
    for block_index, name in enumerate(files):
        start = _hash_string(name, HASH_TABLE_OFFSET) & (hash_table_size - 1)
        for probe in range(hash_table_size):
            slot = (start + probe) % hash_table_size
            if hash_table[slot][3] == HASH_ENTRY_FREE:
                hash_table[slot] = [
                    _hash_string(name, HASH_NAME_A),
                    _hash_string(name, HASH_NAME_B),
                    0,               # locale (neutral) + platform, packed below
                    block_index,
                ]
                break
        else:  # pragma: no cover - guarded by the size check above
            raise ValueError("hash table full")

    hash_words = []
    for name_a, name_b, locale_platform, block_index in hash_table:
        hash_words += [name_a, name_b, locale_platform, block_index]
    block_words = []
    for entry in block_table:
        block_words += list(entry)

    hash_bytes = _encrypt(hash_words, _hash_string("(hash table)", HASH_FILE_KEY))
    block_bytes = _encrypt(block_words, _hash_string("(block table)", HASH_FILE_KEY))

    hash_pos = MPQ_HEADER_SIZE + len(payload)
    block_pos = hash_pos + len(hash_bytes)
    archive_size = block_pos + len(block_bytes)

    header = struct.pack(
        "<4sIIHHIIII",
        MPQ_MAGIC,
        MPQ_HEADER_SIZE,
        archive_size,
        0,              # format version 1
        SECTOR_SIZE_SHIFT,
        hash_pos,
        block_pos,
        hash_table_size,
        len(block_table),
    )
    return bytes(header + payload + hash_bytes + block_bytes)


def build_map_file(chk: bytes) -> bytes:
    listfile = b"staredit\\scenario.chk\r\n"
    return build_mpq({
        "staredit\\scenario.chk": chk,
        "(listfile)": listfile,
    })


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str) -> str:
    name = _ILLEGAL.sub("", name).strip().rstrip(".")
    return name or "Untitled"


def find_maps(archive: BoltArchive, verbose: bool = False):
    """Yield (MapInfo, chk_bytes) for every scenario in the archive."""
    for entry in archive.entries():
        if entry.size < 1024:
            continue
        # Cheap sniff first: decompress just enough to see the leading tag.
        try:
            head = archive.read(entry, limit=4)
        except (ValueError, IndexError):
            continue
        if head[:4] not in CHK_FIRST_TAGS:
            continue
        try:
            data = archive.read(entry)
        except (ValueError, IndexError) as exc:
            if verbose:
                print(f"  ! {entry.path}: {exc}", file=sys.stderr)
            continue
        if not looks_like_chk(data):
            continue
        yield parse_map(entry.path, data), data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract StarCraft 64 maps from a ROM you own and convert "
                    "them to PC StarCraft .scm/.scx files.",
        epilog="The extracted maps are Blizzard's copyrighted content. "
               "Keep them to yourself.",
    )
    parser.add_argument("rom", help="StarCraft 64 ROM (.z64, .v64 or .n64)")
    parser.add_argument("-o", "--out", default="maps",
                        help="output directory (default: maps)")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list the maps and exit without writing anything")
    parser.add_argument("--chk", action="store_true",
                        help="also write the raw .chk alongside each map")
    parser.add_argument("--dump-all", metavar="DIR",
                        help="additionally dump every file in the BOLT archive")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        rom = load_rom(args.rom)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    name = rom_internal_name(rom)
    print(f"ROM      : {args.rom}")
    print(f"Internal : {name} [{rom_cart_id(rom)}]  {len(rom) / 2**20:.0f} MiB")
    if "STARCRAFT" not in name.upper():
        print(f"warning  : internal name is {name!r}, not StarCraft 64 -- "
              f"continuing anyway", file=sys.stderr)

    try:
        archive = BoltArchive(rom)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"BOLT     : offset {archive.base:#x}, built {archive.build_stamp}")
    print()

    if args.dump_all:
        count = 0
        for entry in archive.entries():
            try:
                data = archive.read(entry)
            except (ValueError, IndexError) as exc:
                print(f"  ! {entry.path}: {exc}", file=sys.stderr)
                continue
            dest = os.path.join(args.dump_all, entry.path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
            count += 1
        print(f"dumped {count} BOLT files to {args.dump_all}/\n")

    maps = list(find_maps(archive, args.verbose))
    if not maps:
        print("error: no CHK scenarios found in this ROM", file=sys.stderr)
        return 1

    header = (f"{'BOLT':9} {'ext':5} {'edition':18} {'dim':9} "
              f"{'tileset':14} {'p':>2}  name")
    print(header)
    print("-" * len(header))
    for info, _ in maps:
        print(f"{info.bolt_path:9} {info.extension:5} {info.edition:18} "
              f"{str(info.width) + 'x' + str(info.height):9} "
              f"{info.tileset_name:14} {info.players:2}  {info.name}")
    print(f"\n{len(maps)} scenarios")

    if args.list:
        return 0

    os.makedirs(args.out, exist_ok=True)
    written = 0
    for info, chk in maps:
        prefix = info.bolt_path.replace("/", "-")
        stem = f"{prefix} {safe_filename(info.name)}"
        dest = os.path.join(args.out, stem + info.extension)
        with open(dest, "wb") as fh:
            fh.write(build_map_file(chk))
        if args.chk:
            with open(os.path.join(args.out, stem + ".chk"), "wb") as fh:
                fh.write(chk)
        written += 1

    print(f"\nwrote {written} maps to {os.path.abspath(args.out)}")
    print("Copy them into your StarCraft Maps\\ folder to play.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
