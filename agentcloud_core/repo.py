"""The reference implementation of the Agent Cloud repository format (SPEC.md).

    repo = Repo.init(store, "projects/alpha", key=kek)        # or Repo.open(...)
    tx = repo.branch("agents/researcher-17").begin(writer="agent:researcher-17")
    p = tx.write("reports/q3.md", data, inputs=[upstream_prov], sources=["https://..."])
    tx.commit("draft Q3 report")
    repo.merge_many(["agents/a", "agents/b"], "teams/research")    # hierarchical fan-in
    repo.retract(p, reason="fabricated source")                    # flags everything downstream

No server: every operation is object-store GETs, create-only PUTs, and compare-and-swap on
branch heads.
"""
from __future__ import annotations

import base64
import bisect
import hashlib
import hmac
import json
import os
import random
import secrets
import threading
import time
from collections import OrderedDict

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .cdc import AVG, MAX, MIN, chunk_bytes
from .merge3 import merge3
from .prolly import INTERNAL_TARGET, LEAF_TARGET, MAX_NODE, Trees


class Conflict(Exception):
    """A single-writer branch moved under a commit, or a CAS kept failing."""


class MergeConflict(Exception):
    def __init__(self, paths: list[str], details: list[dict]):
        super().__init__(f"merge conflict in {len(paths)} path(s): {', '.join(paths[:5])}")
        self.paths, self.details = paths, details


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class LocalKEK:
    """A key-encryption key held by the caller (32 bytes). Swap in a KMS adapter with the same
    wrap/unwrap methods to keep the key in AWS KMS, GCP KMS or Vault."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        self._a = AESGCM(key)
        self.name = "local"

    def wrap(self, data: bytes) -> bytes:
        n = os.urandom(12)
        return n + self._a.encrypt(n, data, b"agentcloud-kek")

    def unwrap(self, blob: bytes) -> bytes:
        return self._a.decrypt(blob[:12], blob[12:], b"agentcloud-kek")


class Codec:
    """Object ids and at-rest encryption (SPEC §3)."""

    def __init__(self, dek: bytes | None, id_key: bytes | None):
        self.aead = AESGCM(dek) if dek else None
        self.id_key = id_key

    def id(self, typ: str, plain: bytes) -> str:
        msg = typ.encode() + b"\x00" + plain
        if self.id_key:
            return hmac.new(self.id_key, msg, hashlib.sha256).hexdigest()
        return hashlib.sha256(msg).hexdigest()

    def seal(self, oid: str, plain: bytes) -> bytes:
        if not self.aead:
            return plain
        n = os.urandom(12)
        return n + self.aead.encrypt(n, plain, oid.encode())

    def open(self, oid: str, typ: str, stored: bytes) -> bytes:
        plain = self.aead.decrypt(stored[:12], stored[12:], oid.encode()) if self.aead else stored
        if self.id(typ, plain) != oid:
            raise ValueError(f"object {oid[:12]} failed verification")
        return plain


class Repo:
    def __init__(self, store, prefix: str, descriptor: dict, codec: Codec, key=None):
        self.store, self.p, self.descriptor, self.codec, self.key = store, prefix.rstrip("/") + "/", descriptor, codec, key
        self.trees = Trees(lambda h: self.get("tree", h), lambda o: self.put("tree", o), self.codec.id("tree", _canon({"e": []})))
        self._cache: OrderedDict = OrderedDict()
        self._known: set[str] = set()
        self._lock = threading.Lock()
        self._retraction_cache: tuple[int, set, set] | None = None

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def init(cls, store, prefix: str, key: bytes | LocalKEK | None = None) -> "Repo":
        kek = LocalKEK(key) if isinstance(key, (bytes, bytearray)) else key
        desc = {"format": "agentcloud-repo", "version": 1, "created_at": time.time(),
                "chunking": {"algorithm": "fastcdc-gear-v1", "min": MIN, "avg": AVG, "max": MAX},
                "tree": {"algorithm": "prolly-blake2b-v1", "leaf_target": LEAF_TARGET, "internal_target": INTERNAL_TARGET,
                         "max_node": MAX_NODE}}
        dek = id_key = None
        if kek is not None:
            dek, id_key = os.urandom(32), os.urandom(32)
            desc["encryption"] = {"scheme": "aes-256-gcm-envelope-v1", "kek": kek.name,
                                  "wrapped_dek": base64.b64encode(kek.wrap(dek)).decode(),
                                  "id_key_wrapped": base64.b64encode(kek.wrap(id_key)).decode()}
        else:
            desc["encryption"] = {"scheme": "none"}
        if not store.put_if_absent(prefix.rstrip("/") + "/acf.json", _canon(desc)):
            raise FileExistsError(f"a repository already exists at {prefix}")
        return cls(store, prefix, desc, Codec(dek, id_key), key)

    @classmethod
    def open(cls, store, prefix: str, key: bytes | LocalKEK | None = None) -> "Repo":
        got = store.get(prefix.rstrip("/") + "/acf.json")
        if got is None:
            raise FileNotFoundError(f"no repository at {prefix}")
        desc = json.loads(got[0])
        if desc.get("format") != "agentcloud-repo" or desc.get("version") != 1:
            raise ValueError("unsupported repository format")
        enc = desc["encryption"]
        if enc["scheme"] == "none":
            return cls(store, prefix, desc, Codec(None, None), key)
        kek = LocalKEK(key) if isinstance(key, (bytes, bytearray)) else key
        if kek is None:
            raise PermissionError("this repository is encrypted; pass its key")
        try:
            dek = kek.unwrap(base64.b64decode(enc["wrapped_dek"]))
            id_key = kek.unwrap(base64.b64decode(enc["id_key_wrapped"]))
        except Exception as e:
            raise PermissionError("wrong key for this repository") from e
        return cls(store, prefix, desc, Codec(dek, id_key), key)

    # ---------------------------------------------------------------- objects

    def _okey(self, oid: str) -> str:
        return f"{self.p}objects/{oid[:2]}/{oid}"

    def put(self, typ: str, obj) -> str:
        plain = obj if typ == "chunk" else _canon(obj)
        oid = self.codec.id(typ, plain)
        if oid not in self._known:
            self.store.put_if_absent(self._okey(oid), self.codec.seal(oid, plain))
            with self._lock:
                self._known.add(oid)
                if len(self._known) > 500_000:
                    self._known.clear()
        return oid

    def get(self, typ: str, oid: str):
        with self._lock:
            if oid in self._cache:
                self._cache.move_to_end(oid)
                return self._cache[oid]
        got = self.store.get(self._okey(oid))
        if got is None:
            raise KeyError(f"missing {typ} object {oid[:12]}")
        plain = self.codec.open(oid, typ, got[0])
        val = plain if typ == "chunk" else json.loads(plain)
        with self._lock:
            self._cache[oid] = val
            if len(self._cache) > 50_000:
                self._cache.popitem(last=False)
        return val

    def _write_blob(self, data: bytes) -> tuple[str, int]:
        chunks = [[self.put("chunk", c), len(c)] for c in (chunk_bytes(data) if data else [b""])]
        return self.put("blob", {"size": len(data), "chunks": chunks}), len(data)

    def read_blob(self, blob_id: str) -> bytes:
        return b"".join(self.get("chunk", c) for c, _ in self.get("blob", blob_id)["chunks"])

    # ---------------------------------------------------------------- refs

    def _ref_key(self, branch: str) -> str:
        if not branch or ".." in branch.split("/") or branch.startswith("/"):
            raise ValueError(f"bad branch name {branch!r}")
        return f"{self.p}refs/branches/{branch}"

    def _ref(self, branch: str) -> tuple[dict | None, str | None]:
        got = self.store.get(self._ref_key(branch))
        return (json.loads(got[0]), got[1]) if got else (None, None)

    def _cas_ref(self, branch: str, commit_id: str, seq: int, etag: str | None) -> bool:
        body = _canon({"commit": commit_id, "seq": seq})
        if etag is None:
            return self.store.put_if_absent(self._ref_key(branch), body)
        return self.store.put_if_match(self._ref_key(branch), body, etag) is not None

    def branches(self) -> list[str]:
        base = f"{self.p}refs/branches/"
        return [k[len(base):] for k in self.store.list(base)]

    def branch(self, name: str) -> "Branch":
        self._ref_key(name)
        return Branch(self, name)

    def fork(self, source: str, name: str) -> str:
        """Start branch `name` at the head of `source`."""
        ref, _ = self._ref(source)
        if not ref:
            raise KeyError(f"branch {source} has no commits")
        if not self._cas_ref(name, ref["commit"], ref["seq"], None):
            raise FileExistsError(f"branch {name} exists")
        return ref["commit"]

    def commit(self, cid: str) -> dict:
        return self.get("commit", cid)

    def tree_of(self, cid: str | None) -> str:
        return self.commit(cid)["tree"] if cid else self.trees.EMPTY

    def log(self, branch: str, limit: int = 100) -> list[dict]:
        ref, _ = self._ref(branch)
        out, cid = [], ref["commit"] if ref else None
        while cid and len(out) < limit:
            c = self.commit(cid)
            out.append({"id": cid, **c})
            cid = c["parents"][0] if c["parents"] else None
        return out

    def diff(self, a: str | None, b: str | None) -> dict:
        left, right = self.trees.diff(self.tree_of(a), self.tree_of(b))
        return {"added": sorted(set(right) - set(left)), "removed": sorted(set(left) - set(right)),
                "changed": sorted(p for p in set(left) & set(right) if left[p][2] != right[p][2])}

    def prov(self, prov_id: str) -> dict:
        return self.get("prov", prov_id)

    # ---------------------------------------------------------------- merging

    def _ancestors(self, cid: str | None) -> dict[str, int]:
        out, frontier, d = {}, [cid] if cid else [], 0
        while frontier:
            nxt = []
            for c in frontier:
                if c in out:
                    continue
                out[c] = d
                nxt.extend(self.commit(c)["parents"])
            frontier, d = nxt, d + 1
        return out

    def merge_base(self, a: str | None, b: str | None) -> str | None:
        if not a or not b:
            return None
        anc = self._ancestors(a)
        seen, frontier = set(), [b]
        while frontier:
            hits = [c for c in frontier if c in anc]
            if hits:
                return min(hits, key=lambda c: anc[c])
            nxt = []
            for c in frontier:
                if c not in seen:
                    seen.add(c)
                    nxt.extend(self.commit(c)["parents"])
            frontier = nxt
        return None

    def merge_trees(self, base: str, ours: str, theirs: str, resolve: dict | None = None) -> tuple[str, list[str]]:
        """Three-way merge of trees; returns (tree id, auto-merged paths). Cost tracks the changes."""
        resolve = resolve or {}
        _, changed_theirs = self.trees.diff(base, theirs)
        removed_theirs, _ = self.trees.diff(base, theirs)
        paths = set(changed_theirs) | set(removed_theirs)
        edits, auto, conflicts = {}, [], []
        for path in sorted(paths):
            b = self.trees.get_path(base, path)
            o = self.trees.get_path(ours, path)
            t = changed_theirs.get(path)
            if path not in changed_theirs:
                t = None                                     # theirs deleted it
            bb, ob, tb = (b[2] if b else None), (o[2] if o else None), (t[2] if t else None)
            if tb == bb or ob == tb:
                continue
            if ob == bb:
                edits[path] = t
                continue
            side = resolve.get(path)
            if side == "theirs":
                edits[path] = t
                continue
            if side == "ours":
                continue
            if isinstance(side, dict) and "content" in side:
                blob, size = self._write_blob(side["content"].encode() if isinstance(side["content"], str) else side["content"])
                edits[path] = ["f", path, blob, size, (o or t)[4]]
                continue
            if o and t:
                texts = []
                for e in (b, o, t):
                    raw = self.read_blob(e[2]) if e else b""
                    texts.append(None if (b"\x00" in raw[:8192]) else raw)
                if None not in texts:
                    try:
                        r = merge3(*(x.decode("utf-8") for x in texts))
                    except UnicodeDecodeError:
                        r = None
                    if r is not None and r.clean:
                        blob, size = self._write_blob(r.text.encode())
                        edits[path] = ["f", path, blob, size, o[4]]
                        auto.append(path)
                        continue
            conflicts.append({"path": path, "ours": "deleted" if not o else "changed", "theirs": "deleted" if not t else "changed"})
        if conflicts:
            raise MergeConflict([c["path"] for c in conflicts], conflicts)
        return self.trees.apply(ours, edits), auto

    def merge(self, source: str, target: str, resolve: dict | None = None, author: str = "integrator") -> str:
        return self.merge_many([source], target, resolve, author)

    def merge_many(self, sources: list[str], target: str, resolve: dict | None = None, author: str = "integrator",
                   attempts: int = 50) -> str:
        """Merge several branches into `target` in one commit (fan-in). Retries on contention."""
        for attempt in range(attempts):
            tref, etag = self._ref(target)
            t_head = tref["commit"] if tref else None
            heads = []
            for s in sources:
                sref, _ = self._ref(s)
                if sref and sref["commit"] not in heads:
                    heads.append(sref["commit"])
            t_anc = self._ancestors(t_head)
            heads = [h for h in heads if h not in t_anc]           # already merged
            if not heads:
                return t_head
            if t_head is None and len(heads) == 1:
                new, seq = heads[0], self.commit(heads[0])["seq"]   # fast-forward a new branch
            elif len(heads) == 1 and t_head in self._ancestors(heads[0]):
                new, seq = heads[0], self.commit(heads[0])["seq"]   # fast-forward
            else:
                tree = self.tree_of(t_head)
                for h in heads:
                    base = self.merge_base(t_head, h)
                    tree, _ = self.merge_trees(self.tree_of(base), tree, self.tree_of(h), resolve)
                seq = (tref["seq"] + 1) if tref else 0
                new = self.put("commit", {"tree": tree, "parents": ([t_head] if t_head else []) + heads, "branch": target,
                                          "author": author, "message": f"merge {', '.join(sources[:5])}"
                                          + ("..." if len(sources) > 5 else ""), "ts": time.time(), "seq": seq,
                                          "edges": None, "fence": None})
            if self._cas_ref(target, new, seq, etag):
                return new
            time.sleep(min(0.2, 0.002 * (2 ** attempt)) * random.random())
        raise Conflict(f"could not merge into {target} after {attempts} attempts")

    # ---------------------------------------------------------------- lineage

    def _edges_of_commit(self, cid: str) -> list:
        c = self.commit(cid)
        return [tuple(e) for e in self.get("edges", c["edges"])["edges"]] if c.get("edges") else []

    def _manifest(self) -> tuple[dict, str | None]:
        got = self.store.get(f"{self.p}index/manifest")
        return (json.loads(got[0]), got[1]) if got else ({"segments": [], "watermarks": {}, "edges": 0}, None)

    def _tail_edges(self, manifest: dict) -> set:
        stop = set(manifest["watermarks"].values())
        seen, edges = set(), set()
        frontier = [r["commit"] for r in (self._ref(b)[0] for b in self.branches()) if r]
        while frontier:
            nxt = []
            for c in frontier:
                if c in seen or c in stop:
                    continue
                seen.add(c)
                edges.update(self._edges_of_commit(c))
                nxt.extend(self.commit(c)["parents"])
            frontier = nxt
        return edges

    def compact_index(self) -> dict:
        """Fold every edge not yet indexed into a new sorted segment; merge segments when there
        are more than four. Single writer (the compactor); the manifest is updated by CAS."""
        manifest, etag = self._manifest()
        new = self._tail_edges(manifest)
        segments = list(manifest["segments"])
        if new:
            segments.append(self.put("segment", {"edges": sorted([list(e) for e in new])}))
        if len(segments) > 4:
            merged = set()
            for s in segments:
                merged.update(tuple(e) for e in self.get("segment", s)["edges"])
            segments = [self.put("segment", {"edges": sorted([list(e) for e in merged])})]
        watermarks = {b: r["commit"] for b in self.branches() for r in [self._ref(b)[0]] if r}
        seen = set()
        for s in segments:
            for e in self.get("segment", s)["edges"]:
                seen.add(tuple(e))
        total = len(seen)
        body = _canon({"segments": segments, "watermarks": watermarks, "edges": total})
        ok = (self.store.put_if_absent(f"{self.p}index/manifest", body) if etag is None
              else self.store.put_if_match(f"{self.p}index/manifest", body, etag))
        if not ok:
            raise Conflict("another compactor updated the index")
        return {"segments": len(segments), "edges": total, "new_edges": len(new)}

    def _forward(self) -> dict:
        manifest, _ = self._manifest()
        segs = [self.get("segment", s)["edges"] for s in manifest["segments"]]
        tail = self._tail_edges(manifest)
        return {"segments": segs, "tail": tail}

    @staticmethod
    def _seg_out(seg: list, node: str) -> list[str]:
        i = bisect.bisect_left(seg, [node, ""])
        out = []
        while i < len(seg) and seg[i][0] == node:
            out.append(seg[i][1])
            i += 1
        return out

    def downstream(self, prov_id: str) -> set[str]:
        """Every provenance id derived (transitively) from `prov_id`, across all branches."""
        f = self._forward()
        tail_fwd: dict[str, set] = {}
        for a, b in f["tail"]:
            tail_fwd.setdefault(a, set()).add(b)
        out, frontier = set(), [prov_id]
        while frontier:
            nxt = []
            for n in frontier:
                outs = set(tail_fwd.get(n, ()))
                for seg in f["segments"]:
                    outs.update(self._seg_out(seg, n))
                for m in outs:
                    if m not in out:
                        out.add(m)
                        nxt.append(m)
            frontier = nxt
        out.discard(prov_id)
        return out

    def _retraction_sets(self) -> tuple[set, set]:
        keys = self.store.list(f"{self.p}retractions/")
        if self._retraction_cache and self._retraction_cache[0] == len(keys):
            return self._retraction_cache[1], self._retraction_cache[2]
        roots, affected = set(), set()
        for k in keys:
            rec = json.loads(self.codec.open(k.rsplit("/", 1)[1], "retraction", self.store.get(k)[0]))
            roots.add(rec["root"])
            affected.update(self.get("plist", rec["affected"])["ids"])
        self._retraction_cache = (len(keys), roots, affected)
        return roots, affected

    def retract(self, prov_id: str, reason: str, by: str = "unknown") -> dict:
        affected = self.downstream(prov_id)
        plist = self.put("plist", {"ids": sorted(affected)})
        rec = {"root": prov_id, "reason": reason[:1000], "by": by, "ts": time.time(), "affected": plist}
        plain = _canon(rec)
        rid = self.codec.id("retraction", plain)
        self.store.put_if_absent(f"{self.p}retractions/{rid}", self.codec.seal(rid, plain))
        self._retraction_cache = None
        return {"id": rid, "root": prov_id, "affected": affected}

    def is_retracted(self, prov_id: str) -> bool:
        roots, affected = self._retraction_sets()
        if not roots:
            return False
        if prov_id in roots or prov_id in affected:
            return True
        seen, frontier = set(), [prov_id]           # derived after the retraction: walk its inputs upstream
        while frontier:
            nxt = []
            for p in frontier:
                if p in seen:
                    continue
                seen.add(p)
                if p in roots or p in affected:
                    return True
                nxt.extend(self.prov(p).get("inputs", []))
            frontier = nxt
        return False

    def retracted_files(self, branch: str) -> list[dict]:
        ref, _ = self._ref(branch)
        if not ref:
            return []
        return [{"path": path, "prov": e[4]} for path, e in self.trees.walk(self.tree_of(ref["commit"])).items()
                if self.is_retracted(e[4])]

    # ---------------------------------------------------------------- leases

    def _lease_key(self, branch: str, path: str) -> str:
        return f"{self.p}refs/leases/" + hashlib.sha256(f"{branch}\x00{path}".encode()).hexdigest()[:32]

    def lease(self, branch: str, path: str, holder: str, ttl: float = 300) -> int | None:
        """Acquire (or renew) a lease; returns its fencing token, or None if someone else holds it."""
        key = self._lease_key(branch, path)
        got = self.store.get(key)
        now = time.time()
        if got is None:
            body = _canon({"holder": holder, "fence": 1, "expires_at": now + ttl})
            return 1 if self.store.put_if_absent(key, body) else None
        cur, etag = json.loads(got[0]), got[1]
        if cur["holder"] not in (None, holder) and cur["expires_at"] > now:
            return None
        fence = cur["fence"] if cur["holder"] == holder else cur["fence"] + 1
        body = _canon({"holder": holder, "fence": fence, "expires_at": now + ttl})
        return fence if self.store.put_if_match(key, body, etag) else None

    def release(self, branch: str, path: str, holder: str) -> bool:
        key = self._lease_key(branch, path)
        got = self.store.get(key)
        if not got:
            return False
        cur = json.loads(got[0])
        if cur["holder"] != holder:
            return False
        return self.store.put_if_match(key, _canon({"holder": None, "fence": cur["fence"], "expires_at": 0}), got[1]) is not None


class Branch:
    def __init__(self, repo: Repo, name: str):
        self.repo, self.name = repo, name

    def head(self) -> str | None:
        ref, _ = self.repo._ref(self.name)
        return ref["commit"] if ref else None

    def begin(self, writer: str = "unknown", shared: bool = False, task: str | None = None, model: str | None = None,
              fence: int | None = None) -> "Transaction":
        return Transaction(self, writer, shared, task, model, fence)

    def entry(self, path: str, at: str | None = None) -> dict | None:
        cid = at or self.head()
        e = self.repo.trees.get_path(self.repo.tree_of(cid), path)
        return {"blob": e[2], "size": e[3], "prov": e[4]} if e else None

    def read(self, path: str, at: str | None = None) -> bytes:
        e = self.entry(path, at)
        if e is None:
            raise FileNotFoundError(f"{path} not found on {self.name}")
        return self.repo.read_blob(e["blob"])

    def files(self, at: str | None = None) -> dict[str, dict]:
        cid = at or self.head()
        return {p: {"blob": e[2], "size": e[3], "prov": e[4]} for p, e in self.repo.trees.walk(self.repo.tree_of(cid)).items()}


class Transaction:
    def __init__(self, branch: Branch, writer: str, shared: bool, task, model, fence):
        self.branch, self.repo = branch, branch.repo
        self.writer, self.shared, self.task, self.model, self.fence = writer, shared, task, model, fence
        ref, self.etag = self.repo._ref(branch.name)
        self.base = ref["commit"] if ref else None
        self.seq = ref["seq"] + 1 if ref else 0
        self.edits: dict[str, list | None] = {}
        self.edges: list[list[str]] = []

    def write(self, path: str, data: bytes, inputs: list[str] | None = None, sources: list[str] | None = None,
              deterministic: bool = False) -> str:
        path = path.strip("/")
        if not path or ".." in path.split("/"):
            raise ValueError(f"bad path {path!r}")
        inputs = list(dict.fromkeys(inputs or []))
        blob, size = self.repo._write_blob(data)
        untrusted = bool(sources) or any(self.repo.prov(i).get("trust") == "untrusted" or self.repo.is_retracted(i)
                                         for i in inputs)
        prov = {"writer": self.writer, "task": self.task, "model": self.model, "path": path, "blob": blob,
                "inputs": inputs, "sources": list(sources or []), "trust": "untrusted" if untrusted else "trusted",
                "ts": 0 if deterministic else time.time(), "nonce": "" if deterministic else secrets.token_hex(8)}
        pid = self.repo.put("prov", prov)
        self.edits[path] = ["f", path, blob, size, pid]
        self.edges += [[i, pid] for i in inputs]
        return pid

    def delete(self, path: str) -> None:
        self.edits[path.strip("/")] = None

    def commit(self, message: str = "", attempts: int = 100) -> str:
        repo = self.repo
        base_tree = repo.tree_of(self.base)
        tree = repo.trees.apply(base_tree, self.edits)
        edges = repo.put("edges", {"edges": self.edges}) if self.edges else None
        base, etag, seq = self.base, self.etag, self.seq
        for attempt in range(attempts):
            cid = repo.put("commit", {"tree": tree, "parents": [base] if base else [], "branch": self.branch.name,
                                      "author": self.writer, "message": message, "ts": time.time(), "seq": seq,
                                      "edges": edges, "fence": self.fence})
            if repo._cas_ref(self.branch.name, cid, seq, etag):
                self.base, self.etag, self.seq, self.edits, self.edges = cid, None, seq + 1, {}, []
                return cid
            if not self.shared:
                raise Conflict(f"branch {self.branch.name} moved; it's single-writer (use shared=True to rebase and retry)")
            ref, etag = repo._ref(self.branch.name)              # rebase our changes onto the new head
            new_base = ref["commit"] if ref else None
            tree, _ = repo.merge_trees(base_tree, repo.tree_of(new_base), tree)
            base_tree, base, seq = repo.tree_of(new_base), new_base, (ref["seq"] + 1 if ref else 0)
            time.sleep(min(0.05, 0.0005 * (2 ** min(attempt, 10))) * random.random())
        raise Conflict(f"could not commit to {self.branch.name} after {attempts} attempts")
