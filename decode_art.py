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

IMAGE_MAGIC = b"\x00\x00\x00\x08"
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
    if len(data) <= IMAGE_HEADER or data[:4] != IMAGE_MAGIC:
        return False
    _, _, w, h, _ = struct.unpack_from(">IIHHI", data)
    return IMAGE_HEADER + w * h == len(data)


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

    # Two pairing shapes occur, and both are visible in the archive order.
    #
    #   alternating   009/000 image, 009/001 palette, 009/002 image, ...
    #                 -- each image is followed by its own palette
    #   shared        005/000..005 images, then 005/006 the one palette
    #
    # So: prefer the palette in the slot immediately after the image, and fall
    # back to the directory's single palette. Anything else is reported rather
    # than guessed at -- the wrong palette yields a picture that looks
    # plausible and is wrong, which is worse than no picture.
    by_dir: dict[str, list[str]] = {}
    for p in palettes:
        by_dir.setdefault(directory_of(p), []).append(p)

    def palette_for(img_path: str) -> bytes | None:
        d, _, idx = img_path.rpartition("/")
        try:
            nxt = f"{d}/{int(idx, 16) + 1:03X}"
        except ValueError:
            nxt = None
        if nxt and nxt in palettes:
            return palettes[nxt]
        cands = by_dir.get(d, [])
        return palettes[cands[0]] if len(cands) == 1 else None

    if a.list:
        for d in sorted(set(map(directory_of, images)) | set(by_dir)):
            imgs = [p for p in images if directory_of(p) == d]
            pals = by_dir.get(d, [])
            print(f"  {d or '(root)':10} {len(imgs):4} images  "
                  f"{len(pals):2} palettes"
                  + ("" if len(pals) == 1 or not imgs else "   <- ambiguous"))
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
