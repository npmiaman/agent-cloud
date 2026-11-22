"""Vector search over workspace files.

- **Indexing:** every new version or move puts the file in `vector_queue` (a trigger). Files
  are embedded in 40-line chunks (path plus chunk text), the same chunks keyword search uses.
  Searches embed a bounded number of queued files first (read-your-writes for fast
  embedders); the worker drains the rest in the background (slow models).
- **Storage:** vectors are float32, AES-GCM encrypted with a key derived from the master key,
  and tagged with the model name. Changing the model re-embeds a workspace on its next search.
- **Index:** one HNSW graph per (workspace, model) per process, built on first use and kept
  current by replaying `vector_log`, so every server process sees every other's changes.
- **Search:** permission and prefix filters run inside the HNSW search (exact fallback when
  selective). A file scores its best chunk (cosine similarity).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import threading

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .auth import Principal
from .hnsw import HNSW

SYNC_DRAIN_FILES = 64        # files embedded inline per search (the rest: worker)
EMBED_BATCH = 64
_aead: AESGCM | None = None
_cache: dict[tuple, dict] = {}
_lock = threading.Lock()


def set_key(master_key: bytes) -> None:
    global _aead
    _aead = AESGCM(hmac.new(master_key, b"agentcloud/vectors/v1", hashlib.sha256).digest())


def available(ctx) -> bool:
    return getattr(ctx, "embedder", None) is not None


def _enc(vec: list[float]) -> bytes:
    raw = struct.pack(f"{len(vec)}f", *vec)
    nonce = os.urandom(12)
    return nonce + _aead.encrypt(nonce, raw, b"agentcloud-vector")


def _dec(blob: bytes) -> list[float]:
    raw = _aead.decrypt(blob[:12], blob[12:], b"agentcloud-vector")
    return list(struct.unpack(f"{len(raw) // 4}f", raw))


def _chunks_for(ctx, f, v) -> list[tuple[int, int, str]]:
    from .agent_env import SEARCH_MAX_FILE, TEXT_CACHE, is_texty
    if is_texty(f["path"], v["content_type"]) and v["size"] <= SEARCH_MAX_FILE:
        hit = TEXT_CACHE.get(ctx, f["owner_id"], v["blob_sha256"])
        if hit:
            return [(c[0], c[1], f"/{f['path']}\n{c[4]}") for c in hit[2]]
    return [(1, 1, f"/{f['path']}")]


def _remove_file(db, file_id: str) -> None:
    for r in db.all("SELECT id, workspace_id, model FROM vector_chunks WHERE file_id=?", file_id):
        db.exec("INSERT INTO vector_log(workspace_id, op, chunk_id, model) VALUES (?,?,?,?)", r["workspace_id"], "remove",
                r["id"], r["model"])
    db.exec("DELETE FROM vector_chunks WHERE file_id=?", file_id)


def drain(ctx, workspace_ids: list[str] | None = None, limit: int | None = None) -> dict:
    """Embed queued files. Returns {"embedded": files, "pending": files still queued}."""
    if not available(ctx):
        return {"embedded": 0, "pending": 0}
    db, emb = ctx.db, ctx.embedder
    model = emb.name
    with db.tx():
        for ws_id in workspace_ids or []:   # model changed, or never indexed: queue the whole workspace
            if not db.one("SELECT 1 FROM vector_chunks WHERE workspace_id=? AND model=? LIMIT 1", ws_id, model) and \
                    not db.one("SELECT 1 FROM vector_queue WHERE workspace_id=? LIMIT 1", ws_id):
                db.exec("""INSERT OR IGNORE INTO vector_queue(file_id, workspace_id) SELECT id, workspace_id FROM files
                           WHERE workspace_id=? AND kind='file' AND deleted_at IS NULL""", ws_id)
    done = 0
    marks = ",".join("?" * len(workspace_ids)) if workspace_ids else ""
    while limit is None or done < limit:
        take = EMBED_BATCH if limit is None else min(EMBED_BATCH, limit - done)
        if workspace_ids:
            rows = db.all(f"SELECT file_id FROM vector_queue WHERE workspace_id IN ({marks}) LIMIT ?", *workspace_ids, take)
        else:
            rows = db.all("SELECT file_id FROM vector_queue LIMIT ?", take)
        if not rows:
            break
        jobs = []          # (file row, version row, chunks)
        for r in rows:
            f = db.one("""SELECT f.id, f.workspace_id, f.path, f.kind, f.deleted_at, f.current_version, w.owner_id
                          FROM files f JOIN workspaces w ON w.id=f.workspace_id WHERE f.id=?""", r["file_id"])
            v = db.one("SELECT n, blob_sha256, size, content_type FROM versions WHERE file_id=? AND n=?",
                       r["file_id"], f["current_version"]) if f else None
            if f and f["kind"] == "file" and f["deleted_at"] is None and v and v["blob_sha256"]:
                jobs.append((f, v, _chunks_for(ctx, f, v)))
            else:
                jobs.append((None, None, [r["file_id"]]))
        texts = [c[2] for f, _, cs in jobs if f for c in cs]
        vecs = []
        for i in range(0, len(texts), 256):          # embed outside the write transaction (it can be slow)
            vecs.extend(emb.embed(texts[i:i + 256]))
        it = iter(vecs)
        with db.tx():
            for f, v, cs in jobs:
                if f is None:
                    _remove_file(db, cs[0])
                    db.exec("DELETE FROM vector_queue WHERE file_id=?", cs[0])
                    continue
                _remove_file(db, f["id"])
                for start, end, _ in cs:
                    cur = db.exec("""INSERT INTO vector_chunks(file_id, workspace_id, version, start_line, end_line, model, vec)
                                     VALUES (?,?,?,?,?,?,?)""", f["id"], f["workspace_id"], v["n"], start, end, model, _enc(next(it)))
                    db.exec("INSERT INTO vector_log(workspace_id, op, chunk_id, model) VALUES (?,?,?,?)",
                            f["workspace_id"], "add", cur.lastrowid, model)
                db.exec("DELETE FROM vector_queue WHERE file_id=?", f["id"])
        done += len(rows)
    if workspace_ids:
        pending = db.one(f"SELECT COUNT(*) n FROM vector_queue WHERE workspace_id IN ({marks})", *workspace_ids)["n"]
    else:
        pending = db.one("SELECT COUNT(*) n FROM vector_queue")["n"]
    return {"embedded": done, "pending": pending}


def _index_for(ctx, ws_id: str) -> dict:
    """This process's HNSW for (workspace, model), brought up to date from vector_log."""
    db, model = ctx.db, ctx.embedder.name
    key = (id(db), ws_id, model)
    with _lock:
        ent = _cache.get(key)
        if ent is None:
            last = db.one("SELECT COALESCE(MAX(seq), 0) s FROM vector_log WHERE workspace_id=?", ws_id)["s"]
            rows = db.all("SELECT id, file_id, start_line, end_line, vec FROM vector_chunks WHERE workspace_id=? AND model=?",
                          ws_id, model)
            ent = {"hnsw": None, "meta": {}, "seq": last}
            for r in rows:
                vec = _dec(bytes(r["vec"]))
                if ent["hnsw"] is None:
                    ent["hnsw"] = HNSW(len(vec), seed=1)
                ent["hnsw"].add(r["id"], vec)
                ent["meta"][r["id"]] = (r["file_id"], r["start_line"], r["end_line"])
            _cache[key] = ent
            return ent
        for lg in db.all("SELECT seq, op, chunk_id FROM vector_log WHERE workspace_id=? AND model=? AND seq > ? ORDER BY seq",
                         ws_id, model, ent["seq"]):
            if lg["op"] == "remove":
                if ent["hnsw"] is not None:
                    ent["hnsw"].remove(lg["chunk_id"])
                ent["meta"].pop(lg["chunk_id"], None)
            else:
                r = db.one("SELECT id, file_id, start_line, end_line, vec FROM vector_chunks WHERE id=?", lg["chunk_id"])
                if r is None:
                    continue          # added and removed since: a later 'remove' entry handles the graph
                vec = _dec(bytes(r["vec"]))
                if ent["hnsw"] is None:
                    ent["hnsw"] = HNSW(len(vec), seed=1)
                ent["hnsw"].add(r["id"], vec)
                ent["meta"][r["id"]] = (r["file_id"], r["start_line"], r["end_line"])
        mx = db.one("SELECT COALESCE(MAX(seq), 0) s FROM vector_log WHERE workspace_id=?", ws_id)["s"]
        ent["seq"] = max(ent["seq"], mx)
        return ent


