# Format notes

Everything here was confirmed against the USA StarCraft 64 ROM
(internal name `STARCRAFT 64`, cart id `NSQE`, 32 MiB, BOLT archive built
1999-11-08 14:13:43).

## 1. ROM byte order

N64 dumps ship in three interleavings of identical bytes, identified by the
first four bytes of the header:

| Magic | Name | Layout |
|---|---|---|
| `80 37 12 40` | z64 | big endian, the console's native order |
| `37 80 40 12` | v64 | 16-bit byte pairs swapped |
| `40 12 37 80` | n64 | 32-bit words reversed |

Trust the magic, not the extension. The widely circulated
`StarCraft 64 (USA).n64` is actually **v64** data — its header reads
`37 80 40 12`, and the internal name at `0x20` deswaps from `TSRARCFA T46` to
`STARCRAFT 64`. Everything downstream assumes z64.

## 2. BOLT archive

BOLT is Mass Media's container format, used across their N64/GBA/Dreamcast/Xbox
titles. In this ROM it starts at `0x12CA10`. All integers are big endian, and
**all offsets are relative to the `BOLT` magic**, not to the start of the ROM.

Header (16 bytes):

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `'BOLT'` |
| 4 | 4 | build time: hour, minute, second, millisecond |
| 8 | 3 | build date: month, day, year − 1900 |
| 11 | 1 | entry count (0 means 256) |
| 12 | 4 | end offset |

Entry (16 bytes), with the root entry array following the header immediately:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | flags — bit `0x08` means stored uncompressed |
| 1 | 2 | unknown |
| 3 | 1 | file type, or child count when this is a directory |
| 4 | 4 | uncompressed size |
| 8 | 4 | data offset |
| 12 | 4 | file hash — **0 marks a directory** |

Directories point at a child entry array. StarCraft 64's tree is two levels
deep: 23 top-level directories holding 2111 files in total. Every scenario
lives in directory `008`, at indices `008`–`067`.

Filenames are not recoverable. The archive stores only hashes, and the hash is
not reversible — this was chased and abandoned in the staredit.net thread.
Files are therefore addressed by tree position, e.g. `008/065`.

### 2.1 The N64/GBA compressor

An LZSS variant. Output is built by consuming control bytes; two accumulators
(`ext_offset`, `ext_run`) carry extra bits forward, and `op_count` counts
control bytes consumed since the last emit. All three reset after every emit.

| Control byte | Meaning |
|---|---|
| `0xxxxxxx` | back-reference (see below) |
| `1000xxxx` | literal run of `(ext_run << 4 \| low nibble) + 1` bytes, copied from the stream |
| `11xxxxxx` | `ext_offset = (ext_offset << 6) \| (byte & 0x3F)` |
| `101xxxxx` | `ext_run = (ext_run << 5) \| (byte & 0x1F)` |
| `1001xxxx` | `ext_run = (ext_run << 2) \| (byte & 3)`, `ext_offset = (ext_offset << 2) \| ((byte >> 2) & 3)` |

For a back-reference:

```
distance = ((ext_offset << 4) | (byte & 0xF)) + 1
length   = ((ext_run    << 3) | (byte >> 4))  + op_count + 1
```

then copy `length` bytes from `distance` back in the output, one at a time so
overlapping runs work. Note `op_count` in the length — the cost of the control
bytes you just spent is folded into the run, which is what makes the encoding
compact and is easy to miss.

Decoding stops when the entry's declared uncompressed size is reached. Across
all 2111 entries this lands exactly on the declared size every time, which is a
strong signal the algorithm is fully correct.

## 3. CHK scenarios

A CHK is a flat chain of sections:

```
[4-byte tag][int32 size, little endian][size bytes of data]
```

The N64 build stores these **identical to the PC format**, including
little-endian integers — the port kept Blizzard's data layout and byte-swaps
elsewhere. That is the whole reason this conversion is possible at all. A
chunk is treated as a scenario when it starts with `TYPE`, `VER ` or `IVER`,
its section chain lands exactly on the end of the buffer, and it holds at least
ten sections.

