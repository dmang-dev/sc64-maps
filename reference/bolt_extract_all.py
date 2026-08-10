#!/usr/bin/env python3
"""
bolt_extract_all.py -- a standalone Python rewrite of Adam Heinermann's
BOLTextract, covering the N64/GBA-era algorithm.

    python bolt_extract_all.py "StarCraft 64 (USA).n64" out/

Where the C++ original needs Visual Studio 2019 and only accepts z64 ROMs,
this needs nothing but CPython and normalises v64/n64 byte order for you.
It dumps *every* file in the archive (2111 of them for StarCraft 64), named
by their position in the BOLT directory tree with a guessed extension --
the archive stores no filenames, only hashes, and those are not reversible.

If you only want the maps, use ../extract_sc64_maps.py instead; it produces
ready-to-play .scm/.scx files rather than raw chunks.

Ported from BOLTextract (GPL-3.0) -- see BOLTextract-cpp/ for the original.
The decompressor mirrors n64.cpp; the archive walk mirrors bolt.cpp; the
extension guessing mirrors guess_type.cpp.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import os
import string
import struct
import sys

Z64_MAGIC = bytes.fromhex("80371240")
V64_MAGIC = bytes.fromhex("37804012")
N64_MAGIC = bytes.fromhex("40123780")

FLAG_UNCOMPRESSED = 0x08
HEADER_SIZE = 16
ENTRY_SIZE = 16


def load_rom(path: str) -> bytes:
    """Read a ROM (or any binary) and normalise N64 dumps to z64 order."""
    with open(path, "rb") as fh:
        raw = fh.read()
    magic = raw[:4]
    if magic == V64_MAGIC:
        buf = bytearray(raw)
        buf[0::2], buf[1::2] = raw[1::2], raw[0::2]
        return bytes(buf)
    if magic == N64_MAGIC:
        buf = bytearray(len(raw))
        buf[0::4], buf[1::4], buf[2::4], buf[3::4] = raw[3::4], raw[2::4], raw[1::4], raw[0::4]
        return bytes(buf)
    return raw  # z64 already, or a non-ROM binary containing a BOLT archive


# --------------------------------------------------------------------------
# Decompression (port of n64.cpp)
# --------------------------------------------------------------------------

def decompress_n64(rom: bytes, base: int, offset: int, expected: int) -> bytes:
    out = bytearray()
    pos = base + offset
    op_count = ext_offset = ext_run = 0

    while len(out) < expected:
        byte = rom[pos]
        pos += 1
        op_count += 1

        if byte & 0x80:
            if byte & 0x40:
                ext_offset = (ext_offset << 6) | (byte & 0x3F)
            elif byte & 0x20:
                ext_run = (ext_run << 5) | (byte & 0x1F)
            elif byte & 0x10:
                ext_run = (ext_run << 2) | (byte & 0b0011)
                ext_offset = (ext_offset << 2) | ((byte & 0b1100) >> 2)
            else:
                run = ((ext_run << 4) | (byte & 0xF)) + 1
                out += rom[pos:pos + run]
                pos += run
                op_count = ext_offset = ext_run = 0
        else:
            if not out:
                raise ValueError("back-reference before any output")
            rel = ((ext_offset << 4) | (byte & 0xF)) + 1
            run = ((ext_run << 3) | (byte >> 4)) + op_count + 1
            if rel > len(out):
                raise ValueError(f"back-reference {rel} exceeds {len(out)} bytes")
            for _ in range(run):
                out.append(out[-rel])
            op_count = ext_offset = ext_run = 0

    return bytes(out)


# --------------------------------------------------------------------------
# Extension guessing (port of guess_type.cpp)
# --------------------------------------------------------------------------

_PRINTABLE = set(bytes(string.printable, "ascii"))


def _is_img(d: bytes) -> bool:
    if len(d) <= 16:
        return False
    unk1, bpp, unk2, width, height, unk4 = struct.unpack_from(">HHIHHI", d, 0)
    return (len(d) == width * height + 16 and (unk1 & 0xFF00) == 0
            and (unk1 & 0xFF) < 5 and bpp == 0x0008 and unk2 == 0 and unk4 == 0)


def _is_pal(d: bytes) -> bool:
    if len(d) <= 8:
        return False
    unk1, entries, _unk2 = struct.unpack_from(">IHH", d, 0)
    return len(d) == 255 * 2 + 8 and unk1 == 0 and entries == 0x00FF


def _is_audio(d: bytes) -> bool:
    if len(d) <= 12:
        return False
    channels, bits, rate, size1, size2 = struct.unpack_from(">BBHII", d, 0)
    if channels > 2 or bits not in (4, 8, 16, 24, 32):
        return False
    if size1 and size2:
        return False
    size = size2 or size1
    return size + 12 == len(d) and 8000 <= rate <= 44100


def _is_tbl(d: bytes) -> bool:
    if len(d) <= 4:
        return False
    count = struct.unpack_from("<H", d, 0)[0]
    if count <= 1:
        return False
    start = count * 2 + 2
    if start >= len(d) or 2 + count * 2 > len(d):
        return False
    offsets = struct.unpack_from(f"<{count}H", d, 2)
    if offsets[0] != start:
        return False
    for i, off in enumerate(offsets):
        if not start <= off < len(d):
            return False
        if i and (d[off - 1] != 0 or off <= offsets[i - 1]):
            return False
    return d[-1] == 0


def _is_grp(d: bytes) -> bool:
    if len(d) <= 14:
        return False
    frames, width, height = struct.unpack_from("<HHH", d, 0)
    if not frames or not width or not height:
        return False
    start = 6 + frames * 8
    if start + 1 >= len(d) or start > len(d):
        return False
    for i in range(frames):
        dx, dy, fw, fh, off = struct.unpack_from("<BBBBI", d, 6 + i * 8)
        if not fw or not fh:
            return False
        if dx + fw > width or dy + fh > height:
            return False
        if not start <= off < len(d):
            return False
        if i == 0 and off != start:
            return False
    return True


def guess_extension(d: bytes) -> str:
    if not d:
        return ".unk"
    if len(d) > 32:
        if d[:4] == b"RIFF":
            return ".wav"
        if d[:4] == b"FONT":
            return ".fnt"
        if d[:4] in (b"TYPE", b"VER ", b"IVER", b"IVE2", b"VCOD"):
            return ".chk"
    if _is_img(d):
        return ".unkimg"
    if _is_pal(d):
        return ".unkpal"
    if _is_audio(d):
        return ".unkpcm"
    if _is_tbl(d):
        return ".tbl"
    if _is_grp(d):
        return ".grp"
    if all(b in _PRINTABLE for b in d):
        return ".txt"
    if len(d) > 32:
        if d[:4] == b"VAGp":
            return ".vag"
        if d[:4] == b"\x7fELF":
            return ".elf"
    return ".unk"


# --------------------------------------------------------------------------
# Archive walk (port of bolt.cpp)
# --------------------------------------------------------------------------

def extract(rom: bytes, out_dir: str, verbose: bool = False) -> tuple[int, int]:
    base = rom.find(b"BOLT")
    if base < 0:
        base = rom.find(b"bolt")
    if base < 0:
        raise ValueError("no BOLT archive found")

    hdr = rom[base:base + HEADER_SIZE]
    hour, minute, second, _ms, month, day, year, num_entries = hdr[4:12]
    print(f"BOLT at {base:#x}, built {1900 + year:04d}-{month:02d}-{day:02d} "
          f"{hour:02d}:{minute:02d}:{second:02d}")

    written = failed = 0

    def entry_at(offset: int):
        b = rom[base + offset:base + offset + ENTRY_SIZE]
        size, data_off, file_hash = struct.unpack_from(">III", b, 4)
        return b[0], b[3], size, data_off, file_hash  # flags, type, ...

    def walk(prefix: str, table_offset: int, count: int, depth: int = 0):
        nonlocal written, failed
        if depth > 8:
            return
        for i in range(count or 256):
            flags, ftype, size, data_off, file_hash = entry_at(table_offset + i * ENTRY_SIZE)
            name = f"{i:03X}"
            if file_hash == 0:                       # directory
                walk(os.path.join(prefix, name), data_off, ftype, depth + 1)
                continue
            try:
                if flags & FLAG_UNCOMPRESSED:
                    data = rom[base + data_off:base + data_off + size]
                else:
                    data = decompress_n64(rom, base, data_off, size)
                if len(data) != size:
                    raise ValueError(f"got {len(data)} bytes, expected {size}")
            except (ValueError, IndexError) as exc:
                print(f"  ! {prefix}/{name}: {exc}", file=sys.stderr)
                failed += 1
                continue
            folder = os.path.join(out_dir, prefix)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, name + guess_extension(data))
            with open(path, "wb") as fh:
                fh.write(data)
            written += 1
            if verbose:
                print(f"  {os.path.relpath(path, out_dir)}  {size} bytes")

    walk("", HEADER_SIZE, num_entries or 256)
    return written, failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract every file from a Mass Media BOLT archive "
                    "(N64/GBA algorithm).")
    parser.add_argument("input", help="ROM or binary containing a BOLT archive")
    parser.add_argument("output", nargs="?", default="bolt-out",
                        help="output directory (default: bolt-out)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        rom = load_rom(args.input)
        written, failed = extract(rom, args.output, args.verbose)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nextracted {written} files to {os.path.abspath(args.output)}")
    if failed:
        print(f"{failed} entries failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
