"""Histogram diff (the algorithm behind `git diff --histogram`).

Myers-style and Ratcliff/Obershelp matchers anchor on whatever matches longest, which in
source and prose is often a run of frequent lines (`}`, blank lines, `return`). That
misaligns edits and creates spurious merge conflicts. Histogram diff instead:

1. strips the common prefix and suffix of the region,
2. finds the common line that occurs *least often* in the region (preferring the longest
   match around it on ties), and uses it as the anchor,
3. recurses on the parts before and after the anchor.

Lines occurring more than MAX_CHAIN times are never anchors. A region with no usable anchor
falls back to difflib's matcher. Implemented with an explicit stack, so very long files
can't hit Python's recursion limit.
"""
from __future__ import annotations

from difflib import SequenceMatcher

MAX_CHAIN = 64


def _anchor(a: list, b: list, alo: int, ahi: int, blo: int, bhi: int):
    counts: dict = {}
    where: dict = {}
    for i in range(alo, ahi):
        line = a[i]
        counts[line] = counts.get(line, 0) + 1
        where.setdefault(line, []).append(i)
    best = None  # (occurrences, -length, si, sj, length)
    for j in range(blo, bhi):
        c = counts.get(b[j])
        if not c or c > MAX_CHAIN or (best and c > best[0]):
            continue
        for i in where[b[j]]:
            si, sj = i, j
            while si > alo and sj > blo and a[si - 1] == b[sj - 1]:
                si -= 1
                sj -= 1
            ei, ej = i + 1, j + 1
            while ei < ahi and ej < bhi and a[ei] == b[ej]:
                ei += 1
                ej += 1
            occ = min(counts[a[k]] for k in range(si, ei))
            key = (occ, -(ei - si), si, sj, ei - si)
            if best is None or key < best:
                best = key
    return best


def histogram_matches(a: list, b: list) -> list[tuple[int, int, int]]:
    """Matching blocks [(i, j, n), ...] in increasing order, ending with (len(a), len(b), 0),
    like difflib.SequenceMatcher.get_matching_blocks()."""
    pairs: list[tuple[int, int]] = []
    stack = [(0, len(a), 0, len(b))]
    while stack:
        alo, ahi, blo, bhi = stack.pop()
        while alo < ahi and blo < bhi and a[alo] == b[blo]:          # common prefix
            pairs.append((alo, blo))
            alo += 1
            blo += 1
        while alo < ahi and blo < bhi and a[ahi - 1] == b[bhi - 1]:  # common suffix
            ahi -= 1
            bhi -= 1
            pairs.append((ahi, bhi))
        if alo >= ahi or blo >= bhi:
            continue
        best = _anchor(a, b, alo, ahi, blo, bhi)
        if best is None:
            sm = SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False)
            for i, j, n in sm.get_matching_blocks():
                pairs.extend((alo + i + k, blo + j + k) for k in range(n))
            continue
        _, _, si, sj, n = best
        pairs.extend((si + k, sj + k) for k in range(n))
        stack.append((alo, si, blo, sj))
        stack.append((si + n, ahi, sj + n, bhi))
    pairs.sort()
    blocks: list[tuple[int, int, int]] = []
    for i, j in pairs:
        if blocks and blocks[-1][0] + blocks[-1][2] == i and blocks[-1][1] + blocks[-1][2] == j:
            blocks[-1] = (blocks[-1][0], blocks[-1][1], blocks[-1][2] + 1)
        else:
            blocks.append((i, j, 1))
    blocks.append((len(a), len(b), 0))
    return blocks
