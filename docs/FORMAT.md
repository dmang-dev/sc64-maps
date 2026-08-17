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

Confirmed empirically as well: `008-00A T1) Wasteland.scm` opens in **StarEdit**
with no errors. That runs Blizzard's own `storm.dll`, so it exercises the retail
MPQ and CHK load paths rather than a reimplementation — terrain, units, player
types, `FORC` names resolved through `STR`, and `TRIG` all read correctly.

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

## 4.4 How the N64 maps compare to the stock PC maps

`compare_with_stock.py` reads the maps installed with PC StarCraft — which are
encrypted and PKWARE-imploded, unlike the ones this project writes — and diffs
their `scenario.chk` against the CHKs taken straight from the ROM, matching on
scenario name.

**Campaign missions cannot be compared.** A modern install keeps them in the
CASC store under `Data\`, not in the legacy MPQs; `Maps\campaign\` holds only
the five *Enslavers* bonus maps, none of which are in StarCraft 64. Comparison
is therefore limited to the melee and scenario maps under `Maps\`, which covers
22 of the 96 N64 scenarios.

Of those 22:

- **18 have byte-identical terrain.** The `MTXM` section matches exactly.
- **The other 4 differ by a handful of tiles** — *Triumvirate* 2 of 16384
  (0.01%), *Volcanis* 4 of 9216 (0.04%), *Dire Straits* 66 (0.40%), *Old
  Faithful* 192 (1.17%). Deliberate small edits, not corruption.
- **Unit data differs in exactly one field.** Across 1735 differing records,
  the only field that ever changes is the 4-byte **serial** (class instance id)
  at offset 0. Position, type, owner, hit points, shields, energy, resources,
  hangar contents, state flags and unit links are identical in every single
  record. A serial is an arbitrary per-map counter with no gameplay meaning, so
  the unit layouts are effectively the same maps with renumbered instances.
- **Triggers were rewritten.** Trigger counts usually match (3 vs 3) but the
  bytes differ, which is expected — the console port re-did victory conditions
  and messaging.

That is a strong fidelity result for the extractor: where a direct comparison
is possible, the recovered data matches Blizzard's originals down to the byte
in most sections, and the differences that remain are ones Mass Media actually
made rather than artifacts of extraction.

Eight further N64 melee maps have a stock counterpart under a slightly
different name (*Lost Temple* vs *The Lost Temple*, *Tarsonis Orbital* vs
*Tarsonis Orbital Platform*, *Opposing Cities* vs *Opposing City States '98*)
or a different revision, and are not auto-matched.

### Reading genuine maps

`compare_with_stock.py` reads 277 of 323 stock maps. All 46 failures sit in
third-party `ladder\*` folders and are map-protector artifacts; **every
Blizzard-shipped map reads** — 44/44 in the root, 91/91 under `BroodWar`, 5/5
under `campaign`. Two things are required that our own maps never need:
block-entry decryption, and PKWARE DCL explode (`pkware_explode.py`, whose
Huffman tables are parsed out of the vendored StormLib source rather than
transcribed). Note that an `MPQ_FILE_IMPLODE` file carries **no** compression
mask byte — the whole sector is PKWARE data — while `MPQ_FILE_COMPRESS`
prefixes one.

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

Directory 003 in fact pairs 1:1 with the campaign at the **same offset the
briefings use**: `003/i ↔ 007/i ↔ 008/(i+8)` for i = 0x00–0x3B, covering exactly
the 60 campaign maps, with `003/03C` the credits pairing with nothing. So
61 = 60 missions + credits. Directory 004's 13 slideshows stand in for the PC
FMV cinematics; their narrative order is **file order, not slide index** — slide
indices are asset-bank offsets (`SLIDEFADEUP n` → image `004/(0x13+n)`), one
slide is referenced by nothing and another is shared by two scripts, so no total
order exists in them.

`extract_glue.py` handles both, and is lossless on all four ROM releases.

### 5.6 Assets are shared with the PC release, byte for byte

The glue scripts reference PC asset paths, and the assets themselves match:

- 48 of 49 referenced paths resolve in the PC MPQs as written; 44/44 of the
  fully-qualified ones resolve in a Remastered CASC storage.
- **18 of 19 background images are byte-identical** to the RLE-decoded pixel
  data of the corresponding PC `.pcx`. The one mismatch is the blank plate: PC
  fills it with palette index 254, the N64 with 0.
- **19 of 19** 48-byte font ramps are byte-identical to the 48×1 pixel row of
  the PC `glue\pal??\tfont.pcx`.
- Exactly one N64-original asset exists — `starfield`, present in neither the
  MPQs nor CASC, reachable only from the credits.

The 20 triplets at `003/(0x3D + 3n)` are (image, palette, 48-byte font ramp).
Images are 640×480 8bpp, 307,216 bytes = 16-byte big-endian header + 640·480.
Palettes are 518 bytes = 6-byte prefix + 256 big-endian RGBA5551 words, and are
**not** a simple bit-truncation of the PC palette (`v>>3` matches 77/256,
round-to-nearest 56/256); the exact quantiser is unidentified.

## 6. Putting briefings back: the MBRF section

PC StarCraft stores campaign briefings as trigger records in `MBRF`, which is
**byte-identical in layout to `TRIG`**: 2400 bytes per record = 16 conditions ×
20 + 64 actions × 32 + a u32 execution-flags word + a 27-byte executed-for-player
array + 1 byte of current action. `MBRF` sits immediately after `TRIG`.

Condition record, 20 bytes: location u32@0, group u32@4, quantity u32@8, unit
u16@12, comparison u8@14, condition-type u8@15, resource u8@16, flags u8@17,
mask u16@18. **Every shipped briefing trigger uses exactly one condition,
opcode 13, with an otherwise all-zero body.**

Action record, 32 bytes: location u32@0, string index u32@4, wav string index
u32@8, time u32@12, group1 u32@16, group2 u32@20, unit type u16@24, action type
u8@26, modifier u8@27, flags u8@28, then 3 zero pad bytes. In `MBRF` the
location and group2 fields are unused.

| Opcode | Action | Fields used |
|---|---|---|
| 0 | *(padding)* | |
| 1 | Wait | time |
| 2 | Play WAV | wav, time |
| 3 | Display Text | string, time |
| 4 | Mission Objectives | string |
| 5 | Show Portrait | unit type = unit id, group1 = slot, flags `0x10` |
| 6 | Hide Portrait | group1 = slot |
| 7 | Display Speaking Portrait | group1 = slot, time |
| 8 | Transmission | group1, string, wav, time, modifier |

There are exactly **four portrait slots**, 0–3, carried in `group1`. The wav
field at +8 is a **direct index into `STR`** holding a path string — not an
index into the `WAV ` section, which is only a registration list of those `STR`
indices.

### 6.1 Which idiom to emit

All **433** Transmission actions across 67 genuine campaign scenarios carry a
non-zero wav index — no exceptions. The N64 briefings have no audio, so this
project emits Blizzard's wav-free sequence instead of opcode 8:

```
ShowPortrait → DisplaySpeakingPortrait → DisplayText → Wait
```

Whether a wav-*less* opcode 8 works at all is unknown and needs the engine to
settle; if it does, each line collapses from three actions to one.

### 6.2 Timing has to be invented

The N64 script format carries **no timing information whatsoever** — no
durations, no waits, nothing. Every duration is synthesised. Fitting over 336
N64 lines matched to genuine campaign transmissions gives

```
t = 73.08 × characters + 517   ms
```

with a median absolute residual of ~1.2 s and p90 ~3.6 s. Across all 433
campaign transmissions the ms-per-character median is 74.1 (p10 61.2, p90 95.0),
so the underlying spread is about 1.5× between fast and slow lines — a
per-character model cannot do materially better. Emitted times round to 100 ms,
floor 1500, cap 45000.

### 6.3 The `STR` trap

Injecting a briefing means allocating new `STR` entries, which means knowing
which entries are already referenced. The referencing sections are `SPRP`,
`FORC`, `UNIS`/`UNIx`, `SWNM`, `MRGN`, `WAV `, `TRIG` and `MBRF`.

The one that bites: the custom-unit-name array in `UNIS`/`UNIx` is a `u16[228]`
at a **fixed offset of 3192**, and it is *not* the last field — the base-weapon-
damage and upgrade-bonus arrays follow it. Deriving the offset by measuring back
from the section end (4048 bytes for `UNIS`, 4168 for `UNIx`) lands 400 or 520
bytes late, inside the weapon tables, which reads damage integers as string
indices and misses every real unit name. The consequence is silent and ugly:
briefing dialogue gets allocated over a live unit name and displays as that
unit's name in game.

`check_string_reuse.py` guards this, deliberately hardcoding its own copy of the
offset and re-reading originals from the ROM so it shares no code with the
injector.

`STR` growth is otherwise a non-issue: injecting every briefing costs ~84 KB
across all 96 maps and the largest resulting section is ~18 KB against the u16
offset ceiling of 65,535.

### 6.4 The console paced briefings by hand, so durations can be absent

The N64 briefing screen is **paged**: it shows a counter such as `1/9`, one
speaker at a time in a **single** portrait frame with the speaker's name
rendered as text above the dialogue, and a **Next** button. The player
advances it. One `<TEXT>` block in the dir-007 script equals one page — the
9-page briefing observed on the console is `007/028` (*First Strike*), whose
script has exactly nine `<TEXT>` blocks.

That design has two consequences for the data:

- **Durations are meaningless**, because nothing auto-advances.
- **One portrait slot is sufficient**, because identity is carried by the name
  text and by one-speaker-per-page, not by which frame is lit.

Twelve of the 96 scenarios ship a populated `MBRF`. Ten carry real durations.
One (`008/01D`) is an empty stub. The last, **Resurrection IV (`008/065`)**,
has 24 timed actions with **no durations at all** and drives a single portrait
slot, swapping the unit id per line. Read against the console's design that is
coherent and complete data — not something half-finished.

It is nonetheless unplayable *on PC*, where `MBRF` is timed-only and there is
no wait-for-input opcode. Something has to give, and there are two defensible
answers:

| | Result |
|---|---|
| Rebuild from the script (default) | consistent with the other 58 briefings, uses PC's four portrait slots |
| `patch_timings=True` | fills only the durations, keeping the one-portrait presentation intact |

`mbrf_is_unusable()` detects the case; `patch_zero_durations()` implements the
faithful alternative.

**A caution on evidence.** An earlier version of this section argued the
section was vestigial because footage showed the console using two portrait
slots. That footage was the *fan recreation* of Resurrection IV by Zero and
Drake Clawfang, running in PC StarCraft — it shows the PC briefing UI, not the
N64 one. Recreations of the N64-exclusive maps circulate widely and look
plausible at a glance; check the source before treating one as console
behaviour.

### 6.5 Edition trap

Five portraits map to unit ids that mean different units in the two editions —
88 is Artanis in Brood War and Merc Biker in original StarCraft, 98 is Raszagal
versus Greedo. A briefing using ids 17–21 must therefore land in a `.scx`. The
converter checks this against the map's own version stamp and refuses rather
than writing a wrong face. In this ROM nothing actually conflicts.

## 7. CASC (Remastered and later)

Modern installs replaced MPQ with CASC, and StarCraft: Remastered keeps its
campaign maps there rather than in the legacy archives. The chain to one file:

```
.build.info              pipe-separated, gives the build config's MD5
Data/config/xx/yy/hash   build config: names the ENCODING and ROOT files
Data/data/*.idx          EKey prefix -> (archive, offset, encoded size)
Data/data/data.NNN       the archives, holding BLTE blobs
ENCODING                 CKey -> EKey
ROOT                     name -> CKey
```

So: **name → CKey → EKey → archive/offset → BLTE**.

Layout traps worth knowing, each of which cost time here:

- The `.idx` bucket files live in **`Data/data/`** alongside the archives, not
  in `Data/indices/` — that holds CDN-style `.index` files a local storage
  never consults.
- A `FILE_EKEY_ENTRY` is a 9-byte EKey prefix + 5-byte **big-endian** storage
  offset + 4-byte **little-endian** encoded size; the top 10 bits of the 40-bit
  offset select the `data.NNN` archive.
- Every blob is preceded by a 30-byte span whose first 16 bytes are the EKey
  **stored back-to-front**.
- `FILE_CKEY_ENTRY.EKeyCount` is **little-endian** while `ContentSize` beside it
  is big-endian. Reading the count big-endian silently yields exactly one entry
  per page.
- StarCraft I uses the plain-text root handler (`TRootHandler_SC1`), not TVFS,
  MNDX or the WoW handler.

BLTE frames may be `N` stored, `Z` zlib, `4` LZ4, `F` recursive or `E`
Salsa20-encrypted. A survey of 181,119 frames in the StarCraft storage found
**zero encrypted frames**, so the missing-key problem does not arise for this
product — though it would for WoW or Overwatch, where keys come from the
community TACT key list (CascLib regenerates its table from wowdev.wiki via
`wiki2cppkeys.py`).

## 8. Reading MPQ files with no name

MPQ derives a file's decryption key from its filename, so a nameless encrypted
file looks unreadable — and the retail campaign maps are exactly that: 68 blocks
in the large installer archives, referenced by no listfile.

StormLib recovers the key from content instead
(`SBaseCommon.cpp:548-679`). A compressed and encrypted file begins with its
sector offset table, whose first entry is predictable — `(sector_count + 1) * 4`
— which pins the key, and whose second entry bounds it. `FIX_KEY` (`0x20000`)
means the effective key is `((base_key + block_offset) ^ file_size)`.

`mpq_keycrack.py` implements this: **4,732 of 4,746 encrypted blocks recovered,
99.7%**. The 14 failures are the whole failure class and are unfixable by this
technique — encrypted-but-uncompressed 2-byte stubs, which have no sector offset
table to attack.

Two cautions. The first-DWORD test does **not** leave a unique candidate; it
averages 1.91, and the safety comes from the second-DWORD bound plus validating
the whole offset table for monotonicity. That table check is degenerate for
single-sector files, so those rest on the second DWORD alone.

## 9. The melee Scenario list

StarCraft 64 has a melee mode, and it matters more than it sounds. A PC ladder
map injected into a **campaign** BOLT slot loads and renders perfectly well, but
resolves to an instant `Victory` — the slot applies campaign mission-end logic
to a map carrying no campaign triggers, so the end condition is met on frame
one. The same map in a **melee** slot plays normally: resources, supply counter,
no premature win.

### 9.1 Reaching it

From the title screen, `Start` twice reaches the main menu. That menu shows a
race on the left and an episode on the right, and **D-pad LEFT** cycles them
together:

| presses | race | episode | logo |
|---|---|---|---|
| 0 | Terran | I | StarCraft |
| 1 | Zerg | II | StarCraft |
| 2 | Protoss | III | StarCraft |
| 3 | Protoss | IV | BroodWar |
| 4 | Terran | V | BroodWar |
| 5 | Zerg | VI | BroodWar |

then wraps. So there is **no separate Brood War mode** — Brood War is episodes
IV–VI of one selector, and both campaigns run the same map loader. The selector
byte is at RAM `0x800DD937`, holding `episode mod 6`. (The ROM also carries
"Expansion Pak required for Broodwar Missions" at `0x0D182C`, so the expansion
campaign is gated on the 4 MB Expansion Pak.)

`A` from the main menu opens mission select, whose header reads
`[Episode N] [Scenario] [Load Saved]`. **D-pad RIGHT** moves to `Scenario`, and
that list is the melee mode.

### 9.2 The table

Three structures sit together in the static segment:

| what | file offset | RAM | layout |
|---|---|---|---|
| label strings | `0x0D15F4` | `0x800D09F4` | NUL-terminated, entries pre-padded with two spaces |
| pointer array | `0x0D16BC` | — | 11 × big-endian pointer |
| records | `0x0D16E8` | — | 10 × `{u8 map_id, u8 opponents}` |

`map_id + 60` is the map index, and `map_id + 68` the BOLT file number in
directory `008`. 60 is Challenger, the first melee map.

| # | record | map_id | opp | index | BOLT | label | scenario name |
|---|---|---|---|---|---|---|---|
| 1 | `0b 01` | 11 | 1 | 71 | `008/04F` | `1v1 Blood Bath` | Blood Bath |
| 2 | `00 01` | 0 | 1 | 60 | `008/044` | `1v1 Challenger` | Challenger |
| 3 | `03 01` | 3 | 1 | 63 | `008/047` | `1v1 Discovery` | Discovery |
| 4 | `08 02` | 8 | 2 | 68 | `008/04C` | `1v2 Triumvirate` | Triumvirate |
| 5 | `0b 02` | 11 | 2 | 71 | `008/04F` | `1v2 Blood Bath` | Blood Bath |
| 6 | `18 02` | 24 | 2 | 84 | `008/05C` | `1v2 Hunters` | The Hunters |
| 7 | `11 03` | 17 | 3 | 77 | `008/055` | `1v3 Power Lines` | Power Lines |
| 8 | `0e 03` | 14 | 3 | 74 | `008/052` | `1v3 Brushfire` | Brushfire |
| 9 | `18 04` | 24 | 4 | 84 | `008/05C` | `1v4 Hunters` | The Hunters |
| 10 | `5f 01` | 95 | 1 | — | — | ` *Mass Hysteria*` | (see 9.5) |

Three things establish the decode rather than merely fitting it. The
`opponents` column reads `1,1,1,2,2,2,3,3,4`, matching every `1vN` label.
`map_id` repeats exactly where the list repeats — `0x0B` twice for Blood Bath,
`0x18` twice for Hunters. And nine of ten resolve to the correct scenario name
read straight out of the referenced CHK. Reading the byte as a *raw* map index
instead yields campaign missions ("T12) The Hammer Falls" for entry 1), so the
`+60` base is not optional.

This also confirms the community static-address rule on live data:
`file = RAM − 0x80000000 + 0xC00` maps `0x800D09F4` to `0x0D15F4` exactly.

### 9.3 Patching here needs a checksum repair

`0x0D16E8` is 857,832, which is **inside** the CIC boot checksum window
`0x1000`–`0x101000`. Patch it and leave the header alone and IPL3 refuses to
hand off: the ROM boots to a black screen, RAM never gets a map index, and the
failure looks exactly like "the patch had no effect".

BOLT starts at `0x12CA10`, past the window, which is why swapping map data
never needed this and why the trap is easy to walk into.

The two checksum words are at header `0x10` and `0x14`, big endian. Rather than
assume CIC-6102 because it is the common case, compute with every seed and keep
whichever reproduces the ROM's own stored value; for the USA cart that is the
6101/6102 seed `0xF8CA4DDC`, reproducing `0x0684FBFB 0x5D3EA8A5`.

### 9.4 What can be changed

* **10 list entries**, each repointed by writing two bytes.
* **36 reachable slots.** `map_id` is a `u8`, so indices `60`–`315` are
  expressible but only `60`–`95` exist: the melee maps plus the bonus maps
  (Orbital Death, Eruption, Pro Bowl, Round-Up, King of the Hill, Old Faithful,
  Guardians, Zerg Troopers, Resurrection IV, Rage, Mass Hysteria).
* **Campaign slots are unreachable** from this list — the `+60` base floors it.
* The **opponent count** is per entry, independent of the map.

Verified on hardware semantics in an emulator: writing `map_id = 0x0E` to
record 2 made that entry load index 74 (Brushfire). Injecting a PC ladder map
into `008/05D` (index 85, a slot no list entry uses) and pointing record 2 at
`map_id = 0x19` with 3 opponents loaded it as a 1v3 melee game — so injection
and repointing compose, and a ladder map can be added without displacing any
map the stock list shows.

A melee slot does **not** constrain the injected map's dimensions: a 128×112
map runs in Blood Bath's 64×64 slot. The map's own `DIM` governs.

### 9.5 The eleventh entry is dead data

The pointer array has 11 entries; the last is `" *Mass Hysteria*"`, and it never
appears in the rendered list. Its record is `5f 01`, and `0x5F` is 95 — exactly
Mass Hysteria's own map index (`008/067`), which is too precise to be
coincidence. But under the `+60` rule that record means index 155, and the
cartridge holds 96 maps.

Patching that record to a valid `map_id` does **not** make an eleventh item
appear: the list still renders exactly ten, and a cursor driven ten steps down
wraps to `Setup Custom`. So the list length is fixed in the menu code rather
than derived from the pointer array, the record was evidently written in a
different convention, and nothing ever exercised it. Enabling the entry means
finding that length constant — it is not in the bytes adjacent to the table,
which are a separate ascending run (`07 0c 11 14 16 19 1d 20 28 31 3b`).
