"""PKWARE DCL implode -- the compressor side of pkware_explode.

Ported statement-for-statement from the vendored
reference/StormLib/src/pklib/implode.c (Ladislav Zezula's reimplementation of
the PKWARE Data Compression Library).  The Huffman tables are not transcribed
here: they are imported from pkware_explode, which parses them out of
reference/StormLib/src/pklib/explode.c at import time, so the encoder and the
decoder can never drift apart.

The port keeps pklib's data structures rather than substituting a "better"
match finder, because the goal is output that real Storm decodes.  Measured
against implode.c compiled from the same vendored source, this module is
byte-identical on every input tried: 4127 assorted jobs (edge cases, random
data at several entropies, all three dictionary sizes, both compression
types) plus all 5531 sectors of the 96 generated maps.

Against Blizzard's *stored* bytes it is close but not equal.  Re-imploding
the 22308 compressed scenario.chk sectors of the 419 stock StarCraft maps
reproduces 18721 of them exactly (83.9%); of the rest, 3076 come out
smaller, 387 the same size with different bytes, and 124 larger, never by
more than 2 bytes.  Overall ratio 0.2530 against Blizzard's 0.2540.  Since
this module matches implode.c exactly, that residue is a difference between
pklib as Zezula reconstructed it and the compressor inside Blizzard's
Storm, not a defect here.  The dictionary-size rule in storm_dict_size
predicts the header Blizzard wrote for all 22308 of those sectors.

Two places where implode.c reads past the end of valid data are reproduced
deliberately:

  * SortBuffer's end pointer is one position past the last valid byte, so the
    byte-pair hash of that last position reads two bytes that are not part of
    the input (implode.c:458-464 documents this and requires the caller to
    zero the work buffer, which Compress_PKLIB does -- SCompression.cpp:258).
    When the stream is short enough to fit one 0x1000-byte block those two
    bytes are the zeros of a freshly cleared buffer; when it is not, they are
    stale bytes left by the previous block, and that is what pklib sees too.
  * FindRep compares forward up to MAX_REP_LENGTH bytes, which on the final
    block can run past the end of the input.  Any repetition it reports is
    clamped against input_data_end before it is emitted (implode.c:502-507),
    so this never corrupts the output; it only influences which of several
    equally valid matches gets chosen.

Deviation from the C, in one pathological case only: work_buff[0x2204] is the
first byte of phash_offs[] in TCmpStruct, and SortBuffer can read it when the
final block carries exactly 0xFFF bytes.  _sort_buffer mirrors phash_offs[0]
into two pad bytes before it runs, which is exact -- that read happens on the
first iteration of the redistribution pass, before anything writes
phash_offs[0].  Reads further past the work buffer (FindRep's forward compare)
land in zero padding instead of whatever followed the struct.

SPDX-License-Identifier: GPL-3.0-or-later
"""
from pkware_explode import (
    DistBits, DistCode, ExLenBits, LenBits, LenCode, ChBitsAsc0, ChCodeAsc,
)

__all__ = [
    "implode", "ImplodeError", "CMP_BINARY", "CMP_ASCII",
    "DICT_SIZE1", "DICT_SIZE2", "DICT_SIZE3", "storm_dict_size",
]

# pklib.h:19-30
CMP_BINARY = 0
CMP_ASCII = 1
DICT_SIZE1 = 1024
DICT_SIZE2 = 2048
DICT_SIZE3 = 4096

MAX_REP_LENGTH = 0x204          # implode.c:26
_USHRT_MAX = 0xFFFF

# TCmpStruct geometry (pklib.h:66-74)
_WORK_BUFF_SIZE = 0x2204
_PHASH_OFFS_SIZE = 0x2204
_PHASH_TO_INDEX_SIZE = 0x900

# Slack so that the deliberate over-reads described above hit zeros instead of
# raising IndexError.  MAX_REP_LENGTH covers FindRep's forward compare.
_PAD = MAX_REP_LENGTH + 4


class ImplodeError(Exception):
    pass


def storm_dict_size(size: int) -> int:
    """The dictionary size StarCraft picks for a buffer of `size` bytes.

    Compress_PKLIB, SCompression.cpp:271-276.  Diablo I always uses 4096;
    StarCraft scales it, and every sector in every stock StarCraft map was
    produced by this rule.
    """
    if size < 0x600:
        return DICT_SIZE1
    if size < 0xC00:
        return DICT_SIZE2
    return DICT_SIZE3


