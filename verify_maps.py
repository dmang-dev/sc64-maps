#!/usr/bin/env python3
"""
verify_maps.py -- read back the generated .scm/.scx files and check that a
StarCraft-compatible MPQ reader can recover a valid scenario from each one.

    python verify_maps.py maps/

The reader below mirrors StormLib (the reference MPQ implementation that the
StarCraft tool ecosystem is built on), in particular:

  * SFileReadFile.cpp:56  -- the sector offset table is only consulted when a
    compression flag is set on the block entry
  * SFileReadFile.cpp:108-121 -- a sector holds min(sector_size, bytes_left)
    plain bytes; its stored length comes from the sector offset table
  * SFileReadFile.cpp:165 -- a sector is only decompressed when its stored
    length is *less* than its plain length; equal means stored verbatim

Note: mpyq (a popular pure-Python MPQ reader) gets that last rule wrong -- it
compares the stored sector length against every remaining byte in the file
rather than against this sector's own plain length, so it tries to decompress
verbatim sectors in any multi-sector file. Maps written by this project are
correct per StormLib; mpyq just cannot read them back.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import glob
import os
import struct
import sys
import zlib

from extract_sc64_maps import (
    _CRYPT, _hash_string, looks_like_chk, parse_map,
    HASH_FILE_KEY, HASH_TABLE_OFFSET, HASH_NAME_A, HASH_NAME_B,
    MPQ_MAGIC, HASH_ENTRY_FREE,
)

MPQ_FILE_IMPLODE = 0x00000100
MPQ_FILE_COMPRESS = 0x00000200
MPQ_FILE_ENCRYPTED = 0x00010000
MPQ_FILE_SECTOR_CRC = 0x04000000
MPQ_FILE_SINGLE_UNIT = 0x01000000
MPQ_FILE_EXISTS = 0x80000000
MPQ_FILE_COMPRESS_MASK = MPQ_FILE_IMPLODE | MPQ_FILE_COMPRESS


def _decrypt(data: bytes, key: int) -> bytes:
    seed = 0xEEEEEEEE
    out = bytearray()
    for (value,) in struct.iter_unpack("<I", data):
        seed = (seed + _CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        plain = value ^ ((key + seed) & 0xFFFFFFFF)
        out += struct.pack("<I", plain)
        key = (((~key & 0xFFFFFFFF) << 0x15) + 0x11111111 | (key >> 0x0B)) & 0xFFFFFFFF
        seed = (plain + seed + (seed << 5) + 3) & 0xFFFFFFFF
    return bytes(out)


class MpqError(Exception):
    pass


class MpqReader:
    def __init__(self, path: str):
        with open(path, "rb") as fh:
            self.data = fh.read()
        if self.data[:4] != MPQ_MAGIC:
            raise MpqError(f"bad magic {self.data[:4]!r}")
        (_magic, header_size, self.archive_size, self.format_version,
         self.sector_shift, hash_pos, block_pos,
         self.hash_count, self.block_count) = struct.unpack_from(
            "<4sIIHHIIII", self.data, 0)

        if header_size < 32:
            raise MpqError(f"header size {header_size} too small")
        if self.archive_size != len(self.data):
            raise MpqError(
                f"header says {self.archive_size} bytes, file is {len(self.data)}")
        if self.hash_count & (self.hash_count - 1):
            raise MpqError(f"hash table size {self.hash_count} is not a power of two")

        self.sector_size = 512 << self.sector_shift

        raw = self.data[hash_pos:hash_pos + self.hash_count * 16]
        self.hash_table = list(struct.iter_unpack(
            "<IIHHI", _decrypt(raw, _hash_string("(hash table)", HASH_FILE_KEY))))

        raw = self.data[block_pos:block_pos + self.block_count * 16]
        self.block_table = list(struct.iter_unpack(
            "<IIII", _decrypt(raw, _hash_string("(block table)", HASH_FILE_KEY))))

    def find(self, name: str):
        start = _hash_string(name, HASH_TABLE_OFFSET) & (self.hash_count - 1)
        want_a = _hash_string(name, HASH_NAME_A)
        want_b = _hash_string(name, HASH_NAME_B)
        for probe in range(self.hash_count):
            name_a, name_b, _locale, _platform, block = \
                self.hash_table[(start + probe) % self.hash_count]
            if block == HASH_ENTRY_FREE:
                return None
            if name_a == want_a and name_b == want_b:
                return block
        return None

    def read(self, name: str) -> bytes:
        block_index = self.find(name)
        if block_index is None:
            raise MpqError(f"{name!r} not present")
        offset, packed_size, size, flags = self.block_table[block_index]
        if not flags & MPQ_FILE_EXISTS:
            raise MpqError(f"{name!r} block entry has no FILE_EXISTS flag")
        if flags & MPQ_FILE_ENCRYPTED:
            raise MpqError(f"{name!r} is encrypted (not supported here)")
        raw = self.data[offset:offset + packed_size]
        if len(raw) != packed_size:
            raise MpqError(f"{name!r} data runs past end of archive")

        if flags & MPQ_FILE_SINGLE_UNIT or not flags & MPQ_FILE_COMPRESS_MASK:
            return raw[:size]

        # Sectored + compressible: consult the sector offset table.
        sector_count = (size + self.sector_size - 1) // self.sector_size or 1
        entries = sector_count + 1
        if flags & MPQ_FILE_SECTOR_CRC:
            entries += 1
        positions = struct.unpack_from(f"<{entries}I", raw, 0)
        if positions[0] != entries * 4:
            raise MpqError(
                f"{name!r} sector table starts at {positions[0]}, expected {entries * 4}")

        out = bytearray()
        left = size
        for i in range(sector_count):
            plain_len = min(self.sector_size, left)
            sector = raw[positions[i]:positions[i + 1]]
            if len(sector) < plain_len:              # StormLib: strictly less
                method = sector[0]
                if method == 0x02:
                    sector = zlib.decompress(sector[1:])
                else:
                    raise MpqError(
                        f"{name!r} sector {i} uses compression {method:#04x}, "
                        f"which this verifier does not implement")
            elif len(sector) != plain_len:
                raise MpqError(
                    f"{name!r} sector {i} is {len(sector)} bytes, expected {plain_len}")
            out += sector
            left -= plain_len
        if len(out) != size:
            raise MpqError(f"{name!r} reassembled to {len(out)} bytes, expected {size}")
        return bytes(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate maps produced by extract_sc64_maps.py")
    parser.add_argument("maps", nargs="?", default="maps",
                        help="directory of .scm/.scx files (default: maps)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.maps, "*.scm")) +
                   glob.glob(os.path.join(args.maps, "*.scx")))
    if not paths:
        print(f"error: no .scm/.scx files in {args.maps!r}", file=sys.stderr)
        return 1

    passed = 0
    failures = []
    for path in paths:
        base = os.path.basename(path)
        try:
            mpq = MpqReader(path)
            chk = mpq.read("staredit\\scenario.chk")
            if not looks_like_chk(chk):
                raise MpqError("recovered data is not a well-formed CHK")
            info = parse_map(base, chk)
            if not info.width or not info.height:
                raise MpqError("scenario has no DIM section")
            expected = ".scx" if info.is_broodwar else ".scm"
            if not base.endswith(expected):
                raise MpqError(f"named {base[-4:]} but the scenario is {info.edition}")
            # The listfile is optional for the game but we always write one.
            mpq.read("(listfile)")
            passed += 1
            if args.verbose:
                print(f"  ok  {base}  {info.width}x{info.height} "
                      f"{info.tileset_name} {info.edition}")
        except (MpqError, struct.error, zlib.error, ValueError) as exc:
            failures.append((base, exc))

    print(f"\n{passed}/{len(paths)} maps verified")
    for base, exc in failures:
        print(f"  FAIL {base}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