Sections that matter for identification:

| Tag | Contents |
|---|---|
| `VER ` | format version: 59 = StarCraft, 63 = hybrid, 205 = Brood War |
| `TYPE` | `RAWS` (StarCraft) or `RAWB` (Brood War); absent on the oldest maps |
| `DIM ` | width, height in tiles |
| `ERA ` | tileset index (low 3 bits) |
| `OWNR` | per-slot ownership; 5 = computer, 6 = human |
| `SPRP` | scenario name and description, as indices into `STR ` |
| `STR ` | string table: count, then per-string offsets, then NUL-terminated text |

Later sections override earlier ones of the same tag, which real maps rely on.

The 96 scenarios split as 59 vanilla StarCraft, 6 hybrid, and 31 Brood War.
Mission briefings are **not** in the CHKs — they live as separate text files in
directory `007` (96 of them, matching the 96 scenarios) using a tag syntax like
`</BACKGROUND ...>` that has no PC equivalent. Briefings are therefore not
carried over.

## 4. MPQ wrapper

The N64 has no Storm, so scenarios sit bare in the BOLT archive. PC StarCraft
loads maps as MPQ archives, so one has to be built. The only member the game
requires is `staredit\scenario.chk`; this project also writes a `(listfile)`
so editors show something sensible.

Header, format version 1 (32 bytes, little endian): magic `MPQ\x1A`, header
size, archive size, format version `0`, sector size shift `3` (⇒ 4096-byte
sectors), then the hash table offset, block table offset, and their entry
counts.

**Hash table** — power-of-two sized, 16 entries here. A filename maps to a
starting slot via `hash(name, 0) & (size - 1)`, then linear probing. Each entry
holds two more independent hashes of the name (`hash(name, 1)`, `hash(name, 2)`),
a locale and platform word, and a block index. Free slots use `0xFFFFFFFF`.
Names are upper-cased with backslash separators before hashing.

**Block table** — file offset, stored size, plain size, flags.

Both tables are encrypted with Storm's algorithm, keyed by
`hash("(hash table)", 3)` and `hash("(block table)", 3)`. The crypt table
itself is generated from seed `0x00100001`.

### 4.1 Why sectors are stored verbatim

Each file is written with `MPQ_FILE_EXISTS | MPQ_FILE_COMPRESS` and a sector
offset table — `(sector count + 1)` little-endian offsets relative to the
file's data, followed by the sectors.

The `COMPRESS` flag is what makes a reader consult the sector offset table at
all (`StormLib/src/SFileReadFile.cpp:56`). The sectors themselves are stored
uncompressed, which is legal: a reader decompresses a sector only when its
stored length is **strictly less** than its plain length
(`SFileReadFile.cpp:165`), where plain length is
`min(sector_size, bytes_remaining)` (`SFileReadFile.cpp:108-121`). Equal
lengths mean "stored as-is". This is the same path Storm takes for any sector
that failed to compress, so it is universally supported — and it means no
compressor has to be shipped.

The alternative of setting no compression flag at all also works in StormLib,
but leaves out the sector offset table and exercises a much rarer code path.

### 4.2 Checked against the 323 maps shipped with StarCraft

Measured against every `.scm`/`.scx` under a retail install's `Maps\`:

| Property | Genuine maps | Ours | Verdict |
|---|---|---|---|
| Format version / header size | 0 / 32, all 323 | same | identical |
| Sector size shift | 3 (4096 B), all 323 | 3 | identical |
| Archive geometry | data → hash → block, contiguous, all 323 | same | identical |
| `scenario.chk` flags | `0x80010200` ×317, `0x80000100` ×4, `0x80030100` ×1 | `0x80000200` | unusual, legal |
| Compression | PKWARE implode only — 22,019 sectors, zero zlib | none (verbatim) | legal, precedented |
| Encryption | encrypted in 318/322 | none | precedented |
| Hash table size | 1024 in 318 | 16 | legal |
| Trailing bytes | 260-byte `NGIS` signature in 206; none in 110 | none | normal |

