"""Stamp a line of text onto the StarCraft 64 title screen.

The title screen is BOLT entry `002/006`, a 640x480 image of 8-bit palette
indices with its palette in the following slot. Because the pixels are indices
rather than colour, text can be drawn without touching the palette at all:
pick indices that already hold the colours you want and write them straight
into the array. Nothing is requantised, nothing else in the image shifts, and
the result cannot introduce a colour the hardware does not have.

The glyphs are an 8x8 bitmap font defined below, scaled by whole pixels. A
font this plain is legible at 2x on a composite-video N64 in a way a
proportional one is not, and it keeps the repository free of font files.

    python title_brand.py --text "LADDER EDITION" -o out.z64

The image lives inside BOLT, which starts past the boot checksum window, so
unlike the Scenario table this needs no checksum repair.
"""

from __future__ import annotations

import argparse
import struct
import sys

from extract_sc64_maps import BoltArchive, load_rom
from sc64 import find_rom

TITLE_IMAGE = "002/006"
IMAGE_HEADER = 16
PALETTE_SIZE = 518
PALETTE_PREFIX = 6

# 8x8 glyphs, one byte per row, MSB is the leftmost pixel. Only the characters
# a title stamp plausibly needs; anything else renders as a space.
FONT: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0, 0),
    "-": (0x00, 0x00, 0x00, 0x7E, 0x00, 0x00, 0x00, 0x00),
    ".": (0, 0, 0, 0, 0, 0x18, 0x18, 0),
    ":": (0, 0x18, 0x18, 0, 0, 0x18, 0x18, 0),
    "A": (0x18, 0x3C, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x00),
    "B": (0x7C, 0x66, 0x66, 0x7C, 0x66, 0x66, 0x7C, 0x00),
    "C": (0x3C, 0x66, 0x60, 0x60, 0x60, 0x66, 0x3C, 0x00),
    "D": (0x78, 0x6C, 0x66, 0x66, 0x66, 0x6C, 0x78, 0x00),
    "E": (0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x7E, 0x00),
    "F": (0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x60, 0x00),
    "G": (0x3C, 0x66, 0x60, 0x6E, 0x66, 0x66, 0x3E, 0x00),
    "H": (0x66, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x66, 0x00),
    "I": (0x3C, 0x18, 0x18, 0x18, 0x18, 0x18, 0x3C, 0x00),
    "J": (0x1E, 0x0C, 0x0C, 0x0C, 0x0C, 0x6C, 0x38, 0x00),
    "K": (0x66, 0x6C, 0x78, 0x70, 0x78, 0x6C, 0x66, 0x00),
    "L": (0x60, 0x60, 0x60, 0x60, 0x60, 0x60, 0x7E, 0x00),
    "M": (0x63, 0x77, 0x7F, 0x6B, 0x63, 0x63, 0x63, 0x00),
    "N": (0x66, 0x76, 0x7E, 0x7E, 0x6E, 0x66, 0x66, 0x00),
    "O": (0x3C, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00),
    "P": (0x7C, 0x66, 0x66, 0x7C, 0x60, 0x60, 0x60, 0x00),
    "Q": (0x3C, 0x66, 0x66, 0x66, 0x6E, 0x6C, 0x36, 0x00),
    "R": (0x7C, 0x66, 0x66, 0x7C, 0x78, 0x6C, 0x66, 0x00),
    "S": (0x3E, 0x60, 0x60, 0x3C, 0x06, 0x06, 0x7C, 0x00),
    "T": (0x7E, 0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0x00),
    "U": (0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00),
    "V": (0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x18, 0x00),
    "W": (0x63, 0x63, 0x63, 0x6B, 0x7F, 0x77, 0x63, 0x00),
    "X": (0x66, 0x66, 0x3C, 0x18, 0x3C, 0x66, 0x66, 0x00),
    "Y": (0x66, 0x66, 0x66, 0x3C, 0x18, 0x18, 0x18, 0x00),
    "Z": (0x7E, 0x06, 0x0C, 0x18, 0x30, 0x60, 0x7E, 0x00),
    "0": (0x3C, 0x66, 0x6E, 0x7E, 0x76, 0x66, 0x3C, 0x00),
    "1": (0x18, 0x38, 0x18, 0x18, 0x18, 0x18, 0x7E, 0x00),
    "2": (0x3C, 0x66, 0x06, 0x0C, 0x18, 0x30, 0x7E, 0x00),
    "3": (0x3C, 0x66, 0x06, 0x1C, 0x06, 0x66, 0x3C, 0x00),
    "4": (0x0C, 0x1C, 0x3C, 0x6C, 0x7E, 0x0C, 0x0C, 0x00),
    "5": (0x7E, 0x60, 0x7C, 0x06, 0x06, 0x66, 0x3C, 0x00),
    "6": (0x1C, 0x30, 0x60, 0x7C, 0x66, 0x66, 0x3C, 0x00),
    "7": (0x7E, 0x06, 0x0C, 0x18, 0x30, 0x30, 0x30, 0x00),
    "8": (0x3C, 0x66, 0x66, 0x3C, 0x66, 0x66, 0x3C, 0x00),
    "9": (0x3C, 0x66, 0x66, 0x3E, 0x06, 0x0C, 0x38, 0x00),
}
GLYPH_W = GLYPH_H = 8


