"""Persistent search index with exact top-k ranking.

- **Writes only enqueue.** A trigger on every new version and every move puts the file in
  `search_queue`. Searches index anything queued for their workspaces first, so results
  include the caller's latest writes. The worker also drains the queue.
- **Encrypted terms.** Index rows are 40-line chunks in an FTS5 table. Terms are stored as
  keyed hashes (HMAC-SHA256 truncated to 64 bits), in order, never as plaintext.
- **Scoring.** Each chunk scores FTS5 BM25 with column weights (content 1.0, path 1.5,
  scope 0), multiplied by a proximity factor 1 + ALPHA / (1 + span - k). Here `span` is the
  smallest token window containing all k distinct query terms in the chunk (k >= 2), so
  adjacent terms get the full boost. A file scores its best chunk.
- **Coverage.** At least 60% of the distinct query terms must appear in a chunk (content or
  path). This is expressed in the FTS query as an OR of AND-combinations, so FTS5 filters it.
- **Exact top-k via the Threshold Algorithm (Fagin et al.).** Rows stream in BM25 order. Since
  proximity multiplies BM25 by at most (1 + ALPHA), scanning stops once the next row's upper
  bound can't beat the current k-th file. Ties keep stream order, so the result is identical
  to an exhaustive scan.
- **Normalised scores.** Scores are relative to the top hit (1.0). BM25 statistics are global
  across the server's index, so raw values aren't returned.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math

from .auth import Principal, rights_for
from .db import Database

ALPHA = 0.5                  # maximum proximity boost (+50% for adjacent terms)
MAX_COMBO_TERMS = 8          # beyond this, coverage is checked after retrieval instead of in the query
QUEUE_BATCH = 500
_key = b""


def set_key(master_key: bytes) -> None:
    global _key
    _key = hmac.new(master_key, b"agentcloud/search/v1", hashlib.sha256).digest()


def h(term: str) -> str:
    return hmac.new(_key, term.encode(), hashlib.sha256).hexdigest()[:16]


def scope_token(workspace_id: str) -> str:
    return h("\x00workspace:" + workspace_id)


def available(db: Database) -> bool:
    return bool(getattr(db, "fts", False))


# ---------------------------------------------------------------- indexing

def _remove(db: Database, file_id: str) -> None:
    r = db.one("SELECT rows FROM search_files WHERE file_id=?", file_id)
    if r:
        rows = json.loads(r["rows"])
        for i in range(0, len(rows), 500):
            part = rows[i:i + 500]
            db.exec(f"DELETE FROM search_chunks WHERE rowid IN ({','.join('?' * len(part))})", *part)
        db.exec("DELETE FROM search_files WHERE file_id=?", file_id)


def index_file(ctx, file_id: str) -> None:
    """(Re)index one file's current state; removes it if deleted. Caller holds the transaction."""
    from .agent_env import SEARCH_MAX_FILE, TEXT_CACHE, _terms, is_texty
    db = ctx.db
    _remove(db, file_id)
    f = db.one("""SELECT f.id, f.workspace_id, f.path, f.kind, f.deleted_at, f.current_version, w.owner_id
                  FROM files f JOIN workspaces w ON w.id=f.workspace_id WHERE f.id=?""", file_id)
    if not f or f["kind"] != "file" or f["deleted_at"] is not None:
        return
    v = db.one("SELECT blob_sha256, size, content_type FROM versions WHERE file_id=? AND n=?", file_id, f["current_version"])
    if not v or v["blob_sha256"] is None:
        return
    path_terms = " ".join(h(t) for t in _terms(f["path"]))
    chunks = [(1, 1, "")]
    if is_texty(f["path"], v["content_type"]) and v["size"] <= SEARCH_MAX_FILE:
        hit = TEXT_CACHE.get(ctx, f["owner_id"], v["blob_sha256"])
        if hit:   # tokens kept in order: proximity needs positions
            chunks = [(c[0], c[1], " ".join(h(t) for t in _terms(c[4]))) for c in hit[2]] or chunks
    rows = []
    scope = scope_token(f["workspace_id"])
    for start, end, terms in chunks:
        cur = db.exec("""INSERT INTO search_chunks(terms, path_terms, scope, file_id, workspace_id, start_line, end_line)
                         VALUES (?,?,?,?,?,?,?)""", terms, path_terms, scope, file_id, f["workspace_id"], start, end)
        rows.append(cur.lastrowid)
    db.exec("INSERT INTO search_files(file_id, workspace_id, version, path, rows) VALUES (?,?,?,?,?)",
            file_id, f["workspace_id"], f["current_version"], f["path"], json.dumps(rows))


def drain(ctx, workspace_ids: list[str] | None = None, limit: int | None = None) -> int:
    """Index queued files (for these workspaces, or all). Also re-queues a workspace whose
    index is empty but which has files (a database from before the index, or a wiped index)."""
    db = ctx.db
    if not available(db):
        return 0
    done = 0
    with db.tx():
        for ws_id in workspace_ids or []:
            if not db.one("SELECT 1 FROM search_files WHERE workspace_id=? LIMIT 1", ws_id) and \
                    db.one("SELECT 1 FROM files WHERE workspace_id=? AND kind='file' AND deleted_at IS NULL LIMIT 1", ws_id):
                db.exec("""INSERT OR IGNORE INTO search_queue(file_id, workspace_id) SELECT id, workspace_id FROM files
                           WHERE workspace_id=? AND kind='file' AND deleted_at IS NULL""", ws_id)
    while True:
        if workspace_ids is None:
            rows = db.all("SELECT file_id FROM search_queue LIMIT ?", QUEUE_BATCH)
        else:
            marks = ",".join("?" * len(workspace_ids))
            rows = db.all(f"SELECT file_id FROM search_queue WHERE workspace_id IN ({marks}) LIMIT ?", *workspace_ids, QUEUE_BATCH)
        if not rows:
            return done
        with db.tx():
            for r in rows:
                index_file(ctx, r["file_id"])
                db.exec("DELETE FROM search_queue WHERE file_id=?", r["file_id"])
        done += len(rows)
        if limit and done >= limit:
            return done