Three findings settle the design question:

- **Verbatim sectors occur in Blizzard-authored maps.** `(3)Triad.scm` sector
  31 and `(4)Inferno.scm` sector 90 are both stored uncompressed. Retail
  Storm.dll reads them, so the path is exercised by shipped content — not just
  legal on paper.
- **Encryption is not required.** Four genuine, tournament-played iCCup ladder
  maps ship `scenario.chk` with flags `0x80000100` and no `ENCRYPTED` bit.
- **All 96 of our maps pass a full emulation of StormLib's acceptance checks** —
  flag validity under `MPQ_FILE_VALID_FLAGS_SCX`, table positions, sector
  offset table monotonicity, `SOT[0] == table length`, `SOT[-1] == packed
  size`, no oversized sector, and byte-exact read-back of both members.

Two quirks are worth knowing before anyone "fixes" them:

- Our `dwCmpSize` is slightly **larger** than `dwFileSize` (the sector offset
  table is pure overhead when nothing compresses). No real map does this, but
  it is harmless: both StormLib and Storm.dll decide compression from the flags
  and per-sector sizes, never from `dwCmpSize`. Clearing `MPQ_FILE_COMPRESS` to
  "correct" it would break reading outright, because an uncompressed file has
  no sector offset table at all.
- Never set `MPQ_FILE_SINGLE_UNIT` or `MPQ_FILE_SECTOR_CRC` on a map: they
  appear in zero of the 323, and StormLib masks them off for `.scm`/`.scx`
  (`StormLib.h`, `MPQ_FILE_VALID_FLAGS_SCX`), which would desynchronise the
  writer's assumptions from the reader's.

Not compressing costs about 3.9× in size — our scenario data stores at ratio
1.001 against real maps' 0.255. That is size only, not compatibility. PKWARE
implode is the only precedented option if it ever matters.

### 4.3 Reading *genuine* maps

Two traps, both hit during this comparison:

- `dwArchiveSize` is advisory. 199 of 204 sampled maps disagree with the file
  length, because of the appended signature. StormLib recomputes rather than
  trusting the field, so a hard equality check rejects almost every real map.
- Map protectors declare `dwHashTableSize` as `0x10000400` (268 million
  entries). StormLib masks it with `BLOCK_INDEX_MASK` (`0x0FFFFFFF`);
  without that a reader tries to allocate gigabytes.

`verify_maps.py` handles both. It still cannot read genuine maps end to end,
because their `scenario.chk` is encrypted and PKWARE-imploded and it implements
neither — it validates *our* output, and parses real headers and tables only.

Two caveats worth knowing:

- **mpyq cannot read the result.** It compares a sector's stored size against
  every remaining byte in the file rather than against that sector's own plain
  size, so it tries to decompress verbatim sectors in any multi-sector file.
  That is an mpyq bug, not a defect in the output.
- Readers that compute sector counts as `size // sector_size + 1` (mpyq again)
  miscount files whose size is an exact multiple of the sector size. StormLib
  uses `((size - 1) / sector_size) + 1`, which this project matches.

## 5. Mission briefings (BOLT directory 007)

PC StarCraft stores campaign briefings inside the map, as triggers in the CHK's
`MBRF` section. The N64 build does not: briefings are plain-text scripts in
their own BOLT directory, completely separate from the scenarios. That is why
they do not travel with the maps and need `extract_briefings.py`.

Directory 007 holds **119** entries, not 96: the 96 briefing scripts
(`007/000`–`007/05F`, `file_type` 10, first byte `<`) followed by 23 entries
(`007/060`–`007/076`, `file_type` 18) that are **the briefing portrait
bitmaps**. Filtering on directory alone mixes them together.

