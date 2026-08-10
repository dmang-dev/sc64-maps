# sc64-maps

Extract the scenarios from a **StarCraft 64** cartridge dump and repackage them
as `.scm` / `.scx` files that PC StarCraft can load.

The N64 port stores its maps in the *same* CHK scenario format the PC game uses
— byte for byte, little-endian fields and all. They are just buried inside two
layers of packaging: a Mass Media **BOLT** archive with an LZSS-style
compressor, and no MPQ wrapper (the N64 has no Storm). This project peels off
the first layer and adds the second.

96 scenarios come out, including the ones that never shipped on PC.

## Legal

**This repository contains no game data, and you must not redistribute what it
produces.** The maps are Blizzard's copyrighted work. This is a tool for people
who already own a StarCraft 64 cartridge and a copy of PC StarCraft. You supply
the ROM; the tool never downloads one and none is included here.

The code is GPL-3.0-or-later, because the BOLT container walk and decompressor
are derived from [BOLTextract](https://github.com/heinermann/BOLTextract), which
is GPL-3.0.

## What you need to install

**Nothing.** You need CPython 3.9 or newer and that is the whole list — the
extractor is standard library only. No pip packages, no compiler, no MPQ tool.

That is a deliberate choice. The existing prior art needs Visual Studio 2019 to
build, and only accepts `z64` ROMs; this handles all three N64 byte orders and
runs anywhere Python does.

Optional, only if you want to go further:

| Tool | Use for |
|---|---|
| [Chkdraft](https://github.com/jjf28/Chkdraft) or SCMDraft 2 | opening/editing the extracted maps |
| [StormLib](https://github.com/ladislav-zezula/StormLib) / Ladik's MPQ Editor | inspecting the MPQ wrapper |
| [BOLTextract](https://github.com/heinermann/BOLTextract) | the original C++ extractor, for other Mass Media games |

## Usage

List what is in the ROM without writing anything:

```bash
python extract_sc64_maps.py "StarCraft 64 (USA).n64" --list
```

Extract playable maps:

```bash
python extract_sc64_maps.py "StarCraft 64 (USA).n64" -o maps/
```

Check that what came out is loadable:

```bash
python verify_maps.py maps/
```

Then copy the results into your StarCraft `Maps\` folder.

Extract the mission briefings, which are stored separately from the maps and so
do not travel with them:

```bash
python extract_briefings.py "StarCraft 64 (USA).n64" -o briefings/
```

Useful flags: `--chk` also writes the raw scenario chunks, `--dump-all DIR`
dumps all 2111 files in the BOLT archive, `-v` reports per-entry errors.

Any of the three N64 dump formats work (`.z64`, `.v64`, `.n64`) — the header
magic is what decides, not the extension. The common `StarCraft 64 (USA).n64`
release is byte-swapped `v64` data despite its name, which the tool handles.

## What comes out

96 scenarios: the Terran/Zerg/Protoss campaigns, the Brood War campaigns, the
melee maps, both tutorials, and the N64-exclusive content that was never
released for PC — *Resurrection IV*, plus the exclusive missions *Guardians*,
*Rage*, *Zerg Troopers*, and a handful of others.

Plus 96 mission briefings — 69 actually written, holding 549 transmissions, and
27 unwritten placeholders belonging to the melee maps, which are flagged rather
than passed off as content. On PC these live inside the map as `MBRF` triggers,
but the N64 build keeps them as separate plain-text scripts, so they are a
second extraction rather than something that rides along with the maps. Each is
rendered as readable text — objectives, then each transmission with its speaker
and portrait — and `--raw` / `--json` give the original script bytes or the
parsed structure instead.

The speaker's name is a literal line in the script data, so every `<PORTn>`
portrait id resolves to a character by measurement rather than guesswork; the
table is in [docs/FORMAT.md](docs/FORMAT.md) §5.2. The portrait artwork is in
the ROM too, at BOLT entry `007/(0x60+n)` — 60×56 8-bit images, including three
that shipped unused.

Each is written as `.scm` or `.scx` based on the scenario's own version stamp:
`VER` 205 / `TYPE` `RAWB` means Brood War (`.scx`), anything lower is StarCraft
(`.scm`). Filenames are prefixed with the map's address in the BOLT directory
tree (e.g. `008-065`) so they stay stable and unique — several maps share a
title, and a few campaign maps are stored with no title at all.

## How it works

```
ROM (.z64/.v64/.n64)
  └─ normalise byte order to big-endian z64
     └─ find the embedded BOLT archive
        └─ walk its directory tree, LZSS-decompress each entry
           └─ keep entries that are a well-formed CHK section chain
              └─ wrap each in a minimal MPQ as staredit\scenario.chk
                 └─ .scm / .scx
```

The MPQ writer emits format version 1 with a hash table, a block table, and a
sector offset table, all built from scratch (including Storm's crypt table and
the table encryption). Sectors are stored verbatim rather than compressed: a
reader only decompresses a sector when its stored length is *shorter* than its
plain length, so verbatim sectors are read as-is. This is the same path Storm
takes for any sector that failed to compress, which makes it the most portable
option and means no compressor needs shipping.

See [docs/FORMAT.md](docs/FORMAT.md) for the format details, and
[reference/](reference/) for the prior art this is built on.

## Verification

`verify_maps.py` re-opens every generated map with an MPQ reader written to
match StormLib's semantics exactly — the same rules the StarCraft tool
ecosystem relies on — and confirms the scenario inside is a well-formed CHK
with sane dimensions and the right extension for its version. All 96 pass.

The output has also been measured against the **323 genuine maps** shipped with
a retail StarCraft install. Format version, header size, sector size and
archive geometry are identical to real maps in all 323. Where ours differ —
uncompressed rather than PKWARE-imploded, unencrypted, a 16-slot hash table
instead of 1024 — the choice is legal per StormLib and, more importantly,
precedented in shipped content: verbatim sectors occur in Blizzard's own
`(3)Triad.scm` and `(4)Inferno.scm`, and four genuine tournament ladder maps
ship an unencrypted `scenario.chk`. All 96 also pass a full emulation of
StormLib's acceptance checks. See [docs/FORMAT.md](docs/FORMAT.md) §4.2.

And confirmed against Blizzard's own tooling: `008-00A T1) Wasteland.scm` opens
in **StarEdit** with no errors, exercising the retail `storm.dll` load path
rather than a reimplementation. Terrain, the 49 placed units, player types,
custom force names resolved out of the string table (*Colonial Militia*,
*Unidentified Creatures*) and the trigger list all read correctly.

A note if you try to check the output with **mpyq**: it will fail, and the maps
are fine — the bug is in mpyq. It decides whether a sector is compressed by
comparing its stored size against *all remaining bytes in the file* instead of
against that sector's own plain size, so it tries to decompress verbatim
sectors in any file longer than one sector. StormLib compares against the
sector's own size (`SFileReadFile.cpp:165`), which is the correct rule.

That is demonstrated rather than asserted: `reference/mpyq-forks/` vendors four
versions of mpyq (all of which share the bug) plus `sector-fix.patch`, a
three-line fix. With the patch applied, all 96 maps round-trip through mpyq and
the recovered `staredit\scenario.chk` is byte-identical to the CHK taken from
the ROM. Without it, all 96 fail. See
[reference/README.md](reference/README.md).

The extraction itself is cross-checked two ways: the main tool and the
independent rewrite in `reference/bolt_extract_all.py` find the same 96
scenarios, and every one of the archive's 2111 entries decompresses to exactly
the length its BOLT header declares.

## Layout

```
extract_sc64_maps.py        the tool: ROM -> playable maps
extract_briefings.py        ROM -> mission briefings as readable text
verify_maps.py              StormLib-faithful validator for the output
docs/FORMAT.md              BOLT, CHK, MPQ and briefing-script notes
reference/
  README.md                 provenance and licensing
  BOLTextract-cpp/          heinermann's original C++ extractor (GPL-3.0)
  bolt_extract_all.py       Python rewrite of it; dumps all 2111 files
  StormLib/                 Zezula's reference MPQ implementation (MIT)
  mpyq-forks/               four mpyq versions + a fix for their sector bug
```

## Credits

- [Adam Heinermann](https://github.com/heinermann) — reverse-engineered the
  BOLT format and its N64 compressor, which is the hard part of this problem.
- [Ladislav Zezula](https://github.com/ladislav-zezula) — StormLib, the
  reference for everything MPQ.
- The [staredit.net](https://staredit.net/topic/18209/) threads where the
  format was worked out in the open.
