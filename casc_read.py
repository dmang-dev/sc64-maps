#!/usr/bin/env python3
"""
casc_read.py -- read files out of a local Blizzard CASC storage.

    python casc_read.py "I:/Blizzard/StarCraft" --info
    python casc_read.py "I:/Blizzard/StarCraft" --list "*campaign/Terran*"
    python casc_read.py "I:/Blizzard/StarCraft" \
        --extract locales/enUS/Assets/campaign/Terran/Terran01/staredit/scenario.chk \
        -o terran01.chk
    python casc_read.py "I:/Blizzard/StarCraft" --survey

CASC replaced MPQ in Blizzard's modern products. StarCraft: Remastered keeps
its campaign maps here rather than in the legacy MPQs, and every game after
StarCraft II uses it, so this is the way in to anything newer.

Structure, and the chain needed to reach one file:

    .build.info             pipe-separated, gives the build config's MD5
    Data/config/xx/yy/hash  build config: names the encoding and root files
    Data/data/*.idx         16 buckets mapping an EKey prefix -> (archive,
                            offset, encoded size).  NOTE: these live next to
                            the archives in Data/data, NOT in Data/indices --
                            that directory holds CDN-style .index files, which
                            a local (non-online) storage never consults.
    Data/data/data.NNN      the archives themselves, holding BLTE blobs, each
                            preceded by a 30-byte header span (CascLib's
                            BLTE_HEADER_DELTA) whose first 16 bytes are the
                            blob's EKey stored back-to-front
    ENCODING                fetched by EKey from the build config; maps
                            CKey -> EKey
    ROOT                    fetched by CKey; maps a file name -> CKey

So: name -> CKey (root) -> EKey (encoding) -> archive/offset (.idx) -> BLTE.

BLTE is the container each blob is wrapped in; its frames may be stored,
zlib-compressed, LZ4, recursive, or Salsa20-encrypted.

Structures follow the vendored reference/CascLib (MIT) rather than guesswork:
CascIndexFiles.cpp for the .idx layout, CascStructs.h for the on-disk records,
CascOpenStorage.cpp for the ENCODING manifest, CascRootFile_Text.cpp for the
StarCraft I root.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import collections
import fnmatch
import hashlib
import os
import struct
import sys
import zlib


MD5_SIZE = 16

# CascStructs.h: BLTE_ENCODED_HEADER -- 16-byte EKey, 4-byte encoded size,
# two flag bytes, a Jenkins hash and a checksum, all ahead of the "BLTE" magic.
BLTE_HEADER_DELTA = 0x1E


class CascError(Exception):
    """Anything structurally wrong with the storage."""


class EncryptedFrame(CascError):
    """A BLTE frame is Salsa20-encrypted and the key is not in the storage."""

    def __init__(self, key_name: str = ""):
        super().__init__(f"encrypted BLTE frame (key {key_name or 'unknown'})")
        self.key_name = key_name


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


def blte_chunks(data: bytes) -> list[tuple[int, int]]:
    """Return the (packed_size, plain_size) chunk table of a BLTE blob.

    An empty list means the blob has no chunk table: the whole remainder past
    the 8-byte header is one implicit frame.
    """
    if data[:4] != BLTE_MAGIC:
        raise CascError(f"not BLTE (magic {data[:4]!r})")
    header_size = struct.unpack_from(">I", data, 4)[0]
    if header_size == 0:
        return []
    must_be_0f = data[8]
    if must_be_0f != 0x0F:
        raise CascError(f"BLTE chunk table flag is {must_be_0f:#04x}, want 0x0f")
    count = int.from_bytes(data[9:12], "big")
    if 12 + count * 24 != header_size:
        raise CascError(f"BLTE header size {header_size} does not match "
                        f"{count} chunks")
    out = []
    for i in range(count):
        packed, plain = struct.unpack_from(">II", data, 12 + i * 24)
        out.append((packed, plain))       # the 16-byte MD5 that follows is
    return out                            # not verified here


def blte_decode(data: bytes, key_lookup=None) -> bytes:
    """Decode a BLTE blob into its plain contents.

    Frame modes: 'N' stored, 'Z' zlib, '4' LZ4, 'F' recursive BLTE,
    'E' encrypted (Salsa20 -- needs a key we usually do not have).
    """
    chunks = blte_chunks(data)
    if not chunks:
        # Single implicit frame covering the rest of the blob.
        return _blte_frame(data[8:], key_lookup)

    header_size = struct.unpack_from(">I", data, 4)[0]
    out = bytearray()
    offset = header_size
    for packed, _plain in chunks:
        out += _blte_frame(data[offset:offset + packed], key_lookup)
        offset += packed
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
        # CascDecrypt.cpp: key-name length byte, then the little-endian key
        # name, then an IV.  We only surface the name so callers can say which
        # key is missing; breaking the cipher is out of scope.
        name = ""
        if body:
            n = body[0]
            if 0 < n <= 8 and len(body) >= 1 + n:
                name = body[1:1 + n][::-1].hex().upper()
        raise EncryptedFrame(name)
    raise CascError(f"unknown BLTE frame mode {mode!r}")


# --------------------------------------------------------------------------
# .idx bucket files  (CascIndexFiles.cpp)
# --------------------------------------------------------------------------

IDX_ENTRIES_OFFSET = 32     # HeaderLength (8 guard + 16 header) + HeaderPadding


class IdxHeader:
    """Normalised CASC_INDEX_HEADER for the '##########.idx' (version 2) form."""

    __slots__ = ("revision", "bucket", "flags", "span_size_bytes",
                 "span_offset_bytes", "key_bytes", "segment_bits",
                 "max_file_offset", "entry_length")

    def __init__(self, data: bytes):
        # FILE_INDEX_GUARDED_BLOCK {BlockSize, BlockHash} precedes the header.
        # The 32-bit Jenkins hashes are not recomputed here; the structural
        # constraints below are enough to reject a wrong layout.
        (self.revision, self.bucket, self.flags, self.span_size_bytes,
         self.span_offset_bytes, self.key_bytes,
         self.segment_bits) = struct.unpack_from("<HBBBBBB", data, 8)
        self.max_file_offset = struct.unpack_from("<Q", data, 16)[0]
        self.entry_length = (self.key_bytes + self.span_offset_bytes
                             + self.span_size_bytes)


def parse_idx(data: bytes, bucket: int) -> tuple[IdxHeader, dict]:
    """Parse one .idx bucket into {ekey_prefix: (archive, offset, size)}."""
    hdr = IdxHeader(data)
    if hdr.revision != 0x07:
        raise CascError(f"idx revision {hdr.revision}, only 7 is implemented")
    if hdr.bucket != bucket:
        raise CascError(f"idx says bucket {hdr.bucket}, file name says {bucket}")
    if hdr.flags != 0:
        raise CascError(f"idx flags {hdr.flags:#04x}, only 0 is implemented")
    if (hdr.span_size_bytes, hdr.span_offset_bytes, hdr.key_bytes) != (4, 5, 9):
        raise CascError("unexpected idx field widths "
                        f"{hdr.span_size_bytes}/{hdr.span_offset_bytes}/"
                        f"{hdr.key_bytes}")

    # LoadIndexFile_V2 first tries a single guarded block holding a contiguous
    # array of EKey entries; that is the shape every bucket of this storage
    # uses.  (CascLib's page-scattered fallback at 0x1000 is not implemented.)
    block_size = struct.unpack_from("<I", data, IDX_ENTRIES_OFFSET)[0]
    pos = IDX_ENTRIES_OFFSET + 8
    if block_size == 0 or block_size % hdr.entry_length or \
            pos + block_size > len(data):
        raise CascError(f"idx bucket {bucket:02x}: no contiguous EKey block "
                        f"(block size {block_size})")

    key_n, off_n = hdr.key_bytes, hdr.span_offset_bytes
    mask = (1 << hdr.segment_bits) - 1
    out = {}
    for _ in range(block_size // hdr.entry_length):
        entry = data[pos:pos + hdr.entry_length]
        pos += hdr.entry_length
        # FILE_EKEY_ENTRY: EKey[9], FileOffsetBE[5], EncodedSize[4].
        # CopyEKeyEntry() reads the offset big-endian and the size LITTLE-endian.
        storage_offset = int.from_bytes(entry[key_n:key_n + off_n], "big")
        size = int.from_bytes(entry[key_n + off_n:], "little")
        out[entry[:key_n]] = (storage_offset >> hdr.segment_bits,
                              storage_offset & mask, size)
    return hdr, out


def _newest_idx_files(data_dir: str) -> dict:
    """Pick one .idx per bucket -- the highest version, as CascLib does."""
    best = {}
    for name in os.listdir(data_dir):
        if not name.lower().endswith(".idx") or len(name) != 14:
            continue
        try:
            bucket = int(name[0:2], 16)
            version = int(name[2:10], 16)
        except ValueError:
            continue
        if bucket >= 16:
            continue
        if bucket not in best or version > best[bucket][0]:
            best[bucket] = (version, os.path.join(data_dir, name))
    return {b: p for b, (_v, p) in best.items()}


# --------------------------------------------------------------------------
# ENCODING manifest  (CascOpenStorage.cpp / CascStructs.h)
# --------------------------------------------------------------------------

ENCODING_HEADER_SIZE = 22       # sizeof(FILE_ENCODING_HEADER), packed


def parse_encoding(data: bytes) -> tuple[dict, dict]:
    """Parse the ENCODING manifest into ({ckey: (size, [ekey, ...])}, header)."""
    if data[:2] != b"EN" or data[2] != 0x01:
        raise CascError(f"not an ENCODING manifest (magic {data[:3]!r})")
    ckey_len, ekey_len = data[3], data[4]
    if ckey_len != MD5_SIZE or ekey_len != MD5_SIZE:
        raise CascError(f"unsupported key lengths {ckey_len}/{ekey_len}")

    info = {
        "ckey_page_size": int.from_bytes(data[5:7], "big") * 1024,
        "ekey_page_size": int.from_bytes(data[7:9], "big") * 1024,
        "ckey_page_count": int.from_bytes(data[9:13], "big"),
        "ekey_page_count": int.from_bytes(data[13:17], "big"),
        "espec_block_size": int.from_bytes(data[18:22], "big"),
    }

    # FILE_CKEY_PAGE[] (32 bytes each: first key + segment MD5) then the pages.
    page = (ENCODING_HEADER_SIZE + info["espec_block_size"]
            + info["ckey_page_count"] * 32)
    stride = 6 + ckey_len                    # EKeyCount + ContentSize + CKey
    table = {}
    for _ in range(info["ckey_page_count"]):
        end = page + info["ckey_page_size"]
        if end > len(data):
            raise CascError("ENCODING truncated")
        pos = page
        while pos + stride <= end:
            # FILE_CKEY_ENTRY.EKeyCount is a native USHORT, i.e. little-endian;
            # ContentSize is an explicit big-endian BYTE[4].
            count = int.from_bytes(data[pos:pos + 2], "little")
            if count == 0:
                break
            size = int.from_bytes(data[pos + 2:pos + 6], "big")
            ckey = data[pos + 6:pos + 6 + ckey_len]
            base = pos + stride
            table[ckey] = (size, [data[base + i * ekey_len:
                                       base + (i + 1) * ekey_len]
                                  for i in range(count)])
            pos = base + count * ekey_len
        page = end
    return table, info


# --------------------------------------------------------------------------
# The storage
# --------------------------------------------------------------------------

class CascStorage:
    """A local CASC storage, opened read-only.

    Loading walks .build.info -> build config -> .idx buckets -> ENCODING ->
    ROOT.  For StarCraft the root is the plain-text 'name|ckey' listing that
    CascRootFile_Text.cpp calls TRootHandler_SC1; other products use TVFS,
    MNDX or the WoW binary roots, none of which are implemented here.
    """

    def __init__(self, root: str, verbose: bool = False):
        self.root = root
        self.data_dir = os.path.join(root, "Data", "data")
        self._archives = {}
        self.warnings = []

        rows = parse_build_info(root)
        self.build = next((r for r in rows if r.get("Active") == "1"), rows[0])
        self.build_key = self.build.get("Build Key", "")
        self.config = parse_config(config_path(root, self.build_key))

        self.index = {}
        self.idx_header = None
        for bucket, path in sorted(_newest_idx_files(self.data_dir).items()):
            with open(path, "rb") as fh:
                hdr, entries = parse_idx(fh.read(), bucket)
            self.idx_header = hdr
            self.index.update(entries)
        if not self.index:
            raise CascError(f"no usable .idx files in {self.data_dir}")
        self.key_bytes = self.idx_header.key_bytes

        encoding_ekey = self.config["encoding"][1]
        self.encoding, self.encoding_info = parse_encoding(
            self.read_ekey(encoding_ekey))

        self.names = {}                     # lowercased name -> ckey
        self.name_list = []                 # names as the root spells them
        self._load_root(verbose)

    # -- root ---------------------------------------------------------------

    def _load_root(self, verbose: bool) -> None:
        ckey = bytes.fromhex(self.config["root"][0])
        data = self.read_ckey(ckey)
        # TRootHandler_SC1::IsRootFile -- CSV with 2 or 3 columns whose second
        # column is a 32-character MD5 string.
        first = data.split(b"\n", 1)[0].strip()
        if first.count(b"|") != 1 or len(first.split(b"|")[1]) != 32:
            raise CascError("root is not the StarCraft text format "
                            f"(first line {first[:64]!r})")
        text = data.decode("utf-8", "replace")
        for line in text.replace("\r\n", "\n").split("\n"):
            if "|" not in line:
                continue
            name, _, key = line.partition("|")
            key = key.strip()
            if len(key) != 32:
                continue
            self.name_list.append(name)
            self.names[name.lower().replace("\\", "/")] = bytes.fromhex(key)
        if verbose:
            print(f"root: {len(self.name_list)} names", file=sys.stderr)

    # -- raw reads ----------------------------------------------------------

    def _archive(self, number: int):
        fh = self._archives.get(number)
        if fh is None:
            fh = open(os.path.join(self.data_dir, "data.%03u" % number), "rb")
            self._archives[number] = fh
        return fh

    def locate(self, ekey) -> tuple[int, int, int] | None:
        """EKey -> (archive number, offset in data.NNN, encoded size)."""
        if isinstance(ekey, str):
            ekey = bytes.fromhex(ekey)
        return self.index.get(ekey[:self.key_bytes])

    def is_free_space(self, ekey) -> bool:
        """True for the bookkeeping entries that carry no payload.

        Every (bucket, archive) pair gets one 30-byte record whose whole body
        is the header span -- BLTE_ENCODED_HEADER::field_14 is 1, meaning "the
        header span has no data".  They are not in ENCODING and no root name
        points at them.
        """
        found = self.locate(ekey)
        return found is not None and found[2] <= BLTE_HEADER_DELTA

    def read_blob(self, ekey) -> bytes:
        """The BLTE blob for an EKey, header span stripped."""
        found = self.locate(ekey)
        if found is None:
            key = ekey if isinstance(ekey, str) else bytes(ekey).hex()
            raise CascError(f"EKey {key} is not in any local .idx bucket")
        number, offset, size = found
        if size <= BLTE_HEADER_DELTA:
            raise CascError(f"data.{number:03d}+{offset} is a {size}-byte "
                            "free-space marker, not a file")
        fh = self._archive(number)
        fh.seek(offset)
        blob = fh.read(size)
        if blob[:4] == BLTE_MAGIC:
            return blob
        if blob[BLTE_HEADER_DELTA:BLTE_HEADER_DELTA + 4] == BLTE_MAGIC:
            return blob[BLTE_HEADER_DELTA:]
        raise CascError(f"no BLTE magic at data.{number:03d}+{offset}")

    def read_ekey(self, ekey) -> bytes:
        return blte_decode(self.read_blob(ekey))

    def read_ckey(self, ckey) -> bytes:
        """Resolve a CKey through ENCODING and decode it."""
        if isinstance(ckey, str):
            ckey = bytes.fromhex(ckey)
        entry = self.encoding.get(ckey) if self.encoding else None
        if entry is None:
            # ENCODING itself is fetched before the table exists.
            raise CascError(f"CKey {ckey.hex()} is not in ENCODING")
        _size, ekeys = entry
        last = None
        for ekey in ekeys:
            if self.locate(ekey) is not None:
                return self.read_ekey(ekey)
            last = ekey
        raise CascError(f"CKey {ckey.hex()}: none of its {len(ekeys)} EKey(s) "
                        f"are stored locally (e.g. {last.hex() if last else '-'})")

    # -- by name ------------------------------------------------------------

    def resolve(self, name: str) -> bytes | None:
        return self.names.get(name.lower().replace("\\", "/"))

    def read_file(self, name: str) -> bytes:
        ckey = self.resolve(name)
        if ckey is None:
            raise CascError(f"{name!r} is not in the root listing")
        data = self.read_ckey(ckey)
        digest = hashlib.md5(data).digest()
        if digest != ckey:
            self.warnings.append(f"{name}: MD5 {digest.hex()} != CKey {ckey.hex()}")
        return data

    # -- diagnostics --------------------------------------------------------

    def frame_modes(self, ekey) -> list[bytes]:
        """Frame mode bytes of one blob, read without decoding the payload."""
        found = self.locate(ekey)
        if found is None:
            return []
        number, offset, size = found
        if size <= BLTE_HEADER_DELTA:
            return []                       # free-space marker
        fh = self._archive(number)
        fh.seek(offset)
        head = fh.read(BLTE_HEADER_DELTA + 12)
        base = offset
        if head[:4] != BLTE_MAGIC:
            head = head[BLTE_HEADER_DELTA:]
            base += BLTE_HEADER_DELTA
        if head[:4] != BLTE_MAGIC:
            raise CascError(f"no BLTE magic at data.{number:03d}+{offset}")
        header_size = struct.unpack_from(">I", head, 4)[0]
        if header_size == 0:
            fh.seek(base + 8)
            return [fh.read(1)]
        count = int.from_bytes(head[9:12], "big")
        if head[8] != 0x0F or 12 + count * 24 != header_size \
                or base + header_size > offset + size:
            raise CascError(f"bad BLTE chunk table at data.{number:03d}+{offset}")
        fh.seek(base + 12)
        table = fh.read(count * 24)
        modes = []
        pos = base + header_size
        for i in range(count):
            packed = struct.unpack_from(">I", table, i * 24)[0]
            fh.seek(pos)
            modes.append(fh.read(1))
            pos += packed
        return modes

    def close(self) -> None:
        for fh in self._archives.values():
            fh.close()
        self._archives.clear()


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
    index = sorted(os.listdir(idx_dir)) if os.path.isdir(idx_dir) else []
    print(f"\narchives    : {len(archives)}  {archives[:3]}")
    print(f".idx buckets: {len(idx)}  {idx[:3]}")
    print(f".index files: {len(index)}  {index[:2]}")
    total = sum(os.path.getsize(os.path.join(data_dir, a)) for a in archives)
    print(f"archive size: {total / 2**30:.2f} GiB")

    store = CascStorage(root)
    hdr = store.idx_header
    print(f"\nidx header  : revision {hdr.revision} key {hdr.key_bytes}B "
          f"offset {hdr.span_offset_bytes}B size {hdr.span_size_bytes}B "
          f"segment_bits {hdr.segment_bits} max_file_offset "
          f"{hdr.max_file_offset:#x}")
    print(f"idx entries : {len(store.index)} EKeys stored locally")
    info = store.encoding_info
    print(f"ENCODING    : {len(store.encoding)} CKeys, "
          f"{info['ckey_page_count']} pages of {info['ckey_page_size']} bytes, "
          f"espec block {info['espec_block_size']} bytes")
    print(f"ROOT        : text format (TRootHandler_SC1), "
          f"{len(store.name_list)} names")
    store.close()
    return 0


def do_list(root: str, pattern: str | None, limit: int) -> int:
    store = CascStorage(root)
    names = store.name_list
    if pattern:
        pat = pattern.lower()
        names = [n for n in names if fnmatch.fnmatch(n.lower(), pat)]
        print(f"{len(names)} name(s) match {pattern!r}")
    else:
        print(f"{len(names)} name(s) in the root")
        tops = collections.Counter(n.split("/")[0] for n in names)
        exts = collections.Counter(os.path.splitext(n)[1].lower() for n in names)
        print("top-level : " + ", ".join(f"{k}={v}" for k, v in tops.most_common(12)))
        print("extensions: " + ", ".join(f"{k or '(none)'}={v}"
                                         for k, v in exts.most_common(12)))
    for name in sorted(names)[:limit]:
        print(name)
    if len(names) > limit:
        print(f"... {len(names) - limit} more (raise --limit)")
    store.close()
    return 0


def do_extract(root: str, name: str, out: str | None) -> int:
    store = CascStorage(root)
    try:
        data = store.read_file(name)
    except CascError as exc:
        print(f"error: {exc}", file=sys.stderr)
        store.close()
        return 1
    for warning in store.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    ckey = store.resolve(name)
    print(f"{name}")
    print(f"  ckey  {ckey.hex()}")
    print(f"  size  {len(data)} bytes")
    print(f"  head  {data[:32].hex(' ')}")
    if out:
        with open(out, "wb") as fh:
            fh.write(data)
        print(f"  wrote {out}")
    store.close()
    return 0


def do_survey(root: str) -> int:
    """Count BLTE frame modes across every locally stored blob."""
    store = CascStorage(root)
    ekey_to_name = {}
    for name in store.name_list:
        entry = store.encoding.get(store.resolve(name))
        if entry:
            for ekey in entry[1]:
                ekey_to_name.setdefault(ekey[:store.key_bytes], name)

    modes = collections.Counter()
    other = collections.Counter()
    encrypted = []
    scanned = free = 0
    for prefix in store.index:
        if store.is_free_space(prefix):
            free += 1
            continue
        try:
            found = store.frame_modes(prefix)
        except (CascError, struct.error):
            # Not every blob is BLTE: TACT patch manifests ('PA') sit raw
            # behind the header span.  Record their magic instead of guessing.
            number, offset, _size = store.locate(prefix)
            fh = store._archive(number)
            fh.seek(offset + BLTE_HEADER_DELTA)
            other[fh.read(2)] += 1
            continue
        scanned += 1
        modes.update(found)
        if b"E" in found:
            encrypted.append(ekey_to_name.get(prefix, prefix.hex()))
    print(f"index entries : {len(store.index)}")
    print(f"blobs scanned : {scanned} ({free} free-space markers skipped)")
    print(f"frames        : {sum(modes.values())}")
    print("frame modes   : " + ", ".join(
        f"{k.decode('latin-1')}={v}" for k, v in sorted(modes.items())))
    if other:
        print("non-BLTE      : " + ", ".join(
            f"{k!r}={v}" for k, v in sorted(other.items())))
    print(f"encrypted     : {len(encrypted)} blob(s) with an 'E' frame")
    for name in sorted(encrypted)[:20]:
        print(f"  {name}")
    store.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read a local CASC storage.")
    parser.add_argument("root", help="game install directory (holds .build.info)")
    parser.add_argument("--info", action="store_true",
                        help="describe the storage and exit")
    parser.add_argument("--list", nargs="?", const="", metavar="GLOB",
                        help="enumerate the names the root exposes")
    parser.add_argument("--extract", metavar="NAME",
                        help="extract one file by its root name")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="write --extract output here")
    parser.add_argument("--survey", action="store_true",
                        help="count BLTE frame modes over every local blob")
    parser.add_argument("--limit", type=int, default=50,
                        help="how many names --list prints (default 50)")
    args = parser.parse_args(argv)

    if not os.path.exists(os.path.join(args.root, ".build.info")):
        print(f"error: no .build.info in {args.root!r}", file=sys.stderr)
        return 1

    try:
        if args.extract:
            return do_extract(args.root, args.extract, args.output)
        if args.list is not None:
            return do_list(args.root, args.list or None, args.limit)
        if args.survey:
            return do_survey(args.root)
        return describe(args.root)
    except CascError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