Do not select scripts by `file_type` either — in directory 003, type 10 covers
both scripts and 48-byte binary font ramps. Select by index range, or by
sniffing the leading bytes.

Briefing `007/i` belongs to map `008/(i+8)`. Both runs are 96 long, contiguous
and in the same order — `007/000` ↔ `008/008` (*Tutorial 1*) through `007/05F`
↔ `008/067` (*Mass Hysteria*). The offset is just where each run starts in its
directory; `008/000`–`008/007` are non-map entries.

### 5.1 Syntax

The 96 scripts are **pure printable ASCII with CRLF line endings**. Across the
whole corpus there is not one byte outside `0x20`–`0x7E` plus CR/LF, and not a
single bare CR or bare LF. Markup is a bare tag, normally alone on its line:

| Tag | Meaning |
|---|---|
| `<OBJECTIVE>` | mission objectives; exactly one per file, always first |
| `<PORTn>` | select portrait *n* for the transmissions that follow |
| `<TEXT>` | a transmission: speaker line, blank line, then body |
| `<TEXTC>` | closing screen text; same internal shape |

A `<TEXT>` block's chunk begins with the newline that ended the tag's own line.
Dropping exactly that one newline, the block is:

```
speaker
                     <- blank
body ...
```

Dropping *all* leading blanks instead is a mistake: a transmission with no
speaker is written with an **empty** speaker line, and collapsing blanks makes
that indistinguishable from one that has a speaker.

### 5.2 Portraits

`<PORTn>` indexes the bitmaps in the same directory: **portrait *n* is BOLT
entry `007/(0x60 + n)`**. All 23 are 3376 bytes — a 16-byte big-endian header
followed by 60×56 8-bit indexed pixels:

| Offset | Size | Value on all 23 |
|---|---|---|
| 0 | 4 | `0x00000008` — bits per pixel |
| 4 | 4 | 0 |
| 8 | 2+2 | `0x003C`, `0x0038` — width 60, height 56 |
| 12 | 4 | 0 |

`16 + 60*56 = 3376`, matching the entry size exactly. The files carry **no
palette**; the briefing screen must bind a shared one from elsewhere (the
518-byte `file_type` 14 entries in other directories are palettes — 6-byte
header plus 256 big-endian RGBA5551 entries).

Ids used are 0–4, 6–9 and 12–22. There is no `PORT5`, `PORT10` or `PORT11`
anywhere in the ROM, though the bitmaps for all three exist — finished artwork
that shipped unused.

**Who each id is does not need inferring.** The first line of every `<TEXT>`
block is the speaker's name, and each block is scoped by the most recent
`<PORTn>`, so the mapping is measured directly from the data. Over the 69
written briefings (480 non-closing transmissions), every id resolves to exactly
one character; only spelling and honorific variants differ:

| id | blocks | speaker labels in the data |
|---:|---:|---|
| 0 | 47 | Advisor |
| 1 | 22 | Zerg Overmind |
| 2 | 28 | Aldaris (also `Aldaris.`, `Protoss High Templar`) |
| 3 | 17 | General Duke / Duke |
| 4 | 6 | Daggoth |
| 6 | 15 | Fenix |
| 7 | 3 | Fenix |
| 8 | 42 | Jim Raynor / Raynor / Jim |
| 9 | 10 | Kerrigan |
| 12 | 71 | Infested Kerrigan / Kerrigan |
| 13 | 35 | Mengsk |
| 14 | 24 | Tassadar |
| 15 | 5 | Zasz |
| 16 | 28 | Zeratul |
| 17 | 23 | Artanis |
| 18 | 23 | Raszagal (also `Raszegal`) |
| 19 | 17 | Stukov |
| 20 | 32 | DuGalle (also `Du Galle`) |
| 21 | 29 | Infested Duran / Duran |
| 22 | 2 | Mr. Slate |

Key any lookup on the **integer**, never the label — the labels are
inconsistent, including two apparent typos. Ids 6 and 7 are both Fenix and
their mission runs do not overlap, which is consistent with the Zealot →
Dragoon change; 9 and 12 split Kerrigan the same way.

