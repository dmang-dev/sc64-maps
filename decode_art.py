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

    The flag is not arbitrary -- it sorts the archive into four classes that
    line up with the KIND of image. Measured across all 281, each against its
    own paired palette:

        flag 0   240 images, everywhere
                 the general case. Index 0 is alpha-transparent in 151 of
                 them; median index-0 share 0.194.
        flag 1     9 images, 002 and 006
                 large opaque plates and nothing else -- five 640x480
                 backdrops (006/000, 01B, 02A, 036, 038), 002/023 at 640x480,
                 and the three 320x166 message screens (002/017, 019, 01B).
                 Median index-0 share 0.002: they barely reference index 0,
                 because nothing is meant to show through.
        flag 2     6 images, all 009
                 the grids and region maps. See below.
        flag 3    26 images, all 006
                 the UI overlay pieces. Median index-0 share 0.352, and the
                 paired palette's index 0 is OPAQUE BRIGHT GREEN (0,255,0) in
                 25 of the 26 -- a chroma key rather than an alpha channel.

    Every other field in the header is constant across all 281 -- the depth is
    always 8, and the words at +4 and +12 never vary -- so the flag is the only
    thing separating these classes.

    The obvious reading of that table is transparency handling: none for 1,
    chroma key on index 0 for 3, palette alpha for 0. IT IS WRONG, or at least
    it has no effect on drawing. Tested directly: 009/002 is a campaign loading
    screen and 87% of its pixels are index 0, so keying that index out would
    erase almost the whole picture. Rebuilt twice, identical but for the two
    header bytes, flag 0 against flag 3, and shown on the same screen by poking
    the episode byte at 0x800D1182 to 1 (the selector at 0x800228C4 computes
    image = 0x900 + 2 * episode, so that is what chooses which of the eight
    appears). Both arms matched the reference decode at 1.03 -- a real match,
    where a miss scores about 32 -- and differed from each other by 0.00 of
    255. Not one pixel.

    So the flag does not change how a full-screen image is drawn. The code
    that reads it says why: it is a BITFIELD, not the four-valued enum the
    table above makes it look like.

    Found by watching reads of a decompressed image in RDRAM. 009/00C lands at
    0x801C4460 -- the selector at 0x800228F8 leaves that pointer in v0 -- and
    of the PCs that touch its header, most also read the pixels and are bulk
    copies. Seven read the header and never the pixel data, and one of those
    is the interpreter:

        0x800969B0   lhu t8, 0(t7)          t8 = the halfword at +0

    t7 is the image: the same function reads +8 and +10 off it, which are the
    width and height. What follows tests individual bits, never the whole
    value:

        0x800969FC   andi v0, t8, 0x0001    bit 0, gates ~99 instructions
                                            that walk a list at 0x80111C40
        0x80096C30   andi v0, t8, 0x0002    bit 1, gates code that uses the
        0x80096C58   andi v0, t8, 0x0002    halfword at +2 as a table index
        0x80096C90   andi v0, t8, 0x0002    (sll by 2, add base, lw)
        0x80096C0C   andi a2, t8, 0x1100    bits 8 and 12
        0x80096CA0   andi v0, t8, 0x1000    bit 12

    So "flag 3" is not a third kind of image -- it is bits 0 and 1 both set,
    which is exactly why its 26 entries look like a blend of the flag-1 and
    flag-2 populations. Bits 8 and 12 are tested but no image in this cartridge
    sets them.

    Worth knowing before anyone reads the classes above as four separate
    things: they are two independent bits.

    The function holding that read is 0x80096948, 2904 bytes, and it is a
    RASTER BLITTER. It reads a descriptor's bits at +0, a code at +2, signed
    offsets at +4 and +6 and dimensions at +8 and +10; it reads the same six
    fields from a SECOND descriptor and compares them pairwise, falling back
    to a straight memcpy (0x80091300) when they agree; it range-checks a code
    against 9 and 15; and it dispatches to eight specialised routines at
    0x8009D790, D7DC, D82C, D88C, D8FC, D9FC, DABC and DCCC.

    Its inner loop, at 0x80096DB8, decodes a RUN-LENGTH ENCODED stream that is
    not the 8bpp format this tool reads:

        lbu  t0, 0(s0)        next byte of the stream
        andi v0, t0, 0x0080   high bit set means this byte starts a run
        andi t0, t0, 0x007f   low seven bits are the palette index
        lbu  a2, 0(s0)        the following byte is the run length,
                              and a length of zero means "to the row width",
                              which is taken from [t7 + 7408]
        blez t0, +0x18        INDEX 0 IS SKIPPED -- transparent
        lw   v0, 7216(s1)     lookup table base
        sll  v1, t0, 1        index * 2, so 16-bit entries
        lhu  a0, 0(v1)        LUT[index]
        sh   a0, 0(a1)        write one 16-bit pixel
        addiu a1, a1, 2       destination advances two bytes

    So the engine's sprite path is RLE over 7-bit indices, expanding through a
    16-bit LUT into a 16-bit framebuffer, with index 0 hardcoded as
    transparent. That last detail is worth holding onto: transparency here is
    a property of the BLITTER, not of the header flag. An earlier guess that
    the flag selected between "no transparency", "chroma key" and "palette
    alpha" was tested and produced no pixel difference at all, and this is
    why -- the code never consults the flag to make that decision.

    What the two bits actually select is still unestablished. Bit 0 gates a
    list walk at 0x80111C40 and bit 1 gates a table lookup indexed by the
    halfword at +2; both sit inside the descriptor-comparison logic rather
    than the pixel loop. Flags 0, 1 and 3 account for 275 images and all
    of them pair with a palette. Flag 2 accounts for exactly six -- 009/01C,
    009/01E, 009/01F, 009/020, 009/021 and 009/022 -- and those six are
    precisely the ones that resist pairing.

    They resist it for a reason that looks structural rather than accidental.
    Four are 88x81 and decode to a 3x3 grid of rounded panels; 009/01C is a
    64x30 bar and 009/022 a 512x384 rectangular region map; all six use few
    indices over large flat areas. Rendered against every one of the 142
    palettes in the cartridge they produce a plausible-looking grid every
    time, because a layout with no recognisable subject cannot be told right
    from wrong by eye -- the method that settled directories 000, 002, 004,
    006 and 007 has nothing to work with here.

    Nor does the engine help. An execute breakpoint on the resource getter at
    0x80064D60 catches other directory 009 entries -- it sees 009/00C followed
    three frames later by 009/00D, and 009/011 during a melee game -- but
    never these six, across the title, main menu, episode list, scenario tab,
    Load Saved, two-player, campaign briefing, Encyclopedia and a running
    game.

    So "which palette" may simply be the wrong question for flag 2. These are
    left unpaired rather than guessed at.
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


