"""FastCDC content-defined chunking (Xia et al., USENIX ATC 2016).

Splits data at boundaries chosen by the content itself, using a Gear rolling hash. Inserting
or deleting bytes only changes the chunks around the edit; boundaries further on
resynchronise. That's what makes chunk-level dedup work for appends and edits in the middle
of big files.

- **Normalised chunking:** a stricter mask before the target average, a looser one after, so
  chunk sizes concentrate around AVG.
- **Cut-point skipping:** no boundary check in the first MIN bytes of a chunk.
- **Fixed Gear table,** derived from SHA-256 of the index, so boundaries are identical
  across processes, machines and versions. Chunk hashes must stay stable forever.
"""
from __future__ import annotations

import hashlib
from typing import BinaryIO, Iterator

MIN = 2 * 1024
AVG = 8 * 1024
MAX = 64 * 1024
_M64 = (1 << 64) - 1
# 15 one-bits (harder, used before AVG) and 11 one-bits (easier, after AVG), spread over the
# high bits of the hash, as in the paper's normalisation level 2.
MASK_S = 0x0003590703530000
MASK_L = 0x0000D90003530000
GEAR = [int.from_bytes(hashlib.sha256(b"agentcloud-gear-%d" % i).digest()[:8], "big") for i in range(256)]


def cut_point(buf, n: int) -> int:
    """Length of the first chunk in buf[:n]."""
    if n <= MIN:
        return n
    if n > MAX:
        n = MAX
    normal = AVG if n > AVG else n
    fp, i, gear, ms, ml = 0, MIN, GEAR, MASK_S, MASK_L
    while i < normal:
        fp = ((fp << 1) + gear[buf[i]]) & _M64
        if not fp & ms:
            return i + 1
        i += 1
    while i < n:
        fp = ((fp << 1) + gear[buf[i]]) & _M64
        if not fp & ml:
            return i + 1
        i += 1
    return n


def chunk_bytes(data: bytes) -> list[bytes]:
    out, pos, n = [], 0, len(data)
    mv = memoryview(data)
    while pos < n:
        c = cut_point(mv[pos:], n - pos)
        out.append(bytes(mv[pos:pos + c]))
        pos += c
    return out


def chunk_stream(f: BinaryIO, read_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Chunks of a file, with memory bounded by read_size + MAX."""
    buf = bytearray()
    eof = False
    while True:
        while not eof and len(buf) < MAX:
            block = f.read(read_size)
            if not block:
                eof = True
            buf += block
        if not buf:
            return
        c = cut_point(buf, len(buf))
        if c == len(buf) and not eof and len(buf) < MAX:
            continue
        yield bytes(buf[:c])
        del buf[:c]