class _Cmp:
    """TCmpStruct plus the three functions that operate on it."""

    def __init__(self, ctype: int, dsize_bytes: int):
        # implode(), implode.c:606-671
        if dsize_bytes == DICT_SIZE3:
            self.dsize_bits, self.dsize_mask = 6, 0x3F
        elif dsize_bytes == DICT_SIZE2:
            self.dsize_bits, self.dsize_mask = 5, 0x1F
        elif dsize_bytes == DICT_SIZE1:
            self.dsize_bits, self.dsize_mask = 4, 0x0F
        else:
            raise ImplodeError(f"invalid dictionary size {dsize_bytes}")
        self.dsize_bytes = dsize_bytes
        self.ctype = ctype

        nchbits = [0] * 0x306
        nchcodes = [0] * 0x306
        if ctype == CMP_BINARY:
            for i in range(0x100):
                nchbits[i] = 9
                nchcodes[i] = i * 2
        elif ctype == CMP_ASCII:
            for i in range(0x100):
                nchbits[i] = ChBitsAsc0[i] + 1
                nchcodes[i] = (ChCodeAsc[i] * 2) & 0xFFFF
        else:
            raise ImplodeError(f"invalid compression type {ctype}")

        n = 0x100
        for i in range(0x10):
            for c2 in range(1 << ExLenBits[i]):
                nchbits[n] = ExLenBits[i] + LenBits[i] + 1
                nchcodes[n] = ((c2 << (LenBits[i] + 1)) | (LenCode[i] * 2) | 1) & 0xFFFF
                n += 1
        assert n == 0x306
        self.nchbits = nchbits
        self.nchcodes = nchcodes

        self.dist_bits = DistBits
        self.dist_codes = DistCode

        self.work_buff = bytearray(_WORK_BUFF_SIZE + _PAD)
        self.phash_offs = [0] * (_PHASH_OFFS_SIZE + _PAD)
        self.phash_to_index = [0] * _PHASH_TO_INDEX_SIZE
        self.offs09BC = [0] * (MAX_REP_LENGTH + 8)

        self.distance = 0
        self.out = bytearray(2)
        self.out_bytes = 0
        self.out_bits = 0

    # -- OutputBits, implode.c:110-146 -------------------------------------
    # FlushBuf's 0x800-byte windowing is not reproduced: it only recycles a
    # fixed buffer, and the byte stream it emits is identical to writing
    # straight into a growable one.
    def _output_bits(self, nbits: int, bit_buff: int) -> None:
        if nbits > 8:
            self._output_bits(8, bit_buff)
            bit_buff >>= 8
            nbits -= 8

        out = self.out
        pos = self.out_bytes
        if len(out) <= pos + 1:
            out.extend(b"\x00" * (pos + 2 - len(out)))

        out_bits = self.out_bits
        out[pos] |= (bit_buff << out_bits) & 0xFF
        self.out_bits = out_bits + nbits

        if self.out_bits > 8:
            pos += 1
            self.out_bytes = pos
            bit_buff >>= (8 - out_bits)
            out[pos] = bit_buff & 0xFF
            self.out_bits &= 7
        else:
            self.out_bits &= 7
            if self.out_bits == 0:
                self.out_bytes = pos + 1

    # -- SortBuffer, implode.c:44-88 ---------------------------------------
    def _sort_buffer(self, begin: int, end: int) -> None:
        wb = self.work_buff
        h2i = self.phash_to_index
        offs = self.phash_offs

        # work_buff[0x2204..0x2205] aliases phash_offs[0] in TCmpStruct.
        wb[_WORK_BUFF_SIZE] = offs[0] & 0xFF
        wb[_WORK_BUFF_SIZE + 1] = (offs[0] >> 8) & 0xFF

        for i in range(_PHASH_TO_INDEX_SIZE):
            h2i[i] = 0
        for p in range(begin, end):
            h2i[wb[p] * 4 + wb[p + 1] * 5] += 1

        total = 0
        for i in range(_PHASH_TO_INDEX_SIZE):
            total = (total + h2i[i]) & 0xFFFF
            h2i[i] = total

        for p in range(end - 1, begin - 1, -1):
            h = wb[p] * 4 + wb[p + 1] * 5
            h2i[h] -= 1
            offs[h2i[h]] = p

    # -- FindRep, implode.c:152-406 ----------------------------------------
    def _find_rep(self, input_data: int) -> int:
        wb = self.work_buff
        h2i = self.phash_to_index
        offs = self.phash_offs

        hash_idx = wb[input_data] * 4 + wb[input_data + 1] * 5
        min_phash_offs = (input_data - self.dsize_bytes + 1) & 0xFFFF
        phash_offs_index = h2i[hash_idx]

        if offs[phash_offs_index] < min_phash_offs:
            while offs[phash_offs_index] < min_phash_offs:
                phash_offs_index += 1
            h2i[hash_idx] = phash_offs_index

        prev_repetition = offs[phash_offs_index]
        repetition_limit = input_data - 1

        if prev_repetition >= repetition_limit:
            return 0

        rep_length = 1
        equal_byte_count = 0
        input_data_ptr = input_data
        broke_out = False
        while True:
            if (wb[input_data_ptr] == wb[prev_repetition]
                    and wb[input_data_ptr + rep_length - 1] == wb[prev_repetition + rep_length - 1]):
                prev_repetition += 1
                input_data_ptr += 1
                equal_byte_count = 2

                while equal_byte_count < MAX_REP_LENGTH:
                    prev_repetition += 1
                    input_data_ptr += 1
                    if wb[prev_repetition] != wb[input_data_ptr]:
                        break
                    equal_byte_count += 1

                input_data_ptr = input_data
                if equal_byte_count >= rep_length:
                    self.distance = input_data - prev_repetition + equal_byte_count - 1
                    rep_length = equal_byte_count
                    if rep_length > 10:
                        broke_out = True
                        break

            phash_offs_index += 1
            prev_repetition = offs[phash_offs_index]

            if prev_repetition >= repetition_limit:
                return rep_length if rep_length >= 2 else 0

        assert broke_out

        if equal_byte_count == MAX_REP_LENGTH:
            self.distance -= 1
            return equal_byte_count

        if offs[phash_offs_index + 1] >= repetition_limit:
            return rep_length

        # Look for a longer repetition starting at a more recent offset.
        # implode.c:269-311 -- this builds a KMP-style failure table.
        o9bc = self.offs09BC
        o9bc[0] = _USHRT_MAX
        o9bc[1] = 0x0000
        di_val = 0

        offs_in_rep = 1
        while offs_in_rep < rep_length:
            if wb[input_data + offs_in_rep] != wb[input_data + di_val]:
                di_val = o9bc[di_val]
                if di_val != _USHRT_MAX:
                    continue
            offs_in_rep += 1
            di_val = (di_val + 1) & 0xFFFF
            o9bc[offs_in_rep] = di_val

        prev_repetition = offs[phash_offs_index]
        prev_rep_end = prev_repetition + rep_length
        rep_length2 = rep_length

        while True:
            rep_length2 = o9bc[rep_length2]
            if rep_length2 == _USHRT_MAX:
                rep_length2 = 0

            while True:
                phash_offs_index += 1
                prev_repetition = offs[phash_offs_index]
                if prev_repetition >= repetition_limit:
                    return rep_length
                if not (prev_repetition + rep_length2 < prev_rep_end):
                    break

            pre_last_byte = wb[input_data + rep_length - 2]
            if pre_last_byte == wb[prev_repetition + rep_length - 2]:
                if prev_repetition + rep_length2 != prev_rep_end:
                    prev_rep_end = prev_repetition
                    rep_length2 = 0
            else:
                while True:
                    phash_offs_index += 1
                    prev_repetition = offs[phash_offs_index]
                    if prev_repetition >= repetition_limit:
                        return rep_length
                    if (wb[prev_repetition + rep_length - 2] == pre_last_byte
                            and wb[prev_repetition] == wb[input_data]):
                        break
                prev_rep_end = prev_repetition + 2
                rep_length2 = 2

            while wb[prev_rep_end] == wb[input_data + rep_length2]:
                rep_length2 += 1
                if rep_length2 >= MAX_REP_LENGTH:
                    break
                prev_rep_end += 1

            if rep_length2 >= rep_length:
                self.distance = input_data - prev_repetition - 1
                rep_length = rep_length2
                if rep_length == MAX_REP_LENGTH:
                    return rep_length

                while offs_in_rep < rep_length2:
                    if wb[input_data + offs_in_rep] != wb[input_data + di_val]:
                        di_val = o9bc[di_val]
                        if di_val != _USHRT_MAX:
                            continue
                    offs_in_rep += 1
                    di_val = (di_val + 1) & 0xFFFF
                    o9bc[offs_in_rep] = di_val

    # -- WriteCmpData, implode.c:408-588 -----------------------------------
    def compress(self, src: bytes) -> bytes:
        wb = self.work_buff
        dsize = self.dsize_bytes
        src_pos = 0
        src_len = len(src)

        input_data = dsize + MAX_REP_LENGTH
        input_data_ended = 0
        phase = 0

        self.out[0] = self.ctype
        self.out[1] = self.dsize_bits
        self.out_bytes = 2
        self.out_bits = 0

        exit_early = False
        while input_data_ended == 0:
            bytes_to_load = 0x1000
            total_loaded = 0
            while bytes_to_load != 0:
                chunk = src[src_pos:src_pos + bytes_to_load]
                if not chunk:
                    if total_loaded == 0 and phase == 0:
                        exit_early = True
                        break
                    input_data_ended = 1
                    break
                dst = dsize + MAX_REP_LENGTH + total_loaded
                wb[dst:dst + len(chunk)] = chunk
                src_pos += len(chunk)
                bytes_to_load -= len(chunk)
                total_loaded += len(chunk)
            if exit_early:
                break

            input_data_end = dsize + total_loaded
            if input_data_ended:
                input_data_end += MAX_REP_LENGTH

            if phase == 0:
                self._sort_buffer(input_data, input_data_end + 1)
                phase += 1
                if dsize != 0x1000:
                    phase += 1
            elif phase == 1:
                self._sort_buffer(input_data - dsize + MAX_REP_LENGTH,
                                  input_data_end + 1)
                phase += 1
            else:
                self._sort_buffer(input_data - dsize, input_data_end + 1)

            nchbits = self.nchbits
            nchcodes = self.nchcodes
            dist_bits = self.dist_bits
            dist_codes = self.dist_codes
            dsize_bits = self.dsize_bits
            dsize_mask = self.dsize_mask
            output_bits = self._output_bits

            while input_data < input_data_end:
                rep_length = self._find_rep(input_data)
                emitted = False
                while rep_length != 0:
                    if rep_length == 2 and self.distance >= 0x100:
                        break

                    flush = False
                    if input_data_ended and input_data + rep_length > input_data_end:
                        rep_length = input_data_end - input_data
                        if rep_length < 2:
                            break
                        if rep_length == 2 and self.distance >= 0x100:
                            break
                        flush = True
                    elif rep_length >= 8 or input_data + 1 >= input_data_end:
                        flush = True

                    if not flush:
                        save_rep_length = rep_length
                        save_distance = self.distance
                        rep_length = self._find_rep(input_data + 1)
                        if rep_length > save_rep_length:
                            if rep_length > save_rep_length + 1 or save_distance > 0x80:
                                ch = wb[input_data]
                                output_bits(nchbits[ch], nchcodes[ch])
                                input_data += 1
                                continue
                        rep_length = save_rep_length
                        self.distance = save_distance

                    # __FlushRepetition
                    output_bits(nchbits[rep_length + 0xFE],
                                nchcodes[rep_length + 0xFE])
                    distance = self.distance
                    if rep_length == 2:
                        output_bits(dist_bits[distance >> 2],
                                    dist_codes[distance >> 2])
                        output_bits(2, distance & 3)
                    else:
                        output_bits(dist_bits[distance >> dsize_bits],
                                    dist_codes[distance >> dsize_bits])
                        output_bits(dsize_bits, dsize_mask & distance)
                    input_data += rep_length
                    emitted = True
                    break

                if not emitted:
                    ch = wb[input_data]
                    output_bits(nchbits[ch], nchcodes[ch])
                    input_data += 1

            if input_data_ended == 0:
                input_data -= 0x1000
                wb[0:dsize + MAX_REP_LENGTH] = wb[0x1000:0x1000 + dsize + MAX_REP_LENGTH]

        # __Exit
        self._output_bits(self.nchbits[0x305], self.nchcodes[0x305])
        if self.out_bits != 0:
            self.out_bytes += 1
        return bytes(self.out[:self.out_bytes])


def implode(data, ctype: int = CMP_BINARY, dict_size: int | None = None) -> bytes:
    """Compress `data` with the PKWARE DCL algorithm.

    `ctype` is CMP_BINARY (flat 9-bit literals, what map data uses) or
    CMP_ASCII.  `dict_size` is 1024/2048/4096; the default reproduces the
    size StarCraft would have chosen (see storm_dict_size).

    The result carries pklib's two-byte header -- compression type and
    dictionary bits -- and is what an MPQ sector holds when the file is
    flagged MPQ_FILE_IMPLODE.
    """
    data = bytes(data)
    if dict_size is None:
        dict_size = storm_dict_size(len(data))
    return _Cmp(ctype, dict_size).compress(data)
