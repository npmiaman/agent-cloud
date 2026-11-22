"""Prolly trees over immutable, content-addressed objects (SPEC §4).

A folder is a prolly tree over its entries sorted by name:
    file entry   ["f", name, blob_id, size, prov_id]
    folder entry ["d", name, tree_id]
    leaf         {"e": [entry, ...]}
    internal     {"i": level, "c": [[first_name, child_id], ...]}

Nodes end where uint64(BLAKE2b-64("L:" + key)) % T == 0 once they hold T/2 items, or at
MAX_NODE items, so the same entries always give the same root id (history independence),
and a change rewrites only the nodes on its path.

The functions take `get(id) -> obj` and `put(obj) -> id` callables, so they work over any
object store and any encryption.
"""
from __future__ import annotations

import bisect
import hashlib
import json

LEAF_TARGET = 64
INTERNAL_TARGET = 64
MAX_NODE = 1024


def _boundary(name: str, level: int, target: int) -> bool:
    d = hashlib.blake2b(f"{level}:{name}".encode(), digest_size=8).digest()
    return int.from_bytes(d, "big") % target == 0


def _min_node(target: int) -> int:
    return max(2, target // 2)


def _chunk(items: list, key, level: int, target: int) -> list[list]:
    out, cur, lo = [], [], _min_node(target)
    for it in items:
        cur.append(it)
        if len(cur) >= MAX_NODE or (len(cur) >= lo and _boundary(key(it), level, target)):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _closed(chunk: list, key, level: int, target: int) -> bool:
    return not chunk or len(chunk) >= MAX_NODE or (len(chunk) >= _min_node(target) and _boundary(key(chunk[-1]), level, target))


class Trees:
    def __init__(self, get, put, empty_id: str):
        self.get, self.put, self.EMPTY = get, put, empty_id

    # ---------------------------------------------------------------- building

    def _leaf(self, entries: list) -> str:
        return self.put({"e": sorted(entries, key=lambda e: e[1])})

    def _internal(self, refs: list, level: int) -> str:
        while len(refs) > 1:
            groups = _chunk(refs, lambda r: r[0], level, INTERNAL_TARGET)
            if len(groups) == len(refs):
                groups = [refs[i:i + MAX_NODE] for i in range(0, len(refs), MAX_NODE)]
            refs = [[g[0][0], self.put({"i": level, "c": g})] for g in groups]
            level += 1
        return refs[0][1]

    def store_folder(self, entries: list) -> str:
        entries = sorted(entries, key=lambda e: e[1])
        if not entries:
            return self.EMPTY
        leaves = _chunk(entries, lambda e: e[1], 0, LEAF_TARGET)
        if len(leaves) == 1:
            return self._leaf(leaves[0])
        return self._internal([[lf[0][1], self._leaf(lf)] for lf in leaves], 1)

    # ---------------------------------------------------------------- reading

    def node(self, h: str):
        return {"e": []} if h == self.EMPTY else self.get(h)

    def entries(self, h: str) -> list:
        n = self.node(h)
        if "e" in n:
            return n["e"]
        out = []
        for _, child in n["c"]:
            out.extend(self.entries(child))
        return out

    def _leaf_refs(self, h: str) -> list:
        n = self.node(h)
        if "e" in n:
            return [[n["e"][0][1] if n["e"] else "", h]]
        out = []
        for first, child in n["c"]:
            cn = self.node(child)
            out.extend([[first, child]] if "e" in cn else self._leaf_refs(child))
        return out

    def lookup(self, h: str, names: set[str]) -> dict[str, list]:
        out: dict[str, list] = {}

        def rec(hh, want):
            n = self.node(hh)
            if "e" in n:
                for e in n["e"]:
                    if e[1] in want:
                        out[e[1]] = e
                return
            firsts = [c[0] for c in n["c"]]
            by: dict[int, set] = {}
            for name in want:
                by.setdefault(max(0, bisect.bisect_right(firsts, name) - 1), set()).add(name)
            for i, ns in by.items():
                rec(n["c"][i][1], ns)
        if h != self.EMPTY and names:
            rec(h, set(names))
        return out

    def walk(self, h: str, prefix: str = "") -> dict[str, list]:
        out = {}
        for e in self.entries(h):
            if e[0] == "f":
                out[prefix + e[1]] = e
            else:
                out.update(self.walk(e[2], prefix + e[1] + "/"))
        return out

    def get_path(self, root: str, path: str) -> list | None:
        h = root
        parts = path.split("/")
        for i, part in enumerate(parts):
            e = self.lookup(h, {part}).get(part)
            if e is None:
                return None
            if i == len(parts) - 1:
                return e if e[0] == "f" else None
            if e[0] != "d":
                return None
            h = e[2]
        return None

    # ---------------------------------------------------------------- updating

    def folder_update(self, h: str, edits: dict[str, list | None]) -> str:
        n = self.node(h)
        if "e" in n:
            cur = {e[1]: e for e in n["e"]}
            for name, e in edits.items():
                if e is None:
                    cur.pop(name, None)
                else:
                    cur[name] = e
            return self.store_folder(list(cur.values()))
        refs = self._leaf_refs(h)
        firsts = [r[0] for r in refs]
        per_leaf: dict[int, dict] = {}
        for name, e in edits.items():
            per_leaf.setdefault(max(0, bisect.bisect_right(firsts, name) - 1), {})[name] = e

        def leaf_entries(i):
            cur = {e[1]: e for e in self.node(refs[i][1])["e"]}
            for name, e in per_leaf.get(i, {}).items():
                if e is None:
                    cur.pop(name, None)
                else:
                    cur[name] = e
            return sorted(cur.values(), key=lambda e: e[1])
        key = lambda e: e[1]   # noqa: E731
        new_refs, i = [], 0
        while i < len(refs):
            if i not in per_leaf:
                new_refs.append(refs[i])
                i += 1
                continue
            window, j = leaf_entries(i), i
            while True:
                chunks = _chunk(window, key, 0, LEAF_TARGET)
                if not chunks or _closed(chunks[-1], key, 0, LEAF_TARGET) or j + 1 >= len(refs):
                    break
                j += 1
                window = window + leaf_entries(j)
            new_refs.extend([c[0][1], self._leaf(c)] for c in chunks)
            i = j + 1
        if not new_refs:
            return self.EMPTY
        return new_refs[0][1] if len(new_refs) == 1 else self._internal(new_refs, 1)

    def apply(self, root: str, edits: dict[str, list | None]) -> str:
        """edits: {path: file entry (its name is replaced by the basename) or None to delete}."""
        def rec(h, sub):
            own: dict[str, list | None] = {}
            child: dict[str, dict] = {}
            for rel, e in sub.items():
                head, sep, rest = rel.partition("/")
                if sep:
                    child.setdefault(head, {})[rest] = e
                else:
                    own[head] = None if e is None else ["f", head] + list(e[2:])
            cur = self.lookup(h, set(child) | {n for n, e in own.items() if e is None})
            for name, e in list(own.items()):
                if e is None and not (cur.get(name) and cur[name][0] == "f"):
                    del own[name]
            for name, ce in child.items():
                c = cur.get(name)
                sub_root = rec(c[2] if c and c[0] == "d" else self.EMPTY, ce)
                own[name] = None if sub_root == self.EMPTY else ["d", name, sub_root]
            return self.folder_update(h, own) if own else h
        return rec(root, edits)

    # ---------------------------------------------------------------- diffing

    def _delta(self, ha: str | None, hb: str | None) -> tuple[list, list]:
        cache: dict = {}

        def node(h):
            if h not in cache:
                cache[h] = self.node(h)
            return cache[h]
        fa = [ha] if ha and ha != self.EMPTY else []
        fb = [hb] if hb and hb != self.EMPTY else []
        while True:
            common = set(fa) & set(fb)
            fa = [x for x in fa if x not in common]
            fb = [x for x in fb if x not in common]
            grew = False
            for side in (fa, fb):
                nxt = []
                for x in side:
                    n = node(x)
                    if "c" in n:
                        nxt.extend(c[1] for c in n["c"])
                        grew = True
                    else:
                        nxt.append(x)
                side[:] = nxt
            if not grew:
                break
        ea = [e for x in fa for e in node(x)["e"]]
        eb = [e for x in fb for e in node(x)["e"]]
        sa = {json.dumps(e) for e in ea}
        sb = {json.dumps(e) for e in eb}
        return [e for e in ea if json.dumps(e) not in sb], [e for e in eb if json.dumps(e) not in sa]

    def diff(self, a: str, b: str) -> tuple[dict[str, list], dict[str, list]]:
        """File entries that differ between two trees: (left, right). Shared nodes aren't loaded."""
        left: dict[str, list] = {}
        right: dict[str, list] = {}

        def rec(ha, hb, prefix):
            if ha == hb:
                return
            da, db = self._delta(ha, hb)
            ea, eb = {e[1]: e for e in da}, {e[1]: e for e in db}
            for name in set(ea) | set(eb):
                x, y = ea.get(name), eb.get(name)
                if x and y and x[0] == "d" and y[0] == "d":
                    rec(x[2], y[2], prefix + name + "/")
                    continue
                for e, target in ((x, left), (y, right)):
                    if not e:
                        continue
                    if e[0] == "f":
                        target[prefix + name] = e
                    else:
                        target.update(self.walk(e[2], prefix + name + "/"))
        rec(a, b, "")
        return left, right
