#!/usr/bin/env python3
"""
mpq_keycrack.py -- read MPQ blocks whose file NAME is unknown.

An MPQ derives a file's encryption key from its *plain* file name
(``DecryptFileKey``, reference/StormLib/src/SBaseCommon.cpp:681-700), so a
block whose name is missing from the archive's ``(listfile)`` looks like noise.
StarDat.mpq ships no listfile at all, yet 2413 of its 2897 blocks are
encrypted, so name-based reading reaches none of them.

The key can be recovered from the *content* instead, because a
compressed-and-encrypted file starts with a sector offset table whose first
DWORD is predictable:

    SectorOffsets[0] == (sector_count + 1) * 4              (+4 if SECTOR_CRC)

which is exactly ``dwSectorOffsLen`` in ``AllocateSectorOffsets``
(SBaseCommon.cpp:1235-1322) -- the table sits immediately before sector 0, so
its own length is where sector 0 begins. That gives one known plaintext DWORD
paired with its ciphertext, and the MPQ cipher's first round is thin enough to
invert:

    key2_0 = 0xEEEEEEEE + CryptTable[0x400 + (key1 & 0xFF)]
    plain0 = cipher0 ^ (key1 + key2_0)

so ``(key1 + key2_0) == cipher0 ^ plain0`` is known outright, and only the low
byte of key1 feeds the table lookup -- 256 candidates, each checked for
self-consistency. Survivors are then filtered on the *second* DWORD, which is
not known exactly but is bounded: sector 1 cannot start further into the file
than the table length plus one uncompressed sector.

That is StormLib's ``DetectFileKeyBySectorSize``
(SBaseCommon.cpp:548-601). This module reimplements it, together with the
magic-number fallback ``DetectFileKeyByKnownContent`` / ``DetectFileKeyByContent``
(SBaseCommon.cpp:605-679) used when a file has no sector offset table at all.

FIX_KEY (0x00020000, ``MPQ_FILE_KEY_V2``) mixes the block's own offset and size
into the key. That happens *after* the name hash and *before* any encryption,
so what the crack recovers is the already-fixed "effective" key -- the only one
needed to decrypt. Recovering the underlying name hash from it is impossible in
general (the name is the only preimage), but the effective key can be checked
against a known name with ``expected_key()``.

What the technique cannot do:

  * Files that are ENCRYPTED but not compressed have no sector offset table
    (``AllocateSectorOffsets`` returns early), so there is no predictable
    plaintext. Only the magic-number fallback applies, and only if the file
    happens to be a RIFF/PE/XML.
  * The same holds for SINGLE_UNIT files -- one sector, no table.
  * A recovered key decrypts the bytes but never reveals the name; blocks stay
    nameless.

    python mpq_keycrack.py gamedata/mpq/StarDat.mpq --census
    python mpq_keycrack.py gamedata/mpq/BrooDat.mpq --validate

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import bz2
import collections
import os
import struct
import sys
import zlib
from typing import Iterator, NamedTuple, Optional

from compare_with_stock import (COMPRESS, ENCRYPTED, EXISTS, FIX_KEY, IMPLODE,
                                SECTOR_CRC, SINGLE_UNIT, StockMpq, _CRYPT,
                                _decrypt, _hash)
from pkware_explode import explode

# StormLib names this MPQ_HASH_KEY2_MIX (StormCommon.h:292): the quarter of the
# crypt table that stirs the rolling key2.
KEY2_MIX = 0x400
MASK32 = 0xFFFFFFFF


# --------------------------------------------------------------------------
# Block table access
# --------------------------------------------------------------------------

class Block(NamedTuple):
    """One entry of the MPQ block table, as stored."""

    index: int
    offset: int          # relative to the MPQ header, not the file start
    packed: int          # bytes on disk
    size: int            # bytes after decompression
    flags: int

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & ENCRYPTED)

    @property
    def compressed(self) -> bool:
        # MPQ_FILE_COMPRESS_MASK == 0x0000FF00 (StormLib.h:235); in practice
        # only IMPLODE and COMPRESS are ever set.
        return bool(self.flags & (IMPLODE | COMPRESS))

    @property
    def single_unit(self) -> bool:
        return bool(self.flags & SINGLE_UNIT)


def blocks(archive: StockMpq) -> Iterator[Block]:
    """Yield every block table entry of *archive*, in table order."""
    for index in range(len(archive.block_table) // 16):
        offset, packed, size, flags = struct.unpack_from(
            "<IIII", archive.block_table, index * 16)
        yield Block(index, offset, packed, size, flags)


def get_block(archive: StockMpq, index: int) -> Block:
    offset, packed, size, flags = struct.unpack_from(
        "<IIII", archive.block_table, index * 16)
    return Block(index, offset, packed, size, flags)


def expected_key(archive: StockMpq, index: int, name: str) -> int:
    """The effective key a *known* name yields, for cross-checking a crack.

    Mirrors DecryptFileKey (SBaseCommon.cpp:681-700): hash the plain name, then
    apply the FIX_KEY adjustment if the block asks for it.
    """
    block = get_block(archive, index)
    key = _hash(name.replace("/", "\\").split("\\")[-1], 3)
    if block.flags & FIX_KEY:
        key = ((key + block.offset) ^ block.size) & MASK32
    return key


def sector_count(archive: StockMpq, block: Block) -> int:
    """Number of data sectors, per AllocateSectorOffsets (SBaseCommon.cpp:1257)."""
    if block.single_unit:
        return 1
    return (block.size - 1) // archive.sector_size + 1


def sector_table_len(archive: StockMpq, block: Block) -> int:
    """``dwSectorOffsLen`` -- the predictable plaintext (SBaseCommon.cpp:1260)."""
    length = (sector_count(archive, block) + 1) * 4
    if block.flags & SECTOR_CRC:
        length += 4
    return length


# --------------------------------------------------------------------------
# The crack itself
# --------------------------------------------------------------------------

def _detect_key_by_sector_size(cipher0: int, cipher1: int,
                               sector_size: int, decrypted0: int) -> Optional[int]:
    """Port of DetectFileKeyBySectorSize (SBaseCommon.cpp:548-601).

    *cipher0*/*cipher1* are the first two encrypted DWORDs of the sector offset
    table; *decrypted0* is the expected value of the first one. Returns the
    *file* key -- one more than the key the offset table itself is encrypted
    with, because StormLib decrypts the table with ``dwFileKey - 1``
    (SBaseCommon.cpp:1322).
    """
    # StormLib also tries decrypted0+1..+3: some writers pad the gap between
    # the offset table and sector 0, so offsets[0] can exceed the table length.
    for guess in range(decrypted0, decrypted0 + 4):
        # Sector 1 begins after the table plus at most one full sector, since
        # a "compressed" sector is never stored larger than the plain one.
        decrypted1_max = (sector_size + guess) & MASK32

        # (key1 + key2) is fully determined by the known plaintext.
        key1_plus_key2 = (cipher0 ^ guess) - 0xEEEEEEEE & MASK32

        for i in range(0x100):
            key1 = (key1_plus_key2 - _CRYPT[KEY2_MIX + i]) & MASK32
            key2 = (0xEEEEEEEE + _CRYPT[KEY2_MIX + (key1 & 0xFF)]) & MASK32
            plain0 = cipher0 ^ ((key1 + key2) & MASK32)

            # Self-consistency: only key1 values whose own low byte selects the
            # table entry we assumed can be real. Usually exactly one of 256.
            if plain0 != guess:
                continue

            # Advance one cipher round and test the bounded second DWORD.
            next1 = ((((~key1) << 0x15) + 0x11111111) | (key1 >> 0x0B)) & MASK32
            next2 = (plain0 + key2 + (key2 << 5) + 3) & MASK32
            next2 = (next2 + _CRYPT[KEY2_MIX + (next1 & 0xFF)]) & MASK32
            plain1 = cipher1 ^ ((next1 + next2) & MASK32)
            if plain1 <= decrypted1_max:
                return (key1 + 1) & MASK32
    return None


def _detect_key_by_known_content(cipher0: int, cipher1: int,
                                 decrypted0: int, decrypted1: int) -> Optional[int]:
    """Port of DetectFileKeyByKnownContent (SBaseCommon.cpp:605-647).

    Same first round inversion, but both plaintext DWORDs are known exactly, so
    no ``+1`` adjustment: this key encrypts the data directly.
    """
    key1_plus_key2 = (cipher0 ^ decrypted0) - 0xEEEEEEEE & MASK32
    for i in range(0x100):
        key1 = (key1_plus_key2 - _CRYPT[KEY2_MIX + i]) & MASK32
        key2 = (0xEEEEEEEE + _CRYPT[KEY2_MIX + (key1 & 0xFF)]) & MASK32
        plain0 = cipher0 ^ ((key1 + key2) & MASK32)
        if plain0 != decrypted0:
            continue
        next1 = ((((~key1) << 0x15) + 0x11111111) | (key1 >> 0x0B)) & MASK32
        next2 = (plain0 + key2 + (key2 << 5) + 3) & MASK32
        next2 = (next2 + _CRYPT[KEY2_MIX + (next1 & 0xFF)]) & MASK32
        plain1 = cipher1 ^ ((next1 + next2) & MASK32)
        if plain1 == decrypted1:
            return key1
    return None


def _detect_key_by_content(cipher0: int, cipher1: int,
                           available: int, file_size: int) -> Optional[int]:
    """Port of DetectFileKeyByContent (SBaseCommon.cpp:649-679).

    The only fallback for files with no sector offset table. It works purely
    off file-format magic, so it succeeds on RIFF/PE/XML and nothing else.
    """
    if available >= 0x0C:                                   # 'RIFF', size-8
        key = _detect_key_by_known_content(
            cipher0, cipher1, 0x46464952, (file_size - 8) & MASK32)
        if key is not None:
            return key
    if available > 0x40:                                    # 'MZ', e_cblp/e_cp
        key = _detect_key_by_known_content(cipher0, cipher1, 0x00905A4D, 3)
        if key is not None:
            return key
    if available > 0x04:                                    # '<?xm', 'l ve'
        key = _detect_key_by_known_content(
            cipher0, cipher1, 0x6D783F3C, 0x6576206C)
        if key is not None:
            return key
    return None


def _plausible_sector_table(archive: StockMpq, block: Block,
                            positions: tuple[int, ...]) -> bool:
    """Validate a decrypted sector offset table (SBaseCommon.cpp:1332-1352).

    A wrong key survives the two-DWORD test roughly once in 2^20, so re-check
    the whole table: offsets must ascend and no stored sector may exceed the
    archive sector size.
    """
    count = sector_count(archive, block)
    for i in range(count):
        delta = positions[i + 1] - positions[i]
        if delta < 0 or delta > archive.sector_size:
            return False
    return positions[0] >= (count + 1) * 4


def detect_file_key(archive: StockMpq, block_index: int) -> Optional[int]:
    """Recover a block's effective decryption key from its content alone.

    Returns the key usable directly with ``_decrypt`` (FIX_KEY already folded
    in), or ``None`` when the block is not encrypted, is empty, or the key
    cannot be recovered. Never consults the hash table or any name.
    """
    block = get_block(archive, block_index)
    if not block.flags & EXISTS or block.size == 0 or not block.encrypted:
        return None

    pos = archive.base + block.offset

    # Path 1: sector offset table. Requires a compressed, multi-sector file.
    if block.compressed and not block.single_unit:
        head = archive.raw[pos:pos + 8]
        if len(head) < 8:
            return None
        cipher0, cipher1 = struct.unpack("<II", head)
        key = _detect_key_by_sector_size(
            cipher0, cipher1, archive.sector_size,
            sector_table_len(archive, block))
        if key is not None:
            entries = sector_count(archive, block) + 1
            if block.flags & SECTOR_CRC:
                entries += 1
            table = _decrypt(archive.raw[pos:pos + entries * 4],
                             (key - 1) & MASK32)
            positions = struct.unpack(f"<{entries}I", table)
            if _plausible_sector_table(archive, block, positions):
                return key
        return None

    # Path 2: no offset table -- magic numbers only.
    available = min(archive.sector_size, block.packed, block.size)
    head = archive.raw[pos:pos + 8]
    if len(head) < 8:
        return None
    cipher0, cipher1 = struct.unpack("<II", head)
    return _detect_key_by_content(cipher0, cipher1, available, block.size)


# --------------------------------------------------------------------------
# Reading a block once the key is known
# --------------------------------------------------------------------------

# Compression mask bits, from reference/StormLib/src/StormLib.h:274-283.
# Huffman and ADPCM are audio-only and not implemented here; a WAVE sector
# carries 0x41 (mono) or 0x81 (stereo) and is left compressed.
COMPRESSION_NAMES = {
    0x01: "huffman", 0x02: "zlib", 0x08: "pkware", 0x10: "bzip2",
    0x40: "adpcm-mono", 0x80: "adpcm-stereo",
    0x41: "huffman+adpcm-mono", 0x81: "huffman+adpcm-stereo",
}


class UndecodableSector(ValueError):
    """A sector uses a compression this module does not implement."""

    def __init__(self, mask: int):
        self.mask = mask
        super().__init__(
            f"compression mask {mask:#04x} "
            f"({COMPRESSION_NAMES.get(mask, 'unknown')}) not implemented")


def _decompress_sector(sector: bytes, imploded: bool) -> bytes:
    """Decompress one stored sector (SFileReadFile.cpp:162-209)."""
    if imploded:
        # MPQ_FILE_IMPLODE carries no mask byte -- the sector is raw PKWARE.
        return explode(sector)
    mask, body = sector[0], sector[1:]
    if mask == 0x08:
        return explode(body)
    if mask == 0x02:
        return zlib.decompress(body)
    if mask == 0x10:
        return bz2.decompress(body)
    raise UndecodableSector(mask)


def read_block(archive: StockMpq, block_index: int,
               key: Optional[int] = None,
               max_sectors: Optional[int] = None) -> Optional[bytes]:
    """Decrypt and decompress a block, cracking its key if not supplied.

    *max_sectors* stops after that many sectors -- enough to identify content
    without paying for a 20 MB file. Returns ``None`` if the key is unknown;
    raises ``UndecodableSector`` if a sector uses huffman/ADPCM.
    """
    block = get_block(archive, block_index)
    if not block.flags & EXISTS or block.size == 0:
        return None
    if block.encrypted and key is None:
        key = detect_file_key(archive, block_index)
        if key is None:
            return None

    pos = archive.base + block.offset
    imploded = bool(block.flags & IMPLODE)

    if block.single_unit or not block.compressed:
        data = archive.raw[pos:pos + block.packed]
        if block.encrypted:
            data = _decrypt(data, key)
        if block.compressed and block.packed < block.size:
            return _decompress_sector(data, imploded)
        return data[:block.size]

    count = sector_count(archive, block)
    entries = count + 1 + (1 if block.flags & SECTOR_CRC else 0)
    table = archive.raw[pos:pos + entries * 4]
    if block.encrypted:
        table = _decrypt(table, (key - 1) & MASK32)
    positions = struct.unpack(f"<{entries}I", table)

    wanted = count if max_sectors is None else min(count, max_sectors)
    out = bytearray()
    for i in range(wanted):
        sector = archive.raw[pos + positions[i]:pos + positions[i + 1]]
        if block.encrypted:
            sector = _decrypt(sector, (key + i) & MASK32)
        plain = min(archive.sector_size, block.size - len(out))
        # StormLib's rule: a sector is compressed only if stored smaller.
        out += sector[:plain] if len(sector) >= plain else \
            _decompress_sector(sector, imploded)
    return bytes(out)


def sector_compression(archive: StockMpq, block_index: int,
                       key: Optional[int] = None) -> Optional[int]:
    """The compression mask byte of sector 0, or ``None`` if it is stored raw."""
    block = get_block(archive, block_index)
    if not block.compressed or block.single_unit or block.size == 0:
        return None
    if block.encrypted and key is None:
        key = detect_file_key(archive, block_index)
        if key is None:
            return None
    pos = archive.base + block.offset
    entries = sector_count(archive, block) + 1
    if block.flags & SECTOR_CRC:
        entries += 1
    table = archive.raw[pos:pos + entries * 4]
    if block.encrypted:
        table = _decrypt(table, (key - 1) & MASK32)
    positions = struct.unpack(f"<{entries}I", table)
    stored = positions[1] - positions[0]
    plain = min(archive.sector_size, block.size)
    if stored >= plain:
        return None                       # stored uncompressed, no mask byte
    sector = archive.raw[pos + positions[0]:pos + positions[0] + 4]
    if block.encrypted:
        sector = _decrypt(sector, key)
    return sector[0] if sector else None


# --------------------------------------------------------------------------
# Content classification -- what did we just decrypt?
# --------------------------------------------------------------------------

def classify(data: bytes, size: int) -> str:
    """A short structural label for decrypted bytes. Magic only, no guessing."""
    if not data:
        return "empty"
    head = data[:16]
    if head[:4] == b"RIFF":
        return "RIFF/WAVE"
    if head[:4] in (b"SMK2", b"SMK4"):
        return "Smacker video"
    if head[:2] == b"MZ":
        return "PE/DOS executable"
    if head[:4] == b"MPQ\x1a":
        return "nested MPQ"
    if head[:4] == b"\x89PNG":
        return "PNG"
    if head[0] == 0x0A and len(head) > 3 and head[1] in (0, 2, 3, 4, 5) and head[2] == 1:
        return "PCX image"
    if head[:2] == b"BM":
        return "BMP"
    if head[:3] == b"\x1f\x8b\x08":
        return "gzip"
    # StarCraft dialog resource. Identified from BrooDat.mpq block 14, which
    # the BrooDat listfile names Rez\titledlg.bin: every such file opens with
    # eight zero bytes then the same 7F 02 DF 01 80 02 E0 01 control block.
    if head[:16].hex().startswith("00000000000000007f02df018002e001"):
        return "Rez dialog (.bin)"
    # CHK: a chain of 4-byte ASCII tags with little-endian lengths.
    if _looks_like_chk_head(data):
        return "CHK scenario"
    if _looks_like_tbl(data, size):
        return "TBL string table"
    if _looks_like_grp(data, size):
        return "GRP sprite"
    if _looks_like_text(data):
        return "text"
    return "unknown"


def _looks_like_chk_head(data: bytes) -> bool:
    """Walk the CHK section chain; every tag must be printable ASCII."""
    if len(data) < 8:
        return False
    pos, seen = 0, 0
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        if not all(0x20 <= c < 0x7F for c in tag):
            return False
        (length,) = struct.unpack_from("<i", data, pos + 4)
        if length < 0 or pos + 8 + length > len(data) + 0x10000:
            return False
        pos += 8 + length
        seen += 1
        if seen >= 3:
            return True
    return seen >= 2


def _looks_like_tbl(data: bytes, size: int) -> bool:
    """StarCraft .tbl: WORD count, then that many WORD offsets into the blob."""
    if len(data) < 4:
        return False
    (count,) = struct.unpack_from("<H", data, 0)
    if count == 0 or count * 2 + 2 > len(data) or count * 2 + 2 > size:
        return False
    (first,) = struct.unpack_from("<H", data, 2)
    return first == count * 2 + 2


def _looks_like_grp(data: bytes, size: int) -> bool:
    """GRP: WORD frames, WORD max width, WORD max height, then frame headers."""
    if len(data) < 8:
        return False
    frames, width, height = struct.unpack_from("<HHH", data, 0)
    if not (1 <= frames <= 2000 and 1 <= width <= 512 and 1 <= height <= 512):
        return False
    return 6 + frames * 8 <= size


def _looks_like_text(data: bytes) -> bool:
    sample = data[:512]
    printable = sum(1 for c in sample if 0x20 <= c < 0x7F or c in (9, 10, 13))
    return printable >= len(sample) * 0.95


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _census(path: str, limit: Optional[int] = None, chk_only: bool = False) -> int:
    archive = StockMpq(path)
    name = os.path.basename(path)
    entries = list(blocks(archive))
    if limit:
        entries = entries[:limit]

    total = len(entries)
    encrypted = [b for b in entries if b.encrypted]
    recovered, failed = [], []
    kinds = collections.Counter()
    masks = collections.Counter()
    chks = []

    for block in entries:
        key = detect_file_key(archive, block.index) if block.encrypted else None
        if block.encrypted:
            if key is None:
                failed.append(block)
                continue
            recovered.append(block)
        mask = sector_compression(archive, block.index, key)
        masks[COMPRESSION_NAMES.get(mask, f"{mask:#04x}") if mask is not None
              else "stored"] += 1
        try:
            # One sector is enough to classify; CHKs need the whole file.
            head = read_block(archive, block.index, key, max_sectors=1)
        except UndecodableSector as exc:
            kinds[f"undecodable ({COMPRESSION_NAMES.get(exc.mask, hex(exc.mask))})"] += 1
            continue
        except Exception as exc:                        # noqa: BLE001
            kinds[f"error: {type(exc).__name__}"] += 1
            continue
        if head is None:
            kinds["no key"] += 1
            continue
        kind = classify(head, block.size)
        kinds[kind] += 1
        if kind == "CHK scenario":
            chks.append(block)

    print(f"== {name}")
    print(f"   blocks total       {total}")
    print(f"   encrypted          {len(encrypted)}")
    print(f"   key recovered      {len(recovered)}")
    print(f"   key NOT recovered  {len(failed)}")
    if failed:
        flagcount = collections.Counter(f"{b.flags:#010x}" for b in failed)
        print("   failures by flags: " +
              ", ".join(f"{k} x{v}" for k, v in flagcount.most_common()))
    print("   sector-0 compression:")
    for k, v in masks.most_common():
        print(f"      {k:24} {v}")
    print("   content types:")
    for k, v in kinds.most_common():
        print(f"      {k:24} {v}")
    if chks:
        print(f"   CHK scenarios: {[b.index for b in chks]}")
    return len(chks)


def _validate(path: str, sample: int = 0) -> int:
    """Crack keys for blocks whose names we do know, and check both ways."""
    archive = StockMpq(path)
    listfile = archive.read("(listfile)")
    if not listfile:
        print(f"{os.path.basename(path)}: no (listfile), cannot validate")
        return 1
    names = [n.strip() for n in listfile.decode("latin1").splitlines() if n.strip()]

    tried = ok_key = ok_bytes = 0
    mismatched, unrecovered, undecodable = [], [], []
    for name in names:
        index = archive._lookup(name)
        if index is None:
            continue
        block = get_block(archive, index)
        if not block.encrypted or block.size == 0:
            continue
        tried += 1
        if sample and tried > sample:
            tried -= 1
            break
        cracked = detect_file_key(archive, index)
        if cracked is None:
            unrecovered.append(name)
            continue
        want = expected_key(archive, index, name)
        if cracked != want:
            mismatched.append((name, cracked, want))
            continue
        ok_key += 1
        try:
            mine = read_block(archive, index, cracked)
            theirs = archive.read(name)
        except UndecodableSector:
            undecodable.append(name)
            continue
        except Exception:                               # noqa: BLE001
            continue
        if mine == theirs:
            ok_bytes += 1

    print(f"== {os.path.basename(path)} validation")
    print(f"   named encrypted blocks tried : {tried}")
    print(f"   key recovered and equal to hash(name,3) [FIX_KEY applied] : {ok_key}")
    print(f"   decrypted bytes identical to name-based read              : {ok_bytes}")
    print(f"   key not recovered : {len(unrecovered)}")
    print(f"   key wrong         : {len(mismatched)}")
    print(f"   not decodable (huffman/ADPCM audio) : {len(undecodable)}")
    for nm, got, want in mismatched[:5]:
        print(f"     ! {nm}: got {got:#010x} want {want:#010x}")
    return 0 if not mismatched and not unrecovered else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover MPQ file keys from content, without file names.")
    parser.add_argument("archive", nargs="+", help="MPQ file(s)")
    parser.add_argument("--validate", action="store_true",
                        help="cross-check cracked keys against (listfile) names")
    parser.add_argument("--census", action="store_true",
                        help="crack every block and tally content types")
    parser.add_argument("--limit", type=int, default=None,
                        help="only look at the first N blocks")
    args = parser.parse_args(argv)

    rc = 0
    for path in args.archive:
        if args.validate:
            rc |= _validate(path)
        if args.census or not args.validate:
            _census(path, args.limit)
    return rc


if __name__ == "__main__":
    sys.exit(main())
