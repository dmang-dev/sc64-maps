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

Two caveats worth knowing:

- **mpyq cannot read the result.** It compares a sector's stored size against
  every remaining byte in the file rather than against that sector's own plain
  size, so it tries to decompress verbatim sectors in any multi-sector file.
  That is an mpyq bug, not a defect in the output.
- Readers that compute sector counts as `size // sector_size + 1` (mpyq again)
  miscount files whose size is an exact multiple of the sector size. StormLib
  uses `((size - 1) / sector_size) + 1`, which this project matches.