### 5.3 Placeholders

27 of the 96 scripts are unwritten placeholders: byte-identical 58-byte files,
contiguous at `007/03C`–`007/056`, carrying the literal body `Blank BRIEFING`.
They pair with the 27 melee maps, which have no briefing. Detect them by that
marker and exclude them from any statistics — they alone supply every
`Blank BRIEFING` line and would otherwise pollute the portrait table.

That leaves **69 written briefings**.

### 5.4 Edge cases

Three files deviate. Two of them cost data if ignored:

- **007/025** contains a `<PORT12` whose `>` was never written — the raw bytes
  are `<PORT12` CRLF `<TEXT>`. A tokeniser that requires the `>` drops the tag
  entirely and the dialogue after it silently inherits the *previous*
  speaker's portrait. A tokeniser using `<([^>]*)>` instead swallows the
  following `<TEXT` as an argument, which is just as wrong. Terminate a tag
  name at the first of `>`, CR, LF or a subsequent `<`. This is the only
  unbalanced angle bracket in directories 003, 004 and 007 combined.
- **007/033** contains a `<PORT8>` whose `<TEXT>` tag was never written. The
  block that follows still has the usual speaker/blank/body shape, so a parser
  that ignores text under a `<PORTn>` silently drops a transmission. Treat it
  as an implicit `<TEXT>`.
- **007/033** also contains a bare `<PORT>` with no digits, followed by the
  only transmission in the corpus whose speaker line is empty. Reads as
  "no portrait / nobody in particular"; parse the id as `None` rather than
  defaulting to 0.
- **007/017** has a `<PORT0>` immediately followed by `<PORT14>` with nothing
  between, so one portrait command is dead — model `<PORTn>` as sticky state
  the next one overwrites, and never assert `count(PORT) == count(TEXT)`. It is
  also the one file where a tag is *not* alone on its line, a `<PORT0>` sitting
  at the end of a prose line. Scan for tags anywhere rather than matching whole
  lines.

`<TEXTC>` deserves its own warning: its payload is always the fixed UI string
`End of Briefing`, never dialogue. Rendering it as a transmission gives every
briefing a phantom speaker of that name.

### 5.5 Related script directories

Two other directories hold scripts in the same family. Neither is a mission
briefing, and they use different markup, so they are not extracted by default.
Both hold binary assets after their script prefix, the same way 007 does:
directory 003 is 61 scripts then 60 asset entries, directory 004 is 13 scripts
then 85.

Note directory 003 is **cp1252, not ASCII** — two files carry a `0x92` curly
apostrophe, which makes `decode('ascii')` and `decode('utf-8')` both raise.
Directory 004 also breaks the "payload starts after CRLF" rule that holds in
003 and 007: 56 of its tags carry their payload inline on the same line as the
closing `>`, so strip a leading CRLF only if one is present.

- **Directory 003** (61 files) — "establishing shot" / glue screens, using
  *double*-angle markup with arguments: `</COMMENT text>`, `</BACKGROUND
  glue\palta\TerranA.pcx>`, `</FONTCOLOR glue\palta\tfont.pcx>`,
  `</DISPLAYTIME 5000>`, `</FADESPEED 100>`, `</PAGE>`, `</SCREENLEFT>`,
  `</SCREENLOWERLEFT>`. The asset paths use PC StarCraft's own naming.
- **Directory 004** (13 files) — slideshow scripts with single-angle markup:
  `<WAIT n>`, `<TEXT1>`, `<TEXT2>`, `<TEXTFADEDOWN>`, `<TEXTSPEED n>`,
  `<SLIDEFADEUP n>`, `<SLIDEFADEDOWN>`, `<SLIDESPEED n>`, `<BORDFADEUP n>`,
  `<BORDFADEDOWN>`.

Neither count (61, 13) matches the 96 missions, so they sit on a different axis
from the briefing/map pairing.
