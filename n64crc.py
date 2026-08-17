"""Detect and repair the N64 boot checksum after patching a ROM.

Most of what this project writes lands in the BOLT archive, which starts at
0x12CA10 -- well past the 0x1000..0x101000 window the CIC boot checksum covers.
Map injection therefore never needs this. Anything that patches the static
segment does: the melee Scenario table at 0x0D16E8 sits at file offset 857,832,
comfortably inside it.

Patch inside the window without fixing the header and IPL3 refuses to hand off.
The ROM boots to a black screen, no code runs, and nothing distinguishes that
from "the patch had no effect" unless you already suspect the checksum. This is
the single easiest way to lose an afternoon on an N64 ROM hack.

The two checksum words live at header offsets 0x10 and 0x14, big endian. Which
seed applies depends on the CIC chip. Rather than assume 6102 because it is the
common case, `detect` computes with every variant and keeps whichever
reproduces the value the unmodified ROM already carries; if none does, it says
so instead of writing a number that merely looks plausible.

    from n64crc import detect, fix, stored
    variant = detect(original_rom_bytes)     # e.g. "6102"
    fix(patched_bytearray, variant)          # rewrites the header in place

The algorithm is the well-known one from the community's n64crc/chksum64,
reimplemented here so this repository stays dependency-free.
"""

from __future__ import annotations

MASK32 = 0xFFFFFFFF

# The checksum covers exactly one megabyte starting after the header.
START = 0x1000
LENGTH = 0x100000

# CIC-6105 mixes in bytes from the loaded image itself, at this offset.
HEADER = 0x40

# Seed per CIC variant, and how the two words are combined at the end.
VARIANTS: dict[str, tuple[int, str]] = {
    "6101": (0xF8CA4DDC, "xor"),
    "6102": (0xF8CA4DDC, "xor"),      # shares 6101's seed
    "6103": (0xA3886759, "add"),
    "6105": (0xDF26F436, "xor"),      # different inner loop, see compute()
    "6106": (0x1FEA617A, "mul"),
}


def _rol(value: int, bits: int) -> int:
    bits &= 0x1F
    if bits == 0:
        return value & MASK32
    return ((value << bits) | (value >> (32 - bits))) & MASK32


def compute(rom: bytes, variant: str) -> tuple[int, int]:
    """The checksum pair `rom` should carry, under `variant`."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown CIC variant {variant!r}")
    if len(rom) < START + LENGTH:
        raise ValueError(f"ROM is too short to checksum: {len(rom):,} bytes")

    seed, combine = VARIANTS[variant]
    t1 = t2 = t3 = t4 = t5 = t6 = seed
    is6105 = variant == "6105"

    for i in range(START, START + LENGTH, 4):
        d = int.from_bytes(rom[i:i + 4], "big")
        if ((t6 + d) & MASK32) < t6:
            t4 = (t4 + 1) & MASK32
        t6 = (t6 + d) & MASK32
        t3 ^= d
        r = _rol(d, d & 0x1F)
        t5 = (t5 + r) & MASK32
        if t2 > d:
            t2 ^= r
        else:
            t2 ^= t6 ^ d
        if is6105:
            j = HEADER + 0x0710 + (i & 0xFF)
            t1 = (t1 + (int.from_bytes(rom[j:j + 4], "big") ^ d)) & MASK32
        else:
            t1 = (t1 + (t5 ^ d)) & MASK32

    if combine == "add":
        return ((t6 ^ t4) + t3) & MASK32, ((t5 ^ t2) + t1) & MASK32
    if combine == "mul":
        return ((t6 * t4) + t3) & MASK32, ((t5 * t2) + t1) & MASK32
    return (t6 ^ t4 ^ t3) & MASK32, (t5 ^ t2 ^ t1) & MASK32


def stored(rom: bytes) -> tuple[int, int]:
    """The checksum pair currently in the header."""
    return (int.from_bytes(rom[0x10:0x14], "big"),
            int.from_bytes(rom[0x14:0x18], "big"))


def detect(rom: bytes) -> str | None:
    """Which CIC variant reproduces this ROM's own stored checksum?

    Pass an UNMODIFIED image. Once you have patched inside the checksum window
    nothing will match, which is the point.
    """
    want = stored(rom)
    for name in VARIANTS:
        if compute(rom, name) == want:
            return name
    return None


def fix(rom: bytearray, variant: str) -> tuple[int, int]:
    """Rewrite the header checksum in place. Returns the new pair."""
    c1, c2 = compute(bytes(rom), variant)
    rom[0x10:0x14] = c1.to_bytes(4, "big")
    rom[0x14:0x18] = c2.to_bytes(4, "big")
    return c1, c2


def main(argv=None) -> int:
    import argparse

    from extract_sc64_maps import load_rom

    ap = argparse.ArgumentParser(
        description="Report or repair an N64 ROM's boot checksum.")
    ap.add_argument("rom")
    ap.add_argument("-o", "--out",
                    help="write a checksum-repaired copy here")
    ap.add_argument("--variant",
                    help="force a CIC variant instead of detecting one")
    a = ap.parse_args(argv)

    data = load_rom(a.rom)
    s = stored(data)
    print(f"stored checksum: {s[0]:#010x} {s[1]:#010x}")
    for name in VARIANTS:
        c = compute(data, name)
        print(f"  {name}: {c[0]:#010x} {c[1]:#010x}"
              + ("   <-- matches" if c == s else ""))

    variant = a.variant or detect(data)
    if variant is None:
        print("\nno variant matches -- this ROM has already been patched "
              "inside the checksum window, so pass --variant explicitly")
        if not a.out:
            return 1
        return 1
    print(f"\nCIC variant: {variant}")

    if a.out:
        buf = bytearray(data)
        c1, c2 = fix(buf, variant)
        with open(a.out, "wb") as fh:
            fh.write(buf)
        print(f"wrote {a.out} with checksum {c1:#010x} {c2:#010x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
