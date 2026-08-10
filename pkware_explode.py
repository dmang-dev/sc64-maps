"""PKWARE DCL explode -- the only compression genuine StarCraft maps use.

Ported from the vendored reference/StormLib/src/pklib/explode.c. The
Huffman tables are parsed out of that C source at import time rather than
transcribed, so they cannot drift from the reference implementation.

Decompression only; there is no compressor here.

SPDX-License-Identifier: GPL-3.0-or-later
"""
import os
import re

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "reference", "StormLib", "src", "pklib", "explode.c")

def _tables():
    txt = open(SRC, 'r', encoding='latin1').read()
    out = {}
    for name in ("DistBits", "DistCode", "ExLenBits", "LenBase", "LenBits",
                 "LenCode", "ChBitsAsc", "ChCodeAsc"):
        m = re.search(r'\b' + name + r'\s*\[[^\]]*\]\s*=\s*\{(.*?)\};', txt, re.S)
        out[name] = [int(v, 16) for v in re.findall(r'0x([0-9A-Fa-f]+)', m.group(1))]
    return out
T = _tables()
DistBits, DistCode = T['DistBits'], T['DistCode']
ExLenBits, LenBase, LenBits, LenCode = T['ExLenBits'], T['LenBase'], T['LenBits'], T['LenCode']
ChBitsAsc0, ChCodeAsc = T['ChBitsAsc'], T['ChCodeAsc']
assert len(DistBits) == 64 and len(DistCode) == 64 and len(ChCodeAsc) == 256
assert len(LenBase) == 16 and len(LenBits) == 16 and len(ChBitsAsc0) == 256

def _gen(start_idx, length_bits, n):
    pos = [0]*0x100
    for i in range(n):
        step = 1 << length_bits[i]
        idx = start_idx[i]
        while idx < 0x100:
            pos[idx] = i
            idx += step
    return pos
LengthCodes = _gen(LenCode, LenBits, 16)
DistPosCodes = _gen(DistCode, DistBits, 64)

def _gen_asc():
    bits = list(ChBitsAsc0)
    o2C34 = [0]*0x100; o2D34 = [0]*0x100; o2E34 = [0]*0x100; o2EB4 = [0]*0x100
    for count in range(0xFF, -1, -1):
        b = bits[count]; code = ChCodeAsc[count]
        if b <= 8:
            add = 1 << b; acc = code
            while acc < 0x100:
                o2C34[acc] = count; acc += add
        else:
            acc = code & 0xFF
            if acc != 0:
                o2C34[acc] = 0xFF
                if code & 0x3F:
                    b -= 4; bits[count] = b
                    add = 1 << b; acc = code >> 4
                    while acc < 0x100:
                        o2D34[acc] = count; acc += add
                else:
                    b -= 6; bits[count] = b
                    add = 1 << b; acc = code >> 6
                    while acc < 0x80:
                        o2E34[acc] = count; acc += add
            else:
                b -= 8; bits[count] = b
                add = 1 << b; acc = code >> 8
                while acc < 0x100:
                    o2EB4[acc] = count; acc += add
    return bits, o2C34, o2D34, o2E34, o2EB4
ChBitsAsc, offs2C34, offs2D34, offs2E34, offs2EB4 = _gen_asc()

class _S:
    """Mirrors TDcmpStruct's bit_buff / extra_bits / in_pos exactly."""
    __slots__ = ('d', 'p', 'buf', 'extra')
    def __init__(self, data):
        self.d = data
        self.buf = data[2]
        self.extra = 0
        self.p = 3
    def waste(self, n):
        if n <= self.extra:
            self.extra -= n; self.buf >>= n; return True
        self.buf >>= self.extra
        if self.p >= len(self.d):
            return False
        self.buf |= self.d[self.p] << 8; self.p += 1
        self.buf >>= (n - self.extra)
        self.extra = self.extra - n + 8
        return True

class ExplodeError(Exception):
    pass

def explode(data):
    if len(data) <= 4:
        raise ExplodeError("too short")
    ctype, dsize_bits = data[0], data[1]
    if ctype not in (0, 1):
        raise ExplodeError(f"invalid mode {ctype}")
    if not 4 <= dsize_bits <= 6:
        raise ExplodeError(f"invalid dict size {dsize_bits}")
    dsize_mask = 0xFFFF >> (0x10 - dsize_bits)
    s = _S(data)
    out = bytearray()
    while True:
        # ---- DecodeLit ----
        if s.buf & 1:
            if not s.waste(1): return bytes(out)
            lc = LengthCodes[s.buf & 0xFF]
            if not s.waste(LenBits[lc]): return bytes(out)
            xb = ExLenBits[lc]
            if xb:
                extra = s.buf & ((1 << xb) - 1)
                if not s.waste(xb) and (lc + extra) != 0x10E:
                    return bytes(out)
                lc = LenBase[lc] + extra
            lit = lc + 0x100
            if lit >= 0x305:
                return bytes(out)
            rep = lit - 0xFE
            # ---- DecodeDist ----
            dpc = DistPosCodes[s.buf & 0xFF]
            if not s.waste(DistBits[dpc]): return bytes(out)
            if rep == 2:
                dist = (dpc << 2) | (s.buf & 3)
                if not s.waste(2): return bytes(out)
            else:
                dist = (dpc << dsize_bits) | (s.buf & dsize_mask)
                if not s.waste(dsize_bits): return bytes(out)
            dist += 1
            if dist > len(out):
                raise ExplodeError(f"distance {dist} past start (out={len(out)})")
            src = len(out) - dist
            for k in range(rep):
                out.append(out[src + k])
        else:
            if not s.waste(1): return bytes(out)
            if ctype == 0:
                v = s.buf & 0xFF
                if not s.waste(8): return bytes(out)
                out.append(v)
            else:
                if s.buf & 0xFF:
                    v = offs2C34[s.buf & 0xFF]
                    if v == 0xFF:
                        if s.buf & 0x3F:
                            if not s.waste(4): return bytes(out)
                            v = offs2D34[s.buf & 0xFF]
                        else:
                            if not s.waste(6): return bytes(out)
                            v = offs2E34[s.buf & 0x7F]
                else:
                    if not s.waste(8): return bytes(out)
                    v = offs2EB4[s.buf & 0xFF]
                if not s.waste(ChBitsAsc[v]): return bytes(out)
                out.append(v)