def search(ctx, p: Principal, query: str, workspaces: list[dict], prefix: str, limit: int) -> tuple[list[dict], int]:
    """(results, files still waiting to be embedded)."""
    from .agent_env import TEXT_CACHE, _snippet, _terms
    from .auth import deny_read_prefixes, readable_prefixes
    from .paths import under
    db = ctx.db
    if not workspaces:
        return [], 0
    st = drain(ctx, [w["id"] for w in workspaces], limit=SYNC_DRAIN_FILES)
    qv = ctx.embedder.embed([query])[0]
    best: dict[str, tuple] = {}
    for ws in workspaces:
        ent = _index_for(ctx, ws["id"])
        if ent["hnsw"] is None or not len(ent["hnsw"]):
            continue
        allow_p = readable_prefixes(db, p, ws)
        deny_p = deny_read_prefixes(db, p, ws)
        paths: dict[str, str | None] = {}

        def path_of(fid):
            if fid not in paths:
                r = db.one("SELECT path FROM files WHERE id=? AND deleted_at IS NULL", fid)
                paths[fid] = r["path"] if r else None
            return paths[fid]

        def allow(cid, ent=ent):
            m = ent["meta"].get(cid)
            if not m:
                return False
            path = path_of(m[0])
            if path is None or (prefix and not path.startswith(prefix)):
                return False
            if allow_p is not None and not any(under(path, a) for a in allow_p):
                return False
            return not any(under(path, d) for d in deny_p)
        for cid, sim in ent["hnsw"].search(qv, max(limit * 4, 20), allow=allow):
            fid, start, end = ent["meta"][cid]
            if fid not in best or sim > best[fid][0]:
                best[fid] = (sim, start, end, ws, path_of(fid))
    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]
    terms = list(dict.fromkeys(_terms(query)))
    results = []
    for fid, (sim, start, end, ws, path) in ranked:
        meta = db.one("""SELECT f.updated_at, v.n, v.size, v.blob_sha256 FROM files f JOIN versions v ON v.file_id=f.id AND v.n=f.current_version
                         WHERE f.id=?""", fid)
        snippet = None
        hit = TEXT_CACHE.get(ctx, ws["owner_id"], meta["blob_sha256"])
        if hit:
            chunk = "\n".join(hit[0].splitlines()[start - 1:end])
            snippet = _snippet(chunk, terms) if terms else chunk[:160]
        results.append({"workspace": ws["name"], "workspace_id": ws["id"], "path": "/" + path, "version": meta["n"],
                        "size": meta["size"], "updated_at": meta["updated_at"], "score": round(float(sim), 6),
                        "lines": f"{start}-{end}", "matched_in": "semantic", "snippet": snippet})
    return results, st["pending"]


def status(ctx, owner_id: str) -> dict:
    db = ctx.db
    if not available(ctx):
        return {"embedder": None, "pending": 0, "indexed_chunks": 0}
    ws = [r["id"] for r in db.all("SELECT id FROM workspaces WHERE owner_id=? AND deleted_at IS NULL", owner_id)]
    if not ws:
        return {"embedder": ctx.embedder.name, "pending": 0, "indexed_chunks": 0}
    marks = ",".join("?" * len(ws))
    return {"embedder": ctx.embedder.name,
            "pending": db.one(f"SELECT COUNT(*) n FROM vector_queue WHERE workspace_id IN ({marks})", *ws)["n"],
            "indexed_chunks": db.one(f"SELECT COUNT(*) n FROM vector_chunks WHERE workspace_id IN ({marks}) AND model=?",
                                     *ws, ctx.embedder.name)["n"]}