# ---------------------------------------------------------------- querying

def _min_span(tokens: list[str], wanted: set[str]) -> tuple[int, int]:
    """(k, span): distinct wanted terms present, and the smallest window containing all of them."""
    present = {t for t in tokens if t in wanted}
    k = len(present)
    if k < 2:
        return k, k
    need, have, best, left = k, {}, len(tokens), 0
    for right, t in enumerate(tokens):
        if t in present:
            have[t] = have.get(t, 0) + 1
            if have[t] == 1:
                need -= 1
        while need == 0:
            best = min(best, right - left + 1)
            lt = tokens[left]
            if lt in present:
                have[lt] -= 1
                if have[lt] == 0:
                    need += 1
            left += 1
    return k, best


def _proximity(terms_text: str, wanted: set[str]) -> float:
    k, span = _min_span(terms_text.split(), wanted)
    return 1.0 if k < 2 else 1.0 + ALPHA / (1 + span - k)


def _match_expr(hashed: list[str], need: int, scopes: list[str]) -> str:
    from itertools import combinations
    q = [f'"{x}"' for x in hashed]
    if len(q) <= MAX_COMBO_TERMS and need > 1:
        body = " OR ".join("(" + " AND ".join(c) + ")" for c in combinations(q, need))
    else:
        body = " OR ".join(q)
    return "scope : (" + " OR ".join(f'"{s}"' for s in scopes) + ") AND (" + body + ")"


def search(ctx, p: Principal, query: str, workspaces: list[dict], prefix: str, limit: int,
           exhaustive: bool = False, stats: dict | None = None) -> list[dict]:
    import heapq

    from .agent_env import TEXT_CACHE, _snippet, _terms
    from .service import ApiError
    db = ctx.db
    terms = list(dict.fromkeys(_terms(query)))
    if not terms:
        raise ApiError(400, "bad_query", "search query is empty")
    if not workspaces:
        return []
    wss = {w["id"]: w for w in workspaces}
    drain(ctx, list(wss))
    need = max(1, math.ceil(len(terms) * 0.6))
    hashed = [h(t) for t in terms]
    wanted = set(hashed)
    expr = _match_expr(hashed, need, [scope_token(w) for w in wss])
    cur = db.connect().execute(
        """SELECT rowid, file_id, workspace_id, start_line, end_line, terms, path_terms,
                  bm25(search_chunks, 1.0, 1.5, 0.0) AS b
           FROM search_chunks WHERE search_chunks MATCH ? ORDER BY b, rowid""", (expr,))
    best: dict[str, tuple] = {}         # file_id -> (score, order, row)
    readable: dict[tuple, bool] = {}
    top: list[float] = []               # min-heap of the k best file scores
    scanned, order = 0, 0
    for row in cur:
        base = -row["b"]
        if not exhaustive and len(top) >= limit and base * (1 + ALPHA) <= top[0]:
            break                       # no remaining row can enter the top k
        scanned += 1
        ws = wss.get(row["workspace_id"])
        if ws is None:
            continue
        sf = db.one("SELECT path FROM search_files WHERE file_id=?", row["file_id"])
        if not sf or (prefix and not sf["path"].startswith(prefix)):
            continue
        key = (ws["id"], sf["path"])
        if key not in readable:
            readable[key] = p.is_user or rights_for(db, p, ws, sf["path"]).read
        if not readable[key]:
            continue
        if len(terms) > MAX_COMBO_TERMS:   # coverage wasn't enforced by the query
            have = set(row["terms"].split()) | set(row["path_terms"].split())
            if sum(1 for x in hashed if x in have) < need:
                continue
        score = base * _proximity(row["terms"], wanted)
        order += 1
        prev = best.get(row["file_id"])
        if prev is None or score > prev[0]:
            best[row["file_id"]] = (score, order, row, sf["path"], ws)
            top = heapq.nlargest(limit, (v[0] for v in best.values()))
            heapq.heapify(top)
    if stats is not None:
        stats["rows_scanned"] = scanned
    ranked = sorted(best.values(), key=lambda v: (-v[0], v[1]))[:limit]
    if not ranked:
        return []
    top_score = ranked[0][0] or 1.0
    results = []
    for score, _, row, path, ws in ranked:
        meta = db.one("""SELECT f.updated_at, v.n, v.size, v.blob_sha256 FROM files f JOIN versions v ON v.file_id=f.id AND v.n=f.current_version
                         WHERE f.id=?""", row["file_id"])
        low = path.lower()
        path_terms = set(_terms(path))
        in_name = any(t in path_terms or t in low for t in terms)
        in_content = any(x in row["terms"].split() for x in hashed)
        snippet, lines = None, None
        if in_content:
            lines = f"{row['start_line']}-{row['end_line']}"
            hit = TEXT_CACHE.get(ctx, ws["owner_id"], meta["blob_sha256"])
            if hit:
                snippet = _snippet("\n".join(hit[0].splitlines()[row["start_line"] - 1:row["end_line"]]), terms)
        results.append({
            "workspace": ws["name"], "workspace_id": ws["id"], "path": "/" + path, "version": meta["n"],
            "size": meta["size"], "updated_at": meta["updated_at"], "score": round(score / top_score, 6), "lines": lines,
            "matched_in": "+".join(k for k, ok in (("name", in_name), ("content", in_content)) if ok),
            "snippet": snippet,
        })
    return results
