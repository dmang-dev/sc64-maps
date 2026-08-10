# reference/

Prior art this project is built on, vendored so the source that the
implementation was derived from stays alongside it.

## BOLTextract-cpp/

Adam Heinermann's original C++ BOLT extractor, upstream at
<https://github.com/heinermann/BOLTextract> (commit `8f79b7a`, 2024-03-10).
Unmodified apart from removing `.git/`.

This is where the hard work was done. BOLT is Mass Media's archive format,
used across their N64/GBA/Dreamcast/Xbox catalogue, and its N64-era compressor
had to be reverse-engineered from scratch — the header comment in `n64.cpp`
says the algorithm was "entirely guessed". The staredit.net threads
([8508](https://staredit.net/topic/8508/),
[18209](https://staredit.net/topic/18209/)) track that work; the earlier thread
ends with the maps still unreadable because they are compressed, and the later
one with the compression solved.

The files that matter:

| File | Contents |
|---|---|
| `BOLT/bolt-extract/bolt.h` | archive and entry structures |
| `BOLT/bolt-extract/bolt.cpp` | archive discovery and directory tree walk |
| `BOLT/bolt-extract/n64.cpp` | the N64/GBA decompressor |
| `BOLT/bolt-extract/guess_type.cpp` | file type sniffing (no filenames survive) |

Building it needs Visual Studio 2019 and a `cxxopts` submodule that is not
vendored here, and it only accepts `z64` ROMs. That is what motivated the
Python rewrite.

**Licence: GPL-3.0.** Because the container walk and decompressor in this
project are derived from it, the whole project is GPL-3.0-or-later.

## bolt_extract_all.py

A standalone Python rewrite of the above, covering the N64/GBA algorithm.

Run it directly to dump every file in the archive:

```bash
python reference/bolt_extract_all.py "StarCraft 64 (USA).n64" out/
```

Differences from the original: no build step and no dependencies, all three N64
byte orders accepted rather than `z64` only, and errors are reported per entry
instead of aborting. `guess_type.cpp` is ported too, so output files get the
same guessed extensions.

On the USA ROM it writes 2111 files — 868 `.grp`, 504 `.unk`, 281 `.unkimg`,
168 `.txt`, 139 `.unkpal`, 96 `.chk`, 46 `.unkpcm`, 6 `.fnt`, 3 `.tbl`. Those
96 `.chk` files are the same 96 scenarios `../extract_sc64_maps.py` finds,
which is a useful independent check on both.

One upstream bug was not carried over: `get_num_entries()` falls off the end
without returning a value for non-Xbox archives, which is undefined behaviour
that happens to work in practice (the caller re-applies the same `0 → 256`
fallback, so StarCraft 64 output is unaffected).

The type-sniffing structs in `guess_type.cpp` are documented as big endian but
read natively, with only `width`/`height` swapped by hand; the port reads them
as big endian throughout. The two readings were checked against each other over
all 2111 entries and classify identically (281 `.unkimg`, 139 `.unkpal`).

## StormLib/

Ladislav Zezula's MPQ implementation, upstream at
<https://github.com/ladislav-zezula/StormLib>. Vendored as the reference for
the MPQ writer in `../extract_sc64_maps.py` and the reader in
`../verify_maps.py` — it is what the StarCraft tool ecosystem is built on, so
its behaviour defines what "a valid map" means in practice.

Nothing here is compiled or linked; it is read, not built. The specific
behaviour the writer depends on is in `src/SFileReadFile.cpp`:

- **line 56** — the sector offset table is only consulted when a compression
  flag is set on the block entry
- **lines 108-121** — a sector holds `min(sector_size, bytes_remaining)` plain
  bytes; its stored length comes from the sector offset table
- **line 165** — a sector is decompressed only when its stored length is
  *strictly less* than its plain length; equal means stored verbatim

See [../docs/FORMAT.md](../docs/FORMAT.md) §4.1 for why that matters.

**Licence: MIT.**

## mpyq-forks/

`mpyq` is the usual pure-Python MPQ reader, and the obvious thing to validate
this project's output with. It **cannot read the maps this project writes**,
and it is wrong to do so — the bug is in mpyq. Four versions are vendored here
so the claim can be checked rather than taken on faith:

| Fork | Revision | Notes |
|---|---|---|
| `eagleflo/` | `6bfba18` (2021-03-02) | upstream, the version on PyPI as 0.2.5 |
| `Zahgon/` | `6bfba18` (2021-03-02) | identical to upstream |
| `a-sakharov/` | `1870cae` (2026-03-30) | adds PKWARE implode support |
| `oaken-source/` | `20bc045` (2023-08-30) | reworks sector reading |
| `TheSil/` | `2a61650` (2024-10-26) | merges oaken-source's work |

**All five share the same two defects**, in `read_file`:

1. A sector is treated as compressed when `sector_bytes_left > len(sector)` —
   comparing its stored size against *every remaining byte in the file* rather
   than against that sector's own plain size. In any file longer than one
   sector this is true for all but the last sector, so verbatim-stored sectors
   get fed to the decompressor and it raises `Unsupported compression type`.
   StormLib compares against `min(sector_size, bytes_remaining)`
   (`SFileReadFile.cpp:108-121` and `:165`).
2. Sector count is `size // sector_size + 1`, which over-counts by one when the
   file size is an exact multiple of the sector size. StormLib uses
   `((size - 1) / sector_size) + 1`.

### How defect 1 got there

It was introduced by a fix for a *different*, real bug.
[PR #26](https://github.com/eagleflo/mpyq/pull/26) (merged 2014-01-05) noticed
that the **last** sector of a file may be stored uncompressed, which the
then-current file-level check (`block_entry.size > block_entry.archived_size`)
got wrong. The PR description cites StormLib's `SFileReadFile.cpp` and
describes the correct rule exactly — "they cut down the expected sector size if
the last sector isn't big enough" — but the code shipped
`sector_bytes_left > len(sector)`, which only reduces to the right answer *for
the last sector*, where `bytes_remaining == min(sector_size, bytes_remaining)`.
Raw sectors anywhere else in a file still get sent to the decompressor.

So `min(sector_size, sector_bytes_left)` is not a competing approach; it is
what PR #26 set out to implement, generalised from the last sector to every
sector.

### The patch

`sector-fix.patch` corrects both defects against `eagleflo/mpyq.py` — two
changed lines and one added line.

```bash
cd reference/mpyq-forks/eagleflo && git apply ../sector-fix.patch
```

Verified two ways:

- **Fixes the maps.** With the patch applied, all 96 generated maps round-trip
  through mpyq and the recovered `staredit\scenario.chk` is byte-identical to
  the CHK taken from the ROM. Without it, all 96 fail with
  `Unsupported compression type`.
- **Regresses nothing.** Run over upstream's own fixtures — including
  `test/last_sector_compression.s2ma`, which PR #26 added specifically to
  demonstrate the last-sector case — stock and patched mpyq extract all 52
  contained files byte-identically.

### Upstreamed

Submitted as [eagleflo/mpyq#39](https://github.com/eagleflo/mpyq/pull/39), from
the fork at [dmang-dev/mpyq](https://github.com/dmang-dev/mpyq) (branch
`fix-multi-sector-uncompressed`). The PR carries the fix plus a test that builds
small archives at runtime rather than adding another binary fixture, so the
layouts under test are visible in the diff: verbatim, deflated and mixed
multi-sector files, an exact multiple of the sector size, a single short sector,
and an empty file. It also asserts the incompressible fixture really did end up
stored verbatim, so the test cannot quietly stop exercising the path it targets.

Upstream has been quiet for a while (newest merged PR is from 2020; #35, #36 and
#37 are still open), so the fork may end up being the practical route. Their CI
is configured `on: [push]` rather than `on: [pull_request]`, so no checks run on
PRs from forks — the suite was run locally instead (8 passed, and the two
targeted tests fail without the fix with `Unsupported compression type: 229`).

This affects any MPQ holding a multi-sector file with incompressible content,
not just these maps.