# Directory 006 is the UI and glue-screen bank, and it follows none of the
# positional rules the other directories do: a palette sits before its images
# as often as after, several images share one, and 006/004, 006/02B, 006/02C,
# 006/030 and 006/035 serve OTHER directories (the briefing portraits, the
# building sprites, the menu frames) rather than anything in 006 itself.
#
# So this is a measured table, not a rule. Every pairing here was decoded and
# examined: the UI pieces form one visual family -- purple armoured bezels
# over a green chroma key with flat dark-green interiors -- the backdrops are
# photographic and not keyed at all, and 006/020 reads "R BUTTON" cleanly.
# Cross-image consistency of that kind is what a wrong palette cannot produce.
#
# 006/000 and 006/012..018 are NOT here. They belong to the same family, but
# 006/001, 006/01C and 006/022 render them identically, so the evidence cannot
# choose between those three, and 006/000 comes out near-black under all of
# them. They are reported as unpaired rather than guessed at.
# Six of directory 006's palettes are near-identical green-key variants:
# 006/001, 006/01C, 006/022, 006/02C, 006/031 and 006/03A agree on 251 of 256
# entries and differ only at indices 1, 2, 3, 4 and 254. 006/01C and 006/022
# are byte-identical to each other, as are 006/031 and 006/03A.
#
# That is why looking cannot choose between them for the screen-A UI pieces,
# and why it does not much matter: those images use the five differing indices
# between 0.000% and 4.8% of their pixels, and 006/014, 006/015, 006/017 and
# 006/03C do not use them at all, so their decode is pixel-identical whichever
# is picked. Each is paired with the green-key palette of its own screen group.
#
# 006/000 is the exception and is deliberately left out: 89.3% of it is index
# 254 -- one of the five that disagree -- so for that image the choice decides
# almost every pixel, and nothing measured so far distinguishes the candidates.
# It is a full-screen border overlay whose interior is that single flat index.
DIR006_GREENKEY = {
    "006/012": "006/001", "006/013": "006/001", "006/014": "006/001",
    "006/015": "006/001", "006/016": "006/001", "006/017": "006/001",
    "006/018": "006/001",
    "006/03C": "006/03A",
    # 006/000 is here on the same reasoning, established later. 89.3% of it is
    # index 254, which reads as a reason to worry until you look at what the
    # six candidates put there: (0,0,0), (8,8,8) or (8,0,0). Near-black in all
    # of them, a maximum difference of 8/255, so that 89% renders identically
    # whichever is chosen. The chroma key is index 0 at (0,255,0), not 254.
    # Five of the six also agree exactly on indices 1-4; only 006/02C differs.
    # Matching the decode against a live Encyclopedia frame puts all six within
    # 18.65..18.88 of it while 006/002 scores 85.9 and 006/01A 227.2, so the
    # family is settled even though the member is not, and the member does not
    # matter.
    "006/000": "006/001",
}

