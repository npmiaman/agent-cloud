"""Line-level three-way merge (the diff3 algorithm over histogram diff alignments).

Given a common ancestor `base` and two descendants `ours` and `theirs`, find the regions
of `base` that both sides left untouched ("sync regions"). Between sync regions:

    only ours changed        -> take ours
    only theirs changed      -> take theirs
    both made the same edit  -> take it once
    both changed differently -> conflict

Adjacent edits (different lines, no unchanged line between them) fall in the same region
and conflict, as in diff3 and git: merging them silently is how subtle bugs get in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from .diffalgo import histogram_matches


@dataclass
class MergeResult:
    text: str
    clean: bool
    conflicts: list[dict] = field(default_factory=list)


def _sync_regions(base: list[str], a: list[str], b: list[str]) -> list[tuple[int, int, int, int, int, int]]:
    """Regions (base_start, base_end, a_start, a_end, b_start, b_end) matched in all three."""
    am = histogram_matches(base, a)
    bm = histogram_matches(base, b)
    out, ia, ib = [], 0, 0
    while ia < len(am) and ib < len(bm):
        abase, amatch, alen = am[ia]
        bbase, bmatch, blen = bm[ib]
        lo, hi = max(abase, bbase), min(abase + alen, bbase + blen)
        if lo < hi:
            asub, bsub = amatch + (lo - abase), bmatch + (lo - bbase)
            out.append((lo, hi, asub, asub + (hi - lo), bsub, bsub + (hi - lo)))
        if abase + alen < bbase + blen:
            ia += 1
        else:
            ib += 1
    out.append((len(base), len(base), len(a), len(a), len(b), len(b)))
    return out


def _with_newline(lines: list[str]) -> list[str]:
    if lines and not lines[-1].endswith("\n"):
        return lines[:-1] + [lines[-1] + "\n"]
    return lines


def merge3(base: str, ours: str, theirs: str, labels: tuple[str, str] = ("ours", "theirs")) -> MergeResult:
    if ours == theirs:
        return MergeResult(ours, True)
    if ours == base:
        return MergeResult(theirs, True)
    if theirs == base:
        return MergeResult(ours, True)
    B, A, T = base.splitlines(keepends=True), ours.splitlines(keepends=True), theirs.splitlines(keepends=True)
    out: list[str] = []
    conflicts: list[dict] = []
    iz = ia = ib = 0
    for zs, ze, as_, ae, bs, be in _sync_regions(B, A, T):
        if as_ > ia or bs > ib or zs > iz:
            zb, oa, tb = B[iz:zs], A[ia:as_], T[ib:bs]
            if oa == tb:
                out += oa
            elif oa == zb:
                out += tb
            elif tb == zb:
                out += oa
            else:
                conflicts.append({"line": iz + 1, "base": "".join(zb), "ours": "".join(oa), "theirs": "".join(tb)})
                out += [f"<<<<<<< {labels[0]}\n"] + _with_newline(oa) + ["=======\n"] + _with_newline(tb) + [f">>>>>>> {labels[1]}\n"]
        out += B[zs:ze]
        iz, ia, ib = ze, ae, be
    return MergeResult("".join(out), not conflicts, conflicts)
