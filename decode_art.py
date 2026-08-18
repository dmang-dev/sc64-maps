"""Decode the cartridge's 8-bit artwork into PNGs.

`extract_glue.py --assets` identifies the binary entries -- images, palettes,
font ramps -- but stops at classification. The pixels are palette indices and
the palettes are RGBA5551, so neither is viewable on its own. This pairs them
and writes PNGs.

    image     16-byte header: u32 bpp, u32 0, u16 width, u16 height, u32 0,
              then width*height 8-bit palette indices
    palette   518 bytes: a 6-byte prefix then 256 big-endian RGBA5551 words
    fontramp  48 bytes: 6 font colours x 8 palette indices

RGBA5551 packs a 16-bit word as RRRRRGGG GGBBBBBA -- five bits each of red,
green and blue, then a single alpha bit. Five-bit channels are widened to eight
by replicating the high bits (`v << 3 | v >> 2`), which maps 31 to 255 exactly;
scaling by 255/31 and rounding gives the same answer, but this is what the
hardware does.

Nothing here needs a third-party package. The PNG writer below is about thirty
lines of stdlib zlib, which keeps this repository's "no pip packages" promise
intact -- worth more than the convenience of an image library for a format this
simple.

The artwork is Blizzard's. Decode it from a cartridge you own and keep the
results to yourself.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib

from extract_sc64_maps import BoltArchive, load_rom

IMAGE_HEADER = 16
PALETTE_SIZE = 518
PALETTE_PREFIX = 6
RAMP_SIZE = 48


# --------------------------------------------------------------------------
# Minimal PNG writer
# --------------------------------------------------------------------------

def _chunk(tag: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))


def write_png(path: str, width: int, height: int, rgba: bytes) -> None:
    """Write RGBA8888 rows as a PNG. `rgba` is width*height*4 bytes."""
    if len(rgba) != width * height * 4:
        raise ValueError(f"expected {width * height * 4} bytes, got {len(rgba)}")
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)                                  # filter: none
        raw += rgba[y * stride:(y + 1) * stride]
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + _chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


# --------------------------------------------------------------------------
# Cartridge formats
# --------------------------------------------------------------------------

def decode_palette(data: bytes) -> list[tuple[int, int, int, int]]:
    """518 bytes -> 256 RGBA8888 tuples."""
    if len(data) != PALETTE_SIZE:
        raise ValueError(f"palette must be {PALETTE_SIZE} bytes, got {len(data)}")
    out = []
    for i in range(256):
        (v,) = struct.unpack_from(">H", data, PALETTE_PREFIX + i * 2)
        r5, g5, b5, a1 = (v >> 11) & 0x1F, (v >> 6) & 0x1F, (v >> 1) & 0x1F, v & 1
        widen = lambda c: (c << 3) | (c >> 2)          # 31 -> 255 exactly
        out.append((widen(r5), widen(g5), widen(b5), 255 if a1 else 0))
    return out


def is_image(data: bytes) -> bool:
    """An 8bpp image entry.

    The header word is not a magic number: its LOW half is the depth and its
    HIGH half is a flag. Testing the whole word against 00 00 00 08 therefore
    accepted only flag 0 and silently rejected 41 images -- 9 with flag 1, 6
    with flag 2 and 26 with flag 3, which between them are all of directory
    006 and part of 002 and 009. The archive holds 281 images, not 240.

    What the flag means is not established here. It is not the depth, and the
    size check below still holds for every value of it, so decoding does not
    depend on knowing.
    """
    if len(data) <= IMAGE_HEADER:
        return False
    depth = struct.unpack_from(">I", data)[0] & 0xFFFF
    if depth != 8:
        return False
    _, _, w, h, _ = struct.unpack_from(">IIHHI", data)
    # The size check is what keeps this honest: an entry that merely starts
    # with an 8 is not an image unless its dimensions account for its length.
    return bool(w and h) and IMAGE_HEADER + w * h == len(data)


def decode_image(data: bytes, palette) -> tuple[int, int, bytes]:
    """Paletted image -> (width, height, RGBA8888 bytes)."""
    _, _, w, h, _ = struct.unpack_from(">IIHHI", data)
    px = data[IMAGE_HEADER:]
    out = bytearray(w * h * 4)
    for i, idx in enumerate(px):
        r, g, b, a = palette[idx]
        o = i * 4
        out[o] = r
        out[o + 1] = g
        out[o + 2] = b
        out[o + 3] = a
    return w, h, bytes(out)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def collect(arc: BoltArchive):
    """(images, palettes) as {bolt_path: bytes}, in archive order."""
    images, palettes = {}, {}
    for e in arc.entries():
        if e.file_hash == 0 or not e.size:
            continue
        try:
            data = arc.read(e)
        except Exception:
            continue
        if is_image(data):
            images[e.path] = data
        elif len(data) == PALETTE_SIZE:
            palettes[e.path] = data
    return images, palettes


def directory_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _next_slot(path: str) -> str | None:
    d, _, idx = path.rpartition("/")
    try:
        return f"{d}/{int(idx, 16) + 1:03X}"
    except ValueError:
        return None


def _slot_num(path: str) -> int:
    try:
        return int(path.rsplit("/", 1)[1], 16)
    except (IndexError, ValueError):
        return -1


def _runs(paths: list[str]) -> list[list[str]]:
    """Split into maximal runs of consecutive slot numbers."""
    out: list[list[str]] = []
    for p in paths:
        if out and _slot_num(p) == _slot_num(out[-1][-1]) + 1:
            out[-1].append(p)
        else:
            out.append([p])
    return out


# Two directories hold images and no palette at all -- 91 in 000 and 23 in 007,
# very nearly half the archive, and the reason this tool used to decode 46 of
# 240. Their palettes live in directory 006, which holds 16 palettes and no
# images of its own.
#
# Neither was found by a statistic. Spatial-coherence scoring was tried and
# fails outright: it ranks palettes that render almost everything black at the
# top, because one flat colour is perfectly coherent, and on a known-good
# pairing (008 image with its own palette) the correct answer only came fifth
# of 142. What settled it was decoding against every candidate and looking:
# 007 is the campaign briefing cast -- the Adjutant, Raynor, Kerrigan, Mengsk,
# DuGalle, cerebrates -- and 000 is the unit and building renders, both
# unmistakable when the palette is right and noise when it is not.
#
# 006/030 differs from 006/004 in exactly one of its 256 entries, and 006/035
# is likewise interchangeable with 006/02B, so the choice between them is
# cosmetic.
EXTERNAL_PALETTES = {
    "000": "006/02B",       # unit and building renders
    "007": "006/004",       # briefing portraits
}


def pair_palettes(images, palettes) -> dict[str, str | None]:
    """{image path: palette path or None}, resolved per directory.

    Four layouts occur, and all four fall out of one pass:

      adjacent    003 (image, palette, font ramp), 009 (image, palette)
                  -- the palette sits in the slot right after its image
      parallel    008: 36 images at 068..08B then 36 palettes at 08C..0AF,
                  paired by position. 004 is three adjacent pairs followed by
                  a parallel block, and 002 is a mixture of both
      shared      005: six images and a single palette between them
      external    000 and 007, which carry no palette -- see above

    Pairing works on RUNS rather than on individual entries. Taking whatever
    palette sat in the next slot went wrong at a block boundary: in 004 the
    last image of a 39-image run sits immediately before the 39-palette block,
    claimed its first palette, and rotated every other pairing by one. That
    still decoded -- to 39 pictures of noise.
    """
    groups: dict[str, dict[str, list[str]]] = {}
    for p in images:
        groups.setdefault(directory_of(p), {"img": [], "pal": []})["img"].append(p)
    for p in palettes:
        groups.setdefault(directory_of(p), {"img": [], "pal": []})["pal"].append(p)

    out: dict[str, str | None] = {}
    for d, g in groups.items():
        imgs, pals = g["img"], g["pal"]
        if not imgs:
            continue

        ext = EXTERNAL_PALETTES.get(d)
        if ext and ext in palettes:
            for i in imgs:
                out[i] = ext
            continue

        if len(pals) == 1:
            for i in imgs:
                out[i] = pals[0]
            continue

        img_runs = _runs(imgs)
        pal_runs = _runs(pals)
        used_runs: set[int] = set()

        # A lone image followed by a palette is an adjacent pair. A RUN of
        # images is a parallel array and takes a whole palette run.
        for run in img_runs:
            if len(run) == 1 and _next_slot(run[0]) in palettes:
                nxt = _next_slot(run[0])
                for n, pr in enumerate(pal_runs):
                    if nxt in pr and len(pr) == 1:
                        out[run[0]] = nxt
                        used_runs.add(n)
                        break

        for run in img_runs:
            if all(i in out for i in run):
                continue
            # Prefer the nearest unused palette run that can cover it, looking
            # forward first: 004 and 008 put the palettes after their images,
            # 002 puts one block before.
            best = None
            for n, pr in enumerate(pal_runs):
                if n in used_runs or len(pr) < len(run):
                    continue
                after = _slot_num(pr[0]) > _slot_num(run[-1])
                dist = abs(_slot_num(pr[0]) - _slot_num(run[-1]))
                key = (0 if after else 1, dist)
                if best is None or key < best[0]:
                    best = (key, n)
            if best is None:
                continue
            n = best[1]
            used_runs.add(n)
            for i, p in zip(run, pal_runs[n]):
                out[i] = p

        # Anything still unpaired takes what is left, in order.
        left_p = [p for p in pals if p not in set(out.values())]
        for i, p in zip([i for i in imgs if i not in out], left_p):
            out[i] = p
        for i in imgs:
            out.setdefault(i, None)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Decode StarCraft 64 artwork into PNGs.",
        epilog="The artwork is Blizzard's. Keep the output to yourself.")
    ap.add_argument("rom")
    ap.add_argument("-o", "--out", default="art")
    ap.add_argument("--palette",
                    help="BOLT path of a palette to use for every image, "
                         "instead of pairing by directory")
    ap.add_argument("-l", "--list", action="store_true",
                    help="report what was found and exit")
    a = ap.parse_args(argv)

    arc = BoltArchive(load_rom(a.rom))
    images, palettes = collect(arc)
    print(f"{len(images)} images, {len(palettes)} palettes")

    pairing = pair_palettes(images, palettes)

    by_dir: dict[str, list[str]] = {}
    for p in palettes:
        by_dir.setdefault(directory_of(p), []).append(p)

    def palette_for(img_path: str) -> bytes | None:
        chosen = pairing.get(img_path)
        return palettes[chosen] if chosen else None

    if a.list:
        for d in sorted(set(map(directory_of, images)) | set(by_dir)):
            imgs = [p for p in images if directory_of(p) == d]
            if not imgs:
                continue
            pals = by_dir.get(d, [])
            srcs = {directory_of(pairing[i]) for i in imgs if pairing.get(i)}
            unpaired = sum(1 for i in imgs if not pairing.get(i))
            where = ",".join(sorted(srcs)) or "-"
            print(f"  {d or '(root)':8} {len(imgs):4} images  {len(pals):3} palettes"
                  f"   palettes from {where:8}"
                  + (f"   {unpaired} UNPAIRED" if unpaired else ""))
        total = sum(1 for i in images if not pairing.get(i))
        print(f"  {len(images) - total}/{len(images)} images have a palette")
        return 0

    os.makedirs(a.out, exist_ok=True)
    forced = decode_palette(arc.read(
        next(e for e in arc.entries() if e.path == a.palette))) if a.palette else None

    written = skipped = 0
    for path, data in images.items():
        if forced is not None:
            pal = forced
        else:
            raw = palette_for(path)
            if raw is None:
                skipped += 1
                continue
            pal = decode_palette(raw)
        w, h, rgba = decode_image(data, pal)
        name = path.replace("/", "_") + f"_{w}x{h}.png"
        write_png(os.path.join(a.out, name), w, h, rgba)
        written += 1

    print(f"wrote {written} PNGs to {a.out}/")
    if skipped:
        print(f"skipped {skipped} whose directory has no single obvious "
              f"palette -- pass --palette to force one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
