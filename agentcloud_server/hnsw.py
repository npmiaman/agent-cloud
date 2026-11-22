"""HNSW approximate nearest-neighbour index (Malkov & Yashunin, 2016/2018).

A hierarchy of proximity graphs: each point gets a random top level (exponentially
distributed), searches descend greedily from the sparse top layers, and the dense bottom layer
is searched with a beam of width `ef`. Neighbours are chosen with the paper's diversity
heuristic (Algorithm 4), which keeps the graph navigable on clustered data.

- **Similarity:** dot product on L2-normalised vectors (cosine).
- **Deletes:** tombstones (deleted points still route searches but aren't returned). The
  graph is rebuilt once tombstones exceed half the points. Re-adding a key replaces its vector.
- **Filtered search** (permissions, path prefixes): the beam is widened. When the filter is too
  selective to fill k results from the beam, it falls back to an exact scan of the allowed
  points, so filtered results are never silently short.
- Uses numpy for batched dot products when available, pure Python otherwise.
"""
from __future__ import annotations

import heapq
import math
import random

try:
    import numpy as _np
except Exception:   # pragma: no cover - numpy is optional
    _np = None


class HNSW:
    def __init__(self, dim: int, M: int = 16, ef_construction: int = 128, ef_search: int = 64, seed: int = 0,
                 use_numpy: bool | None = None):
        self.dim, self.M, self.M0 = dim, M, 2 * M
        self.ef_construction, self.ef_search = ef_construction, ef_search
        self.mL = 1.0 / math.log(M)
        self.rng = random.Random(seed)
        self.np = _np if (use_numpy is not False and _np is not None) else None
        self.keys: list = []                 # internal id -> external key
        self.index: dict = {}                # external key -> live internal id
        self.links: list[list[list[int]]] = []
        self.deleted: set[int] = set()
        self.entry: int | None = None
        self.max_level = -1
        self._vecs = self.np.zeros((64, dim), dtype=self.np.float32) if self.np else []

    # ---------------------------------------------------------------- vectors

    def _norm(self, v):
        n = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
        return [float(x) / n for x in v]

    def _store(self, i: int, v: list[float]) -> None:
        if self.np:
            if i >= len(self._vecs):
                grown = self.np.zeros((len(self._vecs) * 2, self.dim), dtype=self.np.float32)
                grown[:len(self._vecs)] = self._vecs
                self._vecs = grown
            self._vecs[i] = v
        else:
            self._vecs.append(v)

    def _vec(self, i: int):
        return self._vecs[i]

    def _sims(self, q, ids: list[int]) -> list[float]:
        if not ids:
            return []
        if self.np:
            return (self._vecs[ids] @ q).tolist()
        return [sum(a * b for a, b in zip(self._vecs[i], q)) for i in ids]

    def _q(self, v):
        v = self._norm(v)
        return self.np.asarray(v, dtype=self.np.float32) if self.np else v

    # ---------------------------------------------------------------- graph

    def _search_layer(self, q, entry_points: list[int], ef: int, level: int) -> list[tuple[float, int]]:
        sims = self._sims(q, entry_points)
        visited = set(entry_points)
        candidates = [(-s, i) for s, i in zip(sims, entry_points)]
        heapq.heapify(candidates)
        best = [(s, i) for s, i in zip(sims, entry_points)]
        heapq.heapify(best)
        while len(best) > ef:
            heapq.heappop(best)
        while candidates:
            neg, c = heapq.heappop(candidates)
            if len(best) >= ef and -neg < best[0][0]:
                break
            nbrs = [n for n in self.links[c][level] if n not in visited] if level < len(self.links[c]) else []
            visited.update(nbrs)
            for s, n in zip(self._sims(q, nbrs), nbrs):
                if len(best) < ef or s > best[0][0]:
                    heapq.heappush(candidates, (-s, n))
                    heapq.heappush(best, (s, n))
                    if len(best) > ef:
                        heapq.heappop(best)
        return sorted(best, reverse=True)

    def _select(self, base_vec, cands: list[tuple[float, int]], m: int) -> list[int]:
        """Diversity heuristic: keep a candidate only if it's closer to the base point than to
        any neighbour already kept; then fill up with the closest of the rest."""
        kept: list[int] = []
        pruned: list[int] = []
        for s, c in sorted(cands, reverse=True):
            if len(kept) >= m:
                break
            if kept:
                closer_to_kept = max(self._sims(self._vec(c), kept))
                if closer_to_kept > s:
                    pruned.append(c)
                    continue
            kept.append(c)
        for c in pruned:
            if len(kept) >= m:
                break
            kept.append(c)
        return kept

    def _insert(self, i: int, q) -> None:
        level = int(-math.log(1.0 - self.rng.random()) * self.mL)
        self.links.append([[] for _ in range(level + 1)])
        if self.entry is None:
            self.entry, self.max_level = i, level
            return
        ep = [self.entry]
        for lvl in range(self.max_level, level, -1):
            ep = [self._search_layer(q, ep, 1, lvl)[0][1]]
        for lvl in range(min(level, self.max_level), -1, -1):
            found = self._search_layer(q, ep, self.ef_construction, lvl)
            mmax = self.M0 if lvl == 0 else self.M
            nbrs = self._select(q, found, self.M)
            self.links[i][lvl] = nbrs
            for n in nbrs:
                ln = self.links[n][lvl]
                ln.append(i)
                if len(ln) > mmax:
                    nv = self._vec(n)
                    self.links[n][lvl] = self._select(nv, list(zip(self._sims(nv, ln), ln)), mmax)
            ep = [x for _, x in found]
        if level > self.max_level:
            self.entry, self.max_level = i, level

    # ---------------------------------------------------------------- public API

    def __len__(self) -> int:
        return len(self.index)

    def add(self, key, vec) -> None:
        if key in self.index:
            self.remove(key, _rebuild=False)
        q = self._q(vec)
        i = len(self.keys)
        self.keys.append(key)
        self._store(i, q)
        self._insert(i, q)
        self.index[key] = i

    def remove(self, key, _rebuild: bool = True) -> None:
        i = self.index.pop(key, None)
        if i is None:
            return
        self.deleted.add(i)
        if _rebuild and len(self.keys) > 64 and len(self.deleted) > len(self.keys) / 2:
            self.rebuild()

    def rebuild(self) -> None:
        live = [(k, self._vec(i)) for k, i in self.index.items()]
        self.__init__(self.dim, self.M, self.ef_construction, self.ef_search, self.rng.randrange(1 << 30),
                      use_numpy=self.np is not None)
        for k, v in live:
            self.add(k, v)

    def search(self, vec, k: int, ef: int | None = None, allow=None) -> list[tuple]:
        """[(key, similarity), ...] best first. `allow(key)` filters results."""
        if not self.index or k <= 0:
            return []
        q = self._q(vec)
        ef = max(ef or self.ef_search, k)
        if allow is not None:
            ef = max(ef, k * 8)
        ep = [self.entry]
        for lvl in range(self.max_level, 0, -1):
            ep = [self._search_layer(q, ep, 1, lvl)[0][1]]
        found = self._search_layer(q, ep, ef, 0)
        out = [(self.keys[i], s) for s, i in found if i not in self.deleted and (allow is None or allow(self.keys[i]))][:k]
        if len(out) < min(k, len(self.index)):
            return self.exact(vec, k, allow)            # selective filter or many tombstones: be exact
        return out

    def exact(self, vec, k: int, allow=None) -> list[tuple]:
        q = self._q(vec)
        live = [(key, i) for key, i in self.index.items() if allow is None or allow(key)]
        sims = self._sims(q, [i for _, i in live])
        return sorted(((key, s) for (key, _), s in zip(live, sims)), key=lambda x: -x[1])[:k]