# Directory 002's seven menu pieces have no adjacent palette and take one from
# directory 006. Three independent lines agree: an offline analysis of the
# archive, decoding 002/010 both ways (006/02C renders the tab-bar text legibly
# over a clean chroma key, 002/009 gives an illegible bar over a maroon fill),
# and the engine itself -- an execute breakpoint on the resource getter at
# 0x80064D60 shows 006/02C being fetched while these screens are up, with no
# directory 006 image requested alongside it.
#
# The positional rule pairs these with 002/009..00E, which is wrong. Those six
# palettes sit before the images rather than after and serve nothing here.
DIR002_EXTERNAL = {f"002/{n:03X}": "006/02C" for n in range(0x010, 0x017)}

DIR006_PAIRING = {
    "006/01B": "006/01A",       # space backdrop, planet and moon
    "006/01D": "006/01C",       # single readout bar
    "006/01E": "006/01C",       # double readout bar
    "006/01F": "006/01C",       # main panel frame
    "006/020": "006/021",       # "R BUTTON" legend plate
    "006/023": "006/022",       # wide panel frame
    "006/024": "006/022",       # tall panel frame
    "006/025": "006/022",       # tall panel frame, corner bracket
    "006/026": "006/022",       # small landscape panel
    "006/027": "006/022",       # selector bar, gold chevrons
    "006/028": "006/022",       # selector bar, wider
    "006/029": "006/022",       # the same bar with no chevrons
    "006/02A": "006/02B",       # tiled industrial interior backdrop
    "006/02D": "006/02C",       # two-pane panel frame
    "006/02E": "006/02C",       # wide panel frame
    "006/02F": "006/02C",       # small rail / button strip
    "006/032": "006/031",       # panel frame, dial ornament
    "006/033": "006/031",       # wide message box
    "006/034": "006/031",       # small square panel
    "006/036": "006/037",       # moon over a rocky landscape
    "006/038": "006/039",       # deep space, planet limb
    "006/03B": "006/03A",       # large keyed panel frame
}

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

        if d == "002":
            for i in imgs:
                if i in DIR002_EXTERNAL:
                    out[i] = DIR002_EXTERNAL[i]
            # everything else in 002 pairs adjacently; fall through

        if d == "006":
            # Measured, not derived -- see DIR006_PAIRING.
            for i in imgs:
                out[i] = DIR006_PAIRING.get(i) or DIR006_GREENKEY.get(i)
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
