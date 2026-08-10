#!/usr/bin/env python3
"""
extract_glue.py -- extract the StarCraft 64 "glue" screens from a ROM you own
and render them as readable text.

    python extract_glue.py "StarCraft 64 (USA).n64" -o glue/

No third-party packages required (standard library only).

This tool ships NO game data. The screens are Blizzard's copyrighted text and
artwork -- keep what it produces for yourself, do not redistribute it.

Two BOLT directories drive the non-gameplay screens, and both are plain-text
scripts in their own little markup language, the same way the mission briefings
in directory 007 are:

  003  establishing shots -- the still-image + caption card shown before each
       campaign mission, plus the end credits roll.  61 scripts (003/000 ..
       003/03C) followed by 60 binary asset entries.
  004  slideshows -- the still-frame retellings that stand in for the PC game's
       full-motion cinematics.  13 scripts (004/000 .. 004/00C) followed by 85
       binary asset entries.

Directory 003: double-angle markup
----------------------------------
Every tag is written "</NAME arg>".  The leading slash is a sigil, not an XML
end tag -- there is no matching "<NAME>" anywhere in the corpus.  Each tag sits
alone on its own line (0 exceptions in 61 scripts) and CRLF is the terminator.

    </COMMENT text>          developer annotation, emits nothing
    </BACKGROUND path.pcx>   full-screen backdrop
    </FONTCOLOR path.pcx>    text colour ramp (a 48x1 tfont.pcx)
    </DISPLAYTIME ms>        how long the finished page stays up
    </FADESPEED n>           cross-fade rate
    </SCREENLEFT>            open a body region, left-aligned narrative
    </SCREENLOWERLEFT>       open a body region, lower-left title card
    </PAGE>                  render the pending page, then clear it

A </PAGE> is a *flush*, not a container.  The four directives set sticky state
that survives the flush; one of </SCREENLEFT> / </SCREENLOWERLEFT> opens the
body region, the lines after it are the body, and </PAGE> renders body + region
and clears only those two.  A script with two pages therefore only re-states the
directives it actually changes.

Directory 004: single-angle markup
----------------------------------
Same idea, one angle bracket: "<NAME arg>".  Ops run as a linear timeline.

    <BORDFADEUP n> / <BORDFADEDOWN>     frame overlay n (0..2)
    <SLIDEFADEUP n> / <SLIDEFADEDOWN>   still image n (0..38)
    <SLIDESPEED n> / <TEXTSPEED n>      fade rates
    <TEXT1> / <TEXT2>                   caption block; text follows the tag
    <TEXTFADEDOWN>                      clear the caption
    <WAIT n>                            hold

Traps this parser handles (all verified against the USA ROM)
-----------------------------------------------------------
* Directory 003 is CP1252, not ASCII: 003/006 and 003/010 each carry one 0x92
  curly apostrophe, so .decode("ascii") and .decode("utf-8") both raise.  The
  USA/Australia directory 004 happens to be pure ASCII, but the German
  prototype puts umlauts (0xC4 0xD6 0xDC 0xDF 0xE4 0xF6 0xFC) in both
  directories, so both are decoded as CP1252 and a note is filed listing the
  bytes rather than the decode being allowed to fail.
* 56 of the directory-004 tags carry their payload INLINE, on the same line as
  the closing ">", while directories 003 and 007 never do -- hence _lines()
  drops a leading newline only if there is one.
* 004/000, 004/005 and 004/009 do not end with a newline.
* Directory 003 contains 701 TABs (003/01D and the credits) and three
  whitespace-only lines, one of which sits *between* directives rather than
  inside a body, so whitespace-only text outside a region is not an error.
* One COMMENT in 003/00C mixes prose with a trailing '#' ruler, so a comment is
  classified as a ruler by what is left after stripping '#', not by its first
  character.
* BOLT file_type 0x0A covers BOTH the scripts and the 48-byte binary font ramps
  in directory 003.  Never select scripts by file_type -- this tool uses the
  index range and confirms with a content sniff.

Pairing
-------
Establishing shot 003/i belongs to map 008/(i+8) for i in 0x00..0x3B -- the same
offset extract_briefings.py uses for 007/i, and the same 60-entry campaign run.
003/03C is the credits and pairs with nothing.  Directory 004 has no per-mission
pairing; its 13 scripts are episode-level interludes.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field

from extract_sc64_maps import (
    BoltArchive, load_rom, looks_like_chk, parse_map,
    rom_cart_id, rom_internal_name, safe_filename,
)

# --------------------------------------------------------------------------
# Layout constants
# --------------------------------------------------------------------------

ESTABLISHING_DIR = "003"
SLIDESHOW_DIR = "004"

# Highest index that is a script in each directory. Everything above is binary
# artwork. The sniff below is what actually decides; these bound the search so
# a 48-byte font ramp that happens to start with '<' can never be mistaken for
# a script.
LAST_ESTABLISHING_SCRIPT = 0x3C
LAST_SLIDESHOW_SCRIPT = 0x0C

# 003/i <-> 008/(i+8), identical to BRIEFING_TO_MAP_OFFSET in extract_briefings.
GLUE_TO_MAP_OFFSET = 8
LAST_PAIRED_ESTABLISHING = 0x3B   # 003/03C is the credits, it pairs with nothing

DOUBLE_TAG_RE = re.compile(r"</([A-Za-z0-9_]+)([^>\r\n]*)>")
SINGLE_TAG_RE = re.compile(r"<([A-Za-z0-9_]+)([^>\r\n]*)>")

# A script's very first bytes. Directory 003 always opens "</COMMENT ",
# directory 004 always opens with a single-angle op; requiring a letter after
# the bracket rejects binary that merely happens to start with 0x3C.
DOUBLE_SNIFF = re.compile(rb"^</[A-Za-z]")
SINGLE_SNIFF = re.compile(rb"^<[A-Za-z]")

STICKY = {
    "BACKGROUND": "background",
    "FONTCOLOR": "fontcolor",
    "DISPLAYTIME": "displaytime",
    "FADESPEED": "fadespeed",
}
REGIONS = ("SCREENLEFT", "SCREENLOWERLEFT")

# Directory-004 opcodes and whether they take a numeric argument.
SLIDE_OPS = {
    "BORDFADEUP": True, "BORDFADEDOWN": False,
    "SLIDEFADEUP": True, "SLIDEFADEDOWN": False,
    "SLIDESPEED": True, "TEXTSPEED": True,
    "TEXT1": False, "TEXT2": False, "TEXTFADEDOWN": False,
    "WAIT": True,
}
TEXT_OPS = ("TEXT1", "TEXT2")

# Binary asset shapes, both directories.
IMAGE_MAGIC = b"\x00\x00\x00\x08"     # u32 bits-per-pixel; every asset is 8bpp
IMAGE_HEADER = 16
PALETTE_SIZE = 518                    # 6-byte prefix + 256 x u16 RGBA5551
RAMP_SIZE = 48                        # the 48x1 pixel row of a PC tfont.pcx


# --------------------------------------------------------------------------
# Parsed shapes
# --------------------------------------------------------------------------

@dataclass
class Page:
    """One </PAGE> flush: the sticky state in force plus the body region."""
    region: str = ""                 # SCREENLEFT or SCREENLOWERLEFT
    background: str = ""
    fontcolor: str = ""
    displaytime: str = ""
    fadespeed: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "title-card" if self.region == "SCREENLOWERLEFT" else "narrative"

    @property
    def title(self) -> str:
        """SCREENLOWERLEFT pages open with a quoted all-caps mission title."""
        if self.lines and self.lines[0].startswith('"'):
            return self.lines[0].strip().strip('"')
        return ""


@dataclass
class GlueScreen:
    """One directory-003 script."""
    bolt_path: str
    index: int = -1
    label: str = ""                  # first non-ruler </COMMENT>
    comments: list[str] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    map_path: str = ""
    map_name: str = ""
    warnings: list[str] = field(default_factory=list)
    is_credits: bool = False

    @property
    def is_blank(self) -> bool:
        """A placeholder: a lone </COMMENT> and nothing to draw."""
        return not self.pages

    @property
    def backgrounds(self) -> list[str]:
        seen = []
        for p in self.pages:
            if p.background and p.background not in seen:
                seen.append(p.background)
        return seen


@dataclass
class Step:
    """One directory-004 timeline op."""
    op: str
    arg: str = ""
    text: list[str] = field(default_factory=list)


@dataclass
class Slideshow:
    """One directory-004 script."""
    bolt_path: str
    index: int = -1
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def slides(self) -> list[int]:
        return [int(s.arg) for s in self.steps
                if s.op == "SLIDEFADEUP" and s.arg.isdigit()]

    @property
    def borders(self) -> list[int]:
        return sorted({int(s.arg) for s in self.steps
                       if s.op == "BORDFADEUP" and s.arg.isdigit()})

    @property
    def captions(self) -> list[Step]:
        return [s for s in self.steps if s.op in TEXT_OPS]

    @property
    def hold_ticks(self) -> int:
        return sum(int(s.arg) for s in self.steps
                   if s.op == "WAIT" and s.arg.isdigit())


@dataclass
class Asset:
    """One binary entry above the script range."""
    bolt_path: str
    kind: str                        # image / palette / fontramp / unknown
    size: int
    width: int = 0
    height: int = 0
    bpp: int = 0


# --------------------------------------------------------------------------
# Shared tokeniser
# --------------------------------------------------------------------------

def tokenize(text: str, double: bool):
    """Yield (tag_or_None, arg, chunk) in document order.

    The two dialects differ only in the opening sigil, so one function keyed on
    `double` covers both. `chunk` is everything between this tag's ">" and the
    next tag -- for directory 004 that can begin on the same line.
    """
    rx = DOUBLE_TAG_RE if double else SINGLE_TAG_RE
    pos = 0
    pending: tuple[str, str] | None = None
    for match in rx.finditer(text):
        chunk = text[pos:match.start()]
        if pending is None:
            if chunk.strip():
                yield None, "", chunk
        else:
            yield pending[0], pending[1], chunk
        pending = (match.group(1), match.group(2).strip())
        pos = match.end()
    chunk = text[pos:]
    if pending is None:
        if chunk.strip():
            yield None, "", chunk
    else:
        yield pending[0], pending[1], chunk


def _lines(chunk: str) -> list[str]:
    """Split a between-tags chunk into body lines.

    Exactly one leading newline is dropped, and only if it is there: in
    directories 003 and 007 the tag always ends its own line, but 56 of the
    directory-004 tags run straight into their payload.

    Interior blank lines are content -- a title card is "title, blank, caption"
    -- so only trailing blanks go. TABs are preserved: they carry the credits
    roll's indentation, which is what separates a section heading from a name.
    """
    lines = chunk.replace("\r\n", "\n").split("\n")
    if lines and lines[0] == "":
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return [line.rstrip() for line in lines]


def _decode(raw: bytes, encoding: str, warnings: list[str]) -> str:
    """Decode, noting anything that would have broken a stricter codec."""
    try:
        raw.decode("ascii")
    except UnicodeDecodeError:
        odd = sorted({b for b in raw if b > 0x7E})
        warnings.append("non-ASCII byte(s) " +
                        ",".join(f"0x{b:02X}" for b in odd) +
                        f"; decoded as {encoding}")
    return raw.decode(encoding, "replace")


def _is_ruler(text: str) -> bool:
    """True for the '######...' separator comments.

    Tested on what survives after the hashes, not on the first character: the
    one COMMENT in 003/00C puts a real remark in front of its ruler.
    """
    return not text.strip("#").strip()


# --------------------------------------------------------------------------
# Directory 003 -- establishing shots and credits
# --------------------------------------------------------------------------

def parse_screen(bolt_path: str, raw: bytes) -> GlueScreen:
    screen = GlueScreen(bolt_path=bolt_path)
    try:
        screen.index = int(bolt_path.partition("/")[2], 16)
    except ValueError:
        pass
    text = _decode(raw, "cp1252", screen.warnings)

    state = {"background": "", "fontcolor": "", "displaytime": "", "fadespeed": ""}
    region = ""
    body: list[str] = []

    for tag, arg, chunk in tokenize(text, double=True):
        lines = _lines(chunk)

        if tag is None:
            screen.warnings.append(f"{len(lines)} line(s) before any tag")
            continue

        if tag == "COMMENT":
            if not _is_ruler(arg):
                remark = arg.strip("#").strip()
                screen.comments.append(remark)
                if not screen.label:
                    screen.label = remark
        elif tag in STICKY:
            if not arg:
                screen.warnings.append(f"</{tag}> with no argument")
            state[STICKY[tag]] = arg
        elif tag in REGIONS:
            if region:
                screen.warnings.append(
                    f"</{tag}> while </{region}> was still open; "
                    f"discarded {len(body)} pending line(s)")
            region = tag
            body = []
        elif tag == "PAGE":
            if not region:
                screen.warnings.append("</PAGE> with no screen region open")
            screen.pages.append(Page(
                region=region, lines=body,
                background=state["background"], fontcolor=state["fontcolor"],
                displaytime=state["displaytime"], fadespeed=state["fadespeed"]))
            region = ""
            body = []
        else:
            screen.warnings.append(f"unknown tag </{tag}>")

        if lines:
            if region and tag != "PAGE":
                body.extend(lines)
            elif any(line.strip() for line in lines):
                screen.warnings.append(
                    f"{len(lines)} line(s) of text after </{tag}> "
                    f"with no screen region open")

    if region or body:
        screen.warnings.append(
            f"</{region or 'region'}> never flushed by a </PAGE>; "
            f"kept its {len(body)} line(s) as a final page")
        screen.pages.append(Page(
            region=region, lines=body,
            background=state["background"], fontcolor=state["fontcolor"],
            displaytime=state["displaytime"], fadespeed=state["fadespeed"]))

    if not screen.comments:
        screen.warnings.append("no </COMMENT> label")
    return screen


# --------------------------------------------------------------------------
# Directory 004 -- slideshows
# --------------------------------------------------------------------------

def parse_slideshow(bolt_path: str, raw: bytes) -> Slideshow:
    show = Slideshow(bolt_path=bolt_path)
    try:
        show.index = int(bolt_path.partition("/")[2], 16)
    except ValueError:
        pass
    text = _decode(raw, "cp1252", show.warnings)

    for tag, arg, chunk in tokenize(text, double=False):
        lines = _lines(chunk)

        if tag is None:
            show.warnings.append(f"{len(lines)} line(s) before any tag")
            continue

        if tag not in SLIDE_OPS:
            show.warnings.append(f"unknown op <{tag}>")
        else:
            wants_arg = SLIDE_OPS[tag]
            if wants_arg and not arg.lstrip("-").isdigit():
                show.warnings.append(
                    f"<{tag}> wants a number, got {arg!r}")
            elif not wants_arg and arg:
                show.warnings.append(f"<{tag}> takes no argument, got {arg!r}")

        step = Step(op=tag, arg=arg)
        if tag in TEXT_OPS:
            step.text = lines
            if not lines:
                show.warnings.append(f"<{tag}> with no caption text")
        elif any(line.strip() for line in lines):
            show.warnings.append(
                f"{len(lines)} line(s) of text after <{tag}>, "
                f"which is not a caption op")
            step.text = lines
        show.steps.append(step)

    up = [s for s in show.steps if s.op == "SLIDEFADEUP"]
    down = [s for s in show.steps if s.op == "SLIDEFADEDOWN"]
    if len(up) != len(down):
        show.warnings.append(
            f"{len(up)} <SLIDEFADEUP> vs {len(down)} <SLIDEFADEDOWN>")
    return show


# --------------------------------------------------------------------------
# Binary assets
# --------------------------------------------------------------------------

def classify_asset(bolt_path: str, data: bytes) -> Asset:
    """Identify one binary entry from its own bytes.

    image     16-byte header: u32 bpp, u32 0, u16 width, u16 height, u32 0,
              then width*height 8-bit palette indices.
    palette   518 bytes: a 6-byte prefix then 256 big-endian RGBA5551 words.
    fontramp  48 bytes: the raw 48x1 pixel row of a PC glue\\pal??\\tfont.pcx,
              6 font colours x 8 palette indices.
    """
    if len(data) > IMAGE_HEADER and data[:4] == IMAGE_MAGIC:
        bpp, _, width, height, _ = struct.unpack_from(">IIHHI", data)
        if IMAGE_HEADER + width * height == len(data):
            return Asset(bolt_path, "image", len(data), width, height, bpp)
    if len(data) == PALETTE_SIZE:
        return Asset(bolt_path, "palette", len(data))
    if len(data) == RAMP_SIZE:
        return Asset(bolt_path, "fontramp", len(data))
    return Asset(bolt_path, "unknown", len(data))


# --------------------------------------------------------------------------
# Locating the scripts in the ROM
# --------------------------------------------------------------------------

def collect(archive: BoltArchive, verbose: bool = False):
    """Return (screens, slideshows, assets) -- each a list, in BOLT order."""
    screens: dict[int, tuple[str, bytes]] = {}
    shows: dict[int, tuple[str, bytes]] = {}
    assets: list[Asset] = []
    maps: dict[int, tuple[str, object]] = {}

    for entry in archive.entries():
        directory, _, index_hex = entry.path.partition("/")
        if directory not in (ESTABLISHING_DIR, SLIDESHOW_DIR, "008"):
            continue
        try:
            index = int(index_hex, 16)
        except ValueError:
            continue
        try:
            head = archive.read(entry, limit=4)
        except (ValueError, IndexError) as exc:
            if verbose:
                print(f"  ! {entry.path}: {exc}", file=sys.stderr)
            continue

        if directory == "008":
            if head[:4] not in (b"TYPE", b"VER ", b"IVER"):
                continue
            try:
                data = archive.read(entry)
            except (ValueError, IndexError):
                continue
            if looks_like_chk(data):
                maps[index] = (entry.path, parse_map(entry.path, data))
            continue

        double = directory == ESTABLISHING_DIR
        last = LAST_ESTABLISHING_SCRIPT if double else LAST_SLIDESHOW_SCRIPT
        sniff = DOUBLE_SNIFF if double else SINGLE_SNIFF
        try:
            data = archive.read(entry)
        except (ValueError, IndexError) as exc:
            if verbose:
                print(f"  ! {entry.path}: {exc}", file=sys.stderr)
            continue

        # Index range first, content sniff to confirm. file_type is useless
        # here: 0x0A is both a script and a 48-byte font ramp.
        if index <= last and sniff.match(data) and b">" in data[:64]:
            (screens if double else shows)[index] = (entry.path, data)
        else:
            assets.append(classify_asset(entry.path, data))

    out_screens = []
    for index in sorted(screens):
        path, raw = screens[index]
        screen = parse_screen(path, raw)
        if index <= LAST_PAIRED_ESTABLISHING:
            paired = maps.get(index + GLUE_TO_MAP_OFFSET)
            if paired:
                screen.map_path, info = paired
                screen.map_name = info.name
            else:
                screen.warnings.append("no map paired with this screen")
        else:
            screen.is_credits = True
        out_screens.append((screen, raw))

    out_shows = [(parse_slideshow(*shows[i]), shows[i][1]) for i in sorted(shows)]
    return out_screens, out_shows, assets


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_screen(screen: GlueScreen) -> str:
    out = []
    title = screen.label or screen.map_name or "(unlabelled)"
    out.append(title)
    out.append("=" * len(title))
    line = f"glue {screen.bolt_path}"
    if screen.map_path:
        line += f"   map {screen.map_path}   {screen.map_name}"
    out.append(line)
    out.append("")

    for remark in screen.comments[1:]:
        out.append(f"; {remark}")
    if len(screen.comments) > 1:
        out.append("")

    if screen.is_blank:
        out.append("(placeholder -- a comment and nothing to draw)")

    for n, page in enumerate(screen.pages, 1):
        head = f"page {n}  [{page.kind}]"
        out.append(head)
        out.append("-" * len(head))
        out.append(f"  background  {page.background or '(inherited)'}")
        out.append(f"  fontcolor   {page.fontcolor or '(inherited)'}")
        out.append(f"  displaytime {page.displaytime or '(default)'}"
                   f"   fadespeed {page.fadespeed or '(default)'}")
        out.append("")
        out.extend("  " + line for line in page.lines)
        out.append("")

    if screen.warnings:
        out.append("; parser notes: " + "; ".join(screen.warnings))
    return "\n".join(out).rstrip() + "\n"


def render_slideshow(show: Slideshow) -> str:
    out = []
    title = f"slideshow {show.bolt_path}"
    out.append(title)
    out.append("=" * len(title))
    out.append(f"slides {show.slides}   borders {show.borders}   "
               f"captions {len(show.captions)}   total WAIT {show.hold_ticks}")
    out.append("")

    for step in show.steps:
        head = f"<{step.op}{' ' + step.arg if step.arg else ''}>"
        if step.text:
            out.append(head)
            out.extend("    " + line for line in step.text)
            out.append("")
        else:
            out.append(head)

    if show.warnings:
        out.append("")
        out.append("; parser notes: " + "; ".join(show.warnings))
    return "\n".join(out).rstrip() + "\n"


def screen_to_dict(screen: GlueScreen) -> dict:
    return {
        "bolt_path": screen.bolt_path,
        "index": screen.index,
        "label": screen.label,
        "comments": screen.comments,
        "map_path": screen.map_path,
        "map_name": screen.map_name,
        "is_credits": screen.is_credits,
        "is_blank": screen.is_blank,
        "backgrounds": screen.backgrounds,
        "pages": [
            {"region": p.region, "kind": p.kind, "title": p.title,
             "background": p.background, "fontcolor": p.fontcolor,
             "displaytime": p.displaytime, "fadespeed": p.fadespeed,
             "lines": p.lines}
            for p in screen.pages
        ],
        "warnings": screen.warnings,
    }


def slideshow_to_dict(show: Slideshow) -> dict:
    return {
        "bolt_path": show.bolt_path,
        "index": show.index,
        "slides": show.slides,
        "borders": show.borders,
        "hold_ticks": show.hold_ticks,
        "steps": [{"op": s.op, "arg": s.arg, "text": s.text} for s in show.steps],
        "warnings": show.warnings,
    }


def asset_to_dict(asset: Asset) -> dict:
    return {"bolt_path": asset.bolt_path, "kind": asset.kind,
            "size": asset.size, "width": asset.width,
            "height": asset.height, "bpp": asset.bpp}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _stem(screen: GlueScreen) -> str:
    name = screen.map_name or screen.label or "Untitled"
    return f"{screen.bolt_path.replace('/', '-')} {safe_filename(name)}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract the StarCraft 64 establishing shots, credits and "
                    "cinematic slideshows from a ROM you own.",
        epilog="These screens are Blizzard's copyrighted content. "
               "Keep them to yourself.",
    )
    parser.add_argument("rom", help="StarCraft 64 ROM (.z64, .v64 or .n64)")
    parser.add_argument("-o", "--out", default="glue",
                        help="output directory (default: glue)")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list what was found and exit without writing")
    parser.add_argument("--raw", action="store_true",
                        help="also write the original script bytes as .script")
    parser.add_argument("--json", action="store_true",
                        help="also write glue.json with the parsed structure")
    parser.add_argument("--assets", action="store_true",
                        help="also report the binary artwork entries")
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

    screens, shows, assets = collect(archive, args.verbose)
    if not screens and not shows:
        print("error: no glue scripts found in this ROM", file=sys.stderr)
        return 1

    header = (f"{'BOLT':9} {'map':9} {'pg':>2} {'ttl':>3} {'nar':>3}  "
              f"{'label':52} mission")
    print(header)
    print("-" * len(header))
    for screen, _ in screens:
        titles = sum(1 for p in screen.pages if p.kind == "title-card")
        print(f"{screen.bolt_path:9} {screen.map_path or '-':9} "
              f"{len(screen.pages):2} {titles:3} {len(screen.pages) - titles:3}  "
              f"{screen.label[:52]:52} {screen.map_name}"
              f"{'   [placeholder]' if screen.is_blank else ''}")

    print()
    header = f"{'BOLT':9} {'steps':>5} {'caps':>4} {'wait':>5}  slides"
    print(header)
    print("-" * len(header))
    for show, _ in shows:
        print(f"{show.bolt_path:9} {len(show.steps):5} {len(show.captions):4} "
              f"{show.hold_ticks:5}  "
              f"{','.join(str(s) for s in show.slides)}")

    pages = sum(len(s.pages) for s, _ in screens)
    blanks = sum(1 for s, _ in screens if s.is_blank)
    print(f"\n{len(screens)} establishing scripts ({pages} pages, "
          f"{blanks} placeholders), {len(shows)} slideshows "
          f"({sum(len(s.steps) for s, _ in shows)} steps)")

    if args.assets:
        kinds = {}
        for asset in assets:
            kinds.setdefault(asset.kind, []).append(asset)
        print("\nbinary assets:")
        for kind in sorted(kinds):
            group = kinds[kind]
            dims = sorted({(a.width, a.height) for a in group if a.width})
            print(f"  {kind:9} {len(group):4}  "
                  + (", ".join(f"{w}x{h}" for w, h in dims) if dims
                     else f"{group[0].size} bytes each"))
        for asset in assets:
            print(f"    {asset.bolt_path:9} {asset.kind:9} {asset.size:7}"
                  + (f"  {asset.width}x{asset.height} {asset.bpp}bpp"
                     if asset.width else ""))

    flagged = ([s for s, _ in screens if s.warnings]
               + [s for s, _ in shows if s.warnings])
    if flagged:
        print(f"\n{len(flagged)} script(s) with parser notes:")
        for item in flagged:
            print(f"  {item.bolt_path}: {'; '.join(item.warnings)}")

    if args.list:
        return 0

    os.makedirs(args.out, exist_ok=True)
    for screen, raw in screens:
        stem = _stem(screen)
        with open(os.path.join(args.out, stem + ".txt"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(render_screen(screen))
        if args.raw:
            with open(os.path.join(args.out, stem + ".script"), "wb") as fh:
                fh.write(raw)
    for show, raw in shows:
        stem = show.bolt_path.replace("/", "-") + " slideshow"
        with open(os.path.join(args.out, stem + ".txt"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(render_slideshow(show))
        if args.raw:
            with open(os.path.join(args.out, stem + ".script"), "wb") as fh:
                fh.write(raw)

    if args.json:
        payload = {
            "establishing": [screen_to_dict(s) for s, _ in screens],
            "slideshows": [slideshow_to_dict(s) for s, _ in shows],
            "assets": [asset_to_dict(a) for a in assets],
        }
        with open(os.path.join(args.out, "glue.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    print(f"\nwrote {len(screens) + len(shows)} scripts to "
          f"{os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