def palette_rgb(data: bytes) -> list[tuple[int, int, int, int]]:
    out = []
    for i in range(256):
        (v,) = struct.unpack_from(">H", data, PALETTE_PREFIX + i * 2)
        r5, g5, b5, a1 = (v >> 11) & 0x1F, (v >> 6) & 0x1F, (v >> 1) & 0x1F, v & 1
        w = lambda c: (c << 3) | (c >> 2)
        out.append((w(r5), w(g5), w(b5), a1))
    return out


def nearest_index(pal, rgb, opaque_only: bool = True) -> int:
    """Palette slot closest to `rgb`, by squared distance."""
    best, best_d = 0, None
    for i, (r, g, b, a) in enumerate(pal):
        if opaque_only and not a:
            continue
        d = (r - rgb[0]) ** 2 + (g - rgb[1]) ** 2 + (b - rgb[2]) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def draw_text(px: bytearray, width: int, height: int, text: str,
              x: int, y: int, scale: int, ink: int, shadow: int | None) -> None:
    """Write glyph pixels straight into the index array."""
    def blit(gx: int, gy: int, rows, colour: int) -> None:
        for ry, bits in enumerate(rows):
            for rx in range(GLYPH_W):
                if not (bits >> (7 - rx)) & 1:
                    continue
                for sy in range(scale):
                    py = gy + ry * scale + sy
                    if not 0 <= py < height:
                        continue
                    base = py * width
                    for sx in range(scale):
                        pxx = gx + rx * scale + sx
                        if 0 <= pxx < width:
                            px[base + pxx] = colour

    for pass_colour, dx, dy in ((shadow, scale, scale), (ink, 0, 0)):
        if pass_colour is None:
            continue
        cx = x
        for ch in text.upper():
            rows = FONT.get(ch, FONT[" "])
            blit(cx + dx, y + dy, rows, pass_colour)
            cx += (GLYPH_W + 1) * scale


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rom", default=None,
                    help="ROM to patch; found automatically if omitted")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--text", default="LADDER EDITION")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--margin", type=int, default=14,
                    help="pixels from the top and right edges")
    ap.add_argument("--colour", default="255,232,80",
                    help="wanted ink colour; the nearest palette slot is used")
    ap.add_argument("--no-shadow", action="store_true")
    ap.add_argument("--entry", default=TITLE_IMAGE)
    a = ap.parse_args(argv)

    rom_path = find_rom(a.rom)
    if rom_path is None:
        sys.exit("no ROM found; pass --rom")
    rom = bytearray(load_rom(rom_path))
    arc = BoltArchive(bytes(rom))

    ent = {e.path: e for e in arc.entries() if e.file_hash != 0 and e.size}
    if a.entry not in ent:
        sys.exit(f"{a.entry} is not in the archive")
    img = arc.read(ent[a.entry])
    _, _, width, height, _ = struct.unpack_from(">IIHHI", img)
    if IMAGE_HEADER + width * height != len(img):
        sys.exit(f"{a.entry} is not an 8-bit image")

    # The palette sits in the slot after the image.
    d, _, idx = a.entry.rpartition("/")
    pal_path = f"{d}/{int(idx, 16) + 1:03X}"
    if pal_path not in ent:
        sys.exit(f"no palette at {pal_path}")
    pal_raw = arc.read(ent[pal_path])
    if len(pal_raw) != PALETTE_SIZE:
        sys.exit(f"{pal_path} is {len(pal_raw)} bytes, not a palette")
    pal = palette_rgb(pal_raw)

    want = tuple(int(v) for v in a.colour.split(","))
    ink = nearest_index(pal, want)
    shadow = None if a.no_shadow else nearest_index(pal, (0, 0, 0))
    print(f"{a.entry}: {width}x{height}, palette {pal_path}")
    print(f"  ink    -> index {ink} rgb{pal[ink][:3]}")
    if shadow is not None:
        print(f"  shadow -> index {shadow} rgb{pal[shadow][:3]}")

    px = bytearray(img[IMAGE_HEADER:])
    text_w = len(a.text) * (GLYPH_W + 1) * a.scale
    x = width - a.margin - text_w
    draw_text(px, width, height, a.text, x, a.margin, a.scale, ink, shadow)

    new_img = img[:IMAGE_HEADER] + bytes(px)
    assert len(new_img) == len(img)          # same shape, so it fits in place
    off = arc.base + ent[a.entry].offset
    if ent[a.entry].flags & 0x08:
        rom[off:off + len(new_img)] = new_img
    else:
        try:
            import bolt_lzss
        except ImportError:
            sys.exit("the entry is compressed; pip install bolt-lzss to rewrite it")
        packed = bolt_lzss.encode(new_img, 3)
        if bolt_lzss.decode(packed, len(new_img)) != new_img:
            sys.exit("compression round trip failed, refusing to write")

        # Prefer writing in place. The tail padding is a shared budget -- the
        # ladder maps want most of it -- and the title happens to re-encode
        # smaller than Mass Media's own stream, so appending would spend
        # ~150 KiB of that budget for nothing. Only the bytes change: the
        # decompressed size is identical, so the directory record stands.
        extent = bolt_lzss.decoded_length(bytes(rom), len(new_img), start=off)
        if len(packed) <= extent:
            rom[off:off + len(packed)] = packed
            print(f"  re-encoded {len(new_img):,} -> {len(packed):,} bytes, "
                  f"in place ({extent:,} available)")
            with open(a.out, "wb") as fh:
                fh.write(rom)
            print(f"wrote {a.out}")
            return 0

        pos = len(rom)
        while pos > 0 and rom[pos - 1] in (0x00, 0xFF):
            pos -= 1
        dest = (pos + 15) & ~15
        if dest + len(packed) > len(rom):
            sys.exit("not enough tail padding for the re-encoded title")
        rom[dest:dest + len(packed)] = packed
        rec = arc.base + _entry_record(arc, a.entry)
        struct.pack_into(">I", rom, rec + 4, len(new_img))
        struct.pack_into(">I", rom, rec + 8, dest - arc.base)
        print(f"  re-encoded {len(new_img):,} -> {len(packed):,} bytes "
              f"at BOLT+{dest - arc.base:#x}")

    with open(a.out, "wb") as fh:
        fh.write(rom)
    print(f"wrote {a.out}")
    return 0


def _entry_record(arc: BoltArchive, path: str) -> int:
    """BOLT-relative offset of the 16-byte directory record for `path`."""
    from extract_sc64_maps import BOLT_ENTRY_SIZE, BOLT_HEADER_SIZE
    table, count = BOLT_HEADER_SIZE, arc.num_entries or 256
    parts = path.split("/")
    for depth, part in enumerate(parts):
        rec = table + int(part, 16) * BOLT_ENTRY_SIZE
        if depth == len(parts) - 1:
            return rec
        e = arc._entry(part, rec)
        table, count = e.offset, (e.file_type or 256)
    raise ValueError(path)


if __name__ == "__main__":
    sys.exit(main())
