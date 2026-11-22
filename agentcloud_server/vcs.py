"""Workspace version control: commits, history, diffs, rollback, branches and merges.

Every file change is already an immutable version, and every mutation stamps the
workspace's next sequence number. So a commit is just a named sequence number: creating
one is O(1) whatever the workspace size, and commits share all storage. The snapshot at
seq S is each file's latest version with seq <= S, at its path as of S.

Branches are forked workspaces: the fork's metadata points at the same encrypted blobs,
so a branch costs metadata only (one row per file), never a copy of the data. Merging is
a three-way merge by path against the fork point:

    base  = parent as of the fork      ours   = parent now      theirs = branch now
    theirs == base -> keep ours         ours == base -> take theirs
    ours == theirs -> nothing to do     otherwise   -> conflict (resolve ours/theirs)
"""
from __future__ import annotations

from . import service as S
from .auth import Principal, rights_for
from .db import Database, new_id, now
from .service import ApiError, Ctx


import hashlib
import json


def head_seq(db: Database, ws_id: str) -> int:
    return db.one("SELECT seq FROM workspaces WHERE id=?", ws_id)["seq"]


def snapshot(db: Database, ws_id: str, seq: int | None = None) -> dict[str, dict]:
    """{path: {file_id, version, sha256, size, content_type}} for live files at `seq` (None = now)."""
    if seq is None:
        rows = db.all("""SELECT f.id AS file_id, f.path, v.n AS version, v.blob_sha256 AS sha256, v.size, v.content_type
                         FROM files f JOIN versions v ON v.file_id=f.id AND v.n=f.current_version
                         WHERE f.workspace_id=? AND f.kind='file' AND f.deleted_at IS NULL""", ws_id)
        return {r["path"]: dict(r) for r in rows}
    rows = db.all("""
        WITH last AS (
          SELECT v.file_id, MAX(v.n) AS n FROM versions v JOIN files f ON f.id=v.file_id
          WHERE f.workspace_id=? AND f.kind='file' AND COALESCE(v.seq, 0) <= ? GROUP BY v.file_id)
        SELECT f.id AS file_id, v.n AS version, v.blob_sha256 AS sha256, v.size, v.content_type,
               COALESCE((SELECT fp.path FROM file_paths fp WHERE fp.file_id=f.id AND fp.seq <= ?
                         ORDER BY fp.seq DESC LIMIT 1), f.path) AS path
        FROM last JOIN files f ON f.id=last.file_id JOIN versions v ON v.file_id=last.file_id AND v.n=last.n
        WHERE v.blob_sha256 IS NOT NULL""", ws_id, seq, seq)
    return {r["path"]: dict(r) for r in rows}


def _readable(db: Database, p: Principal, ws: dict, snap: dict[str, dict]) -> dict[str, dict]:
    if p.is_user:
        return snap
    return {path: e for path, e in snap.items() if rights_for(db, p, ws, path).read}


def diff(a: dict[str, dict], b: dict[str, dict]) -> dict[str, list]:
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(p for p in set(a) & set(b) if a[p]["sha256"] != b[p]["sha256"])
    # a file that moved keeps its id: report it as a rename rather than remove + add
    by_id_a = {e["file_id"]: p for p, e in a.items()}
    renamed = []
    for p in list(added):
        old = by_id_a.get(b[p]["file_id"])
        if old in removed:
            renamed.append({"from": old, "to": p, "changed": a[old]["sha256"] != b[p]["sha256"]})
            added.remove(p)
            removed.remove(old)
    return {"added": added, "removed": removed, "changed": changed, "renamed": renamed}


# ---------------------------------------------------------------- Merkle trees with prolly-tree folders
#
# Each folder is a prolly tree (probabilistic B-tree): its sorted entries are cut into leaf
# nodes wherever hash(name) hits a 1-in-LEAF_TARGET condition (or MAX_NODE entries), and leaf
# references are indexed the same way by internal nodes, level by level. Boundaries depend
# only on content, so a given set of entries always yields the same root hash (history
# independence), and one change rewrites only the nodes on its path. A folder that fits in one
# leaf is stored exactly as before: a JSON list of entries.

EMPTY_TREE = hashlib.sha256(b"[]").hexdigest()
LEAF_TARGET = 64          # average entries per leaf
INTERNAL_TARGET = 64      # average children per internal node
MAX_NODE = 1024           # hard cap on node size


def _boundary(name: str, level: int, target: int) -> bool:
    d = hashlib.blake2b(f"{level}:{name}".encode(), digest_size=8).digest()
    return int.from_bytes(d, "big") % target == 0


def _min_node(target: int) -> int:
    return max(2, target // 2)      # no tiny tail nodes; folders under target/2 entries are always one node


def _chunk(items: list, key, level: int, target: int) -> list[list]:
    """Cut points: content-defined (hash of the key) once a node has half the target size,
    or forced at MAX_NODE. A deterministic function of the sorted items."""
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


def _store_raw(db: Database, obj) -> str:
    raw = json.dumps(obj, separators=(",", ":"))
    h = hashlib.sha256(raw.encode()).hexdigest()
    db.exec("INSERT OR IGNORE INTO trees(hash, entries) VALUES (?,?)", h, raw)
    return h


def store_tree(db: Database, entries: list) -> str:
    """Store one leaf (a JSON list of entries, sorted by name)."""
    return _store_raw(db, sorted(entries, key=lambda e: e[1]))


def load_tree(db: Database, h: str):
    if h == EMPTY_TREE:
        return []
    r = db.one("SELECT entries FROM trees WHERE hash=?", h)
    if r is None:
        raise ApiError(500, "TREE_MISSING", f"tree {h[:12]} is missing")
    return json.loads(r["entries"])


def _build_internal(db: Database, refs: list, level: int) -> str:
    while len(refs) > 1:
        groups = _chunk(refs, lambda r: r[0], level, INTERNAL_TARGET)
        if len(groups) == len(refs):       # no reduction (every ref a boundary): fall back to fixed-size groups
            groups = [refs[i:i + MAX_NODE] for i in range(0, len(refs), MAX_NODE)]
        refs = [[g[0][0], _store_raw(db, {"i": level, "c": g})] for g in groups]
        level += 1
    return refs[0][1]


def store_folder(db: Database, entries: list) -> str:
    """Store a folder's entries (any order) as a prolly tree; returns the root hash."""
    entries = sorted(entries, key=lambda e: e[1])
    if not entries:
        return EMPTY_TREE
    leaves = _chunk(entries, lambda e: e[1], 0, LEAF_TARGET)
    if len(leaves) == 1:
        return store_tree(db, leaves[0])
    return _build_internal(db, [[lf[0][1], store_tree(db, lf)] for lf in leaves], 1)


def _leaf_refs(db: Database, h: str, cache: dict) -> list:
    """All leaf references [first_name, hash] of a folder, loading internal nodes only."""
    node = cache[h] if h in cache else cache.setdefault(h, load_tree(db, h))
    if isinstance(node, list):
        return [[node[0][1] if node else "", h]]
    out = []
    for first, child in node["c"]:
        cn = cache[child] if child in cache else cache.setdefault(child, load_tree(db, child))
        if isinstance(cn, list):
            out.append([first, child])
        else:
            out.extend(_leaf_refs(db, child, cache))
    return out


def folder_entries(db: Database, h: str) -> list:
    node = load_tree(db, h)
    if isinstance(node, list):
        return node
    out = []
    for _, child in node["c"]:
        out.extend(folder_entries(db, child))
    return out


def folder_lookup(db: Database, h: str, names: set[str]) -> dict[str, list]:
    """Entries for `names` in a folder, loading only the nodes on their paths."""
    import bisect
    out: dict[str, list] = {}

    def rec(hh: str, want: set[str]):
        node = load_tree(db, hh)
        if isinstance(node, list):
            for e in node:
                if e[1] in want:
                    out[e[1]] = e
            return
        firsts = [c[0] for c in node["c"]]
        by_child: dict[int, set] = {}
        for n in want:
            by_child.setdefault(max(0, bisect.bisect_right(firsts, n) - 1), set()).add(n)
        for i, ns in by_child.items():
            rec(node["c"][i][1], ns)
    if h != EMPTY_TREE and names:
        rec(h, set(names))
    return out


def folder_update(db: Database, h: str, edits: dict[str, list | None]) -> str:
    """Apply {name: entry or None} to a folder. Only leaves holding edited names are rewritten
    (extended forward until chunk boundaries realign); internal levels are re-derived from the
    leaf references, and unchanged nodes hash identically, so they aren't stored again. The
    result is identical to store_folder() of the edited entry set."""
    import bisect
    node = load_tree(db, h) if h != EMPTY_TREE else []
    if isinstance(node, list):
        cur = {e[1]: e for e in node}
        for name, e in edits.items():
            if e is None:
                cur.pop(name, None)
            else:
                cur[name] = e
        return store_folder(db, list(cur.values()))
    cache: dict = {}
    refs = _leaf_refs(db, h, cache)
    firsts = [r[0] for r in refs]
    per_leaf: dict[int, dict] = {}
    for name, e in edits.items():
        per_leaf.setdefault(max(0, bisect.bisect_right(firsts, name) - 1), {})[name] = e

    def leaf_entries(i: int) -> list:
        cur = {e[1]: e for e in load_tree(db, refs[i][1])}
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
        new_refs.extend([c[0][1], store_tree(db, c)] for c in chunks)
        i = j + 1
    if not new_refs:
        return EMPTY_TREE
    if len(new_refs) == 1:
        return new_refs[0][1]
    return _build_internal(db, new_refs, 1)


def _file_entry(name: str, e: dict) -> list:
    return ["f", name, e["file_id"], e["version"], e["sha256"], e["size"], e["content_type"]]


def build_tree(db: Database, snap: dict[str, dict]) -> str:
    """Store the tree for a full snapshot (used for a workspace's first commit)."""
    def build(items: dict[str, dict]) -> str:
        files, dirs = [], {}
        for rel, e in items.items():
            head, sep, rest = rel.partition("/")
            if sep:
                dirs.setdefault(head, {})[rest] = e
            else:
                files.append(_file_entry(head, e))
        return store_folder(db, files + [["d", name, build(sub)] for name, sub in dirs.items()])
    return build(snap)


def apply_edits(db: Database, root: str, edits: dict[str, dict | None]) -> str:
    """New tree = `root` with `edits` applied ({path: entry or None to remove}). Only nodes on
    edited paths are loaded and rewritten; everything else is shared with `root`."""
    def apply(h: str, sub: dict[str, dict | None]) -> str:
        own: dict[str, list | None] = {}
        child_edits: dict[str, dict] = {}
        for rel, e in sub.items():
            head, sep, rest = rel.partition("/")
            if sep:
                child_edits.setdefault(head, {})[rest] = e
            else:
                own[head] = None if e is None else _file_entry(head, e)
        current = folder_lookup(db, h, set(child_edits) | {n for n, e in own.items() if e is None})
        for name, e in list(own.items()):
            if e is None and not (current.get(name) and current[name][0] == "f"):
                del own[name]          # nothing to delete (or it's a folder, which file deletes don't touch)
        for name, ce in child_edits.items():
            cur = current.get(name)
            child = apply(cur[2] if cur and cur[0] == "d" else EMPTY_TREE, ce)
            own[name] = None if child == EMPTY_TREE else ["d", name, child]
        return folder_update(db, h, own) if own else h
    return apply(root, edits)


def _state_at(db: Database, file_id: str, seq: int) -> dict | None:
    v = db.one("""SELECT n, blob_sha256, size, content_type FROM versions WHERE file_id=? AND COALESCE(seq, 0) <= ?
                  ORDER BY n DESC LIMIT 1""", file_id, seq)
    if not v or v["blob_sha256"] is None:
        return None
    p = db.one("SELECT path FROM file_paths WHERE file_id=? AND seq <= ? ORDER BY seq DESC LIMIT 1", file_id, seq)
    path = p["path"] if p else db.one("SELECT path FROM files WHERE id=?", file_id)["path"]
    return {"file_id": file_id, "path": path, "version": v["n"], "sha256": v["blob_sha256"], "size": v["size"],
            "content_type": v["content_type"]}


def tree_at(db: Database, ws_id: str, seq: int) -> str:
    """The tree for the workspace as of `seq`, built incrementally from the latest earlier
    commit that has a tree (only files changed since then are touched)."""
    prev = db.one("""SELECT seq, tree FROM commits WHERE workspace_id=? AND tree IS NOT NULL AND seq <= ?
                     ORDER BY seq DESC, created_at DESC LIMIT 1""", ws_id, seq)
    if not prev:
        return build_tree(db, snapshot(db, ws_id, seq))
    if prev["seq"] == seq:
        return prev["tree"]
    edits: dict[str, dict | None] = {}
    changed = [r["file_id"] for r in db.all("SELECT DISTINCT file_id FROM changes WHERE workspace_id=? AND seq > ? AND seq <= ?",
                                            ws_id, prev["seq"], seq)]
    for fid in changed:
        old = _state_at(db, fid, prev["seq"])
        if old:
            edits.setdefault(old["path"], None)
    for fid in changed:
        new = _state_at(db, fid, seq)
        if new:
            edits[new["path"]] = new
    return apply_edits(db, prev["tree"], edits)


def walk(db: Database, h: str, prefix: str = "") -> dict[str, dict]:
    out = {}
    for e in folder_entries(db, h):
        if e[0] == "f":
            out[prefix + e[1]] = {"file_id": e[2], "version": e[3], "sha256": e[4], "size": e[5], "content_type": e[6]}
        else:
            out.update(walk(db, e[2], prefix + e[1] + "/"))
    return out


def tree_snapshot(db: Database, commit_id: str) -> dict[str, dict]:
    r = db.one("SELECT workspace_id, seq, tree FROM commits WHERE id=?", commit_id)
    if not r:
        raise ApiError(404, "COMMIT_NOT_FOUND", f"commit {commit_id} not found")
    return walk(db, r["tree"]) if r["tree"] else snapshot(db, r["workspace_id"], r["seq"])


def _folder_delta(db: Database, ha: str | None, hb: str | None) -> tuple[list, list]:
    """Entries that differ between two folders. Expands both prolly trees level by level,
    dropping any node whose hash appears on both sides, so shared subtrees are never loaded."""
    cache: dict = {}

    def node(h):
        if h not in cache:
            cache[h] = load_tree(db, h)
        return cache[h]
    fa = [ha] if ha and ha != EMPTY_TREE else []
    fb = [hb] if hb and hb != EMPTY_TREE else []
    while True:
        common = set(fa) & set(fb)
        fa = [x for x in fa if x not in common]
        fb = [x for x in fb if x not in common]
        grew = False
        for side in (fa, fb):
            nxt = []
            for x in side:
                n = node(x)
                if isinstance(n, dict):
                    nxt.extend(c[1] for c in n["c"])
                    grew = True
                else:
                    nxt.append(x)
            side[:] = nxt
        if not grew:
            break
    ea = [e for x in fa for e in node(x)]
    eb = [e for x in fb for e in node(x)]
    sa = {json.dumps(e) for e in ea}
    sb = {json.dumps(e) for e in eb}
    return [e for e in ea if json.dumps(e) not in sb], [e for e in eb if json.dumps(e) not in sa]


def tree_diff(db: Database, a: str, b: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Entries that differ between two trees, as (left, right) partial snapshots. Identical
    subtrees and identical prolly nodes (same hash) are never loaded."""
    left: dict[str, dict] = {}
    right: dict[str, dict] = {}

    def file_of(e):
        return {"file_id": e[2], "version": e[3], "sha256": e[4], "size": e[5], "content_type": e[6]}

    def rec(ha: str | None, hb: str | None, prefix: str):
        if ha == hb:
            return
        da, db_ = _folder_delta(db, ha, hb)
        ea = {e[1]: e for e in da}
        eb = {e[1]: e for e in db_}
        for name in set(ea) | set(eb):
            x, y = ea.get(name), eb.get(name)
            if x and y and x[0] == "d" and y[0] == "d":
                rec(x[2], y[2], prefix + name + "/")
                continue
            if x and y and x[0] == "f" and y[0] == "f" and x[4] == y[4] and x[2] == y[2]:
                continue
            for e, target in ((x, left), (y, right)):
                if not e:
                    continue
                if e[0] == "f":
                    target[prefix + name] = file_of(e)
                else:
                    target.update(walk(db, e[2], prefix + name + "/"))
    rec(a, b, "")
    for path in [p for p in left if p in right and left[p]["sha256"] == right[p]["sha256"]]:
        if left[path]["file_id"] == right[path]["file_id"]:
            del left[path], right[path]
    return left, right


def _ref_tree(db: Database, ws: dict, ref: str | None) -> str | None:
    """Tree hash for a commit id or 'head'/None (now). None if a legacy commit has no tree."""
    if ref in (None, "", "head", "now"):
        return tree_at(db, ws["id"], head_seq(db, ws["id"]))
    r = db.one("SELECT tree FROM commits WHERE id=? AND workspace_id=?", ref, ws["id"])
    if not r:
        raise ApiError(404, "COMMIT_NOT_FOUND", f"commit {ref} not found in '{ws['name']}'")
    return r["tree"]


def _commit_seq(db: Database, ws: dict, ref: str | None) -> int | None:
    """Resolve a commit id, 'head'/None (= now) or a branch's fork point ('base')."""
    if ref in (None, "", "head", "now"):
        return None
    if ref == "base":
        if not ws.get("forked_from"):
            raise ApiError(400, "NOT_A_BRANCH", f"'{ws['name']}' is not a branch")
        return None  # handled by callers that know the parent
    r = db.one("SELECT seq FROM commits WHERE id=? AND workspace_id=?", ref, ws["id"])
    if not r:
        raise ApiError(404, "COMMIT_NOT_FOUND", f"commit {ref} not found in '{ws['name']}'")
    return r["seq"]


def commit(ctx: Ctx, p: Principal, ws: dict, message: str) -> dict:
    S.require(rights_for(ctx.db, p, ws, ""), "read", "")
    with ctx.db.tx():
        seq = head_seq(ctx.db, ws["id"])
        last = ctx.db.one("SELECT * FROM commits WHERE workspace_id=? ORDER BY seq DESC, created_at DESC LIMIT 1", ws["id"])
        tree = tree_at(ctx.db, ws["id"], seq)          # incremental: only paths changed since the last commit
        cid = new_id("cmt")
        ctx.db.exec("INSERT INTO commits(id, workspace_id, seq, message, actor, created_at, tree) VALUES (?,?,?,?,?,?,?)",
                    cid, ws["id"], seq, (message or "")[:500], p.actor, now(), tree)
        S.log(ctx.db, ws["owner_id"], ws["id"], p.actor, "commit", None, commit_id=cid, seq=seq, message=message)
    files = ctx.db.one("SELECT COUNT(*) n FROM files WHERE workspace_id=? AND kind='file' AND deleted_at IS NULL", ws["id"])["n"]
    since = None
    if last:
        base = last["tree"] or build_tree(ctx.db, snapshot(ctx.db, ws["id"], last["seq"]))
        since = diff(*tree_diff(ctx.db, base, tree))     # cost tracks the change, not the workspace
    return {"id": cid, "seq": seq, "tree": tree, "message": message, "parent": last["id"] if last else None,
            "files": files, "since_parent": since}


def log(ctx: Ctx, p: Principal, ws: dict, limit: int = 50) -> list[dict]:
    S.require(rights_for(ctx.db, p, ws, ""), "read", "")
    rows = ctx.db.all("SELECT * FROM commits WHERE workspace_id=? ORDER BY seq DESC, created_at DESC LIMIT ?", ws["id"], limit)
    return [{"id": r["id"], "seq": r["seq"], "message": r["message"], "actor": r["actor"], "created_at": r["created_at"]} for r in rows]


def compare(ctx: Ctx, p: Principal, ws: dict, frm: str | None, to: str | None) -> dict:
    S.require(rights_for(ctx.db, p, ws, ""), "read", "")
    ta, tb = _ref_tree(ctx.db, ws, frm), _ref_tree(ctx.db, ws, to)
    if ta is None or tb is None:   # a commit from before trees existed
        a = snapshot(ctx.db, ws["id"], _commit_seq(ctx.db, ws, frm))
        b = snapshot(ctx.db, ws["id"], _commit_seq(ctx.db, ws, to))
    else:
        a, b = tree_diff(ctx.db, ta, tb)
    return diff(_readable(ctx.db, p, ws, a), _readable(ctx.db, p, ws, b))


def _apply_target(ctx: Ctx, p: Principal, ws: dict, target: dict[str, dict], message: str) -> dict:
    """Make the workspace's live files equal `target` (same org, so blobs are shared), in one transaction."""
    return _apply_diff(ctx, p, ws, diff(snapshot(ctx.db, ws["id"]), target), target, message)


def _apply_diff(ctx: Ctx, p: Principal, ws: dict, d: dict, target: dict[str, dict], message: str) -> dict:
    """Apply a diff (from the live state to `target`) in one transaction. `target` needs entries
    for every added, changed and renamed path."""
    d = {k: list(v) for k, v in d.items()}
    applied = {"written": [], "deleted": [], "moved": []}
    with ctx.db.tx():
        for r in d["renamed"]:
            S.move(ctx, p, ws, r["from"], r["to"], message)
            applied["moved"].append(r)
            if r["changed"]:
                d["changed"].append(r["to"])
        for path in d["removed"]:
            res = S.delete_file(ctx, p, ws, path, None, message)
            if res.get("status") == "pending_approval":
                raise ApiError(409, "NEEDS_APPROVAL", f"deleting /{path} needs approval, so nothing was changed")
            applied["deleted"].append(path)
        for path in d["added"] + d["changed"]:
            e = target[path]
            f = S.file_row(ctx.db, ws["id"], path)
            if f and f["kind"] == "file" and e.get("version") and S.version_row(ctx.db, f["id"], e["version"]) and \
                    S.version_row(ctx.db, f["id"], e["version"])["blob_sha256"] == e["sha256"]:
                res = S.restore(ctx, p, ws, path, e["version"], message)          # same file: restore keeps lineage
            else:
                res = _write_blob(ctx, p, ws, path, e, message)
            if res.get("status") == "pending_approval":
                raise ApiError(409, "NEEDS_APPROVAL", f"/{path} needs approval, so nothing was changed")
            applied["written"].append(path)
    return applied


def _write_blob(ctx: Ctx, p: Principal, ws: dict, path: str, e: dict, message: str) -> dict:
    """Write existing content (already in this organisation's blob store) to `path` without re-uploading."""
    from . import coordination as C
    from . import provenance as Pv
    r = S.check_write(ctx, p, ws, path)
    if not ctx.blobs.exists(ws["owner_id"], e["sha256"]):
        raise ApiError(500, "BLOB_MISSING", f"content for /{path} is missing from storage")
    with ctx.db.tx():
        C.check_write(ctx.db, ws, p.actor, path, Pv.current().fence)
        f = S.live_row(ctx.db, ws["id"], path)
        cur = S.live_version(f)
        if S._needs_approval(r.write_approval, cur > 0):
            return {"status": "pending_approval", "path": path}
        S.ensure_quota(ctx.db, ws, e["size"], e["sha256"])
        if not p.is_user:
            C.check_budget(ctx.db, p.id, e["size"], proposal=False)
            C.charge(ctx.db, p.id, e["size"])
        res = S._apply_version(ctx.db, ws, path, sha=e["sha256"], size=e["size"], ctype=e["content_type"], op="write",
                               actor=p.actor, message=message)
        S.log(ctx.db, ws["owner_id"], ws["id"], p.actor, res["op"], path, res["id"], version=res["version"], size=e["size"],
              base=cur)
        return res


def rollback(ctx: Ctx, p: Principal, ws: dict, ref: str) -> dict:
    """Make the workspace look exactly like commit `ref`. History is kept: rollback adds new versions."""
    seq = _commit_seq(ctx.db, ws, ref)
    if seq is None:
        raise ApiError(400, "INVALID_REF", "roll back to a commit id")
    t_commit = _ref_tree(ctx.db, ws, ref)
    if t_commit is None:
        cur_part, target_part = snapshot(ctx.db, ws["id"]), snapshot(ctx.db, ws["id"], seq)
    else:
        cur_part, target_part = tree_diff(ctx.db, tree_at(ctx.db, ws["id"], head_seq(ctx.db, ws["id"])), t_commit)
    if not p.is_user:
        hidden = [path for path in set(target_part) | set(cur_part) if not rights_for(ctx.db, p, ws, path).write]
        if hidden:
            raise ApiError(403, "FORBIDDEN", f"a rollback would change {len(hidden)} file(s) you can't write (e.g. /{hidden[0]})")
    applied = _apply_diff(ctx, p, ws, diff(cur_part, target_part), target_part, f"rollback to {ref}")
    S.log(ctx.db, ws["owner_id"], ws["id"], p.actor, "rollback", None, commit_id=ref, **{k: len(v) for k, v in applied.items()})
    return {"rolled_back_to": ref, **applied}


def fork(ctx: Ctx, p: Principal, ws: dict, name: str, make_workspace) -> dict:
    """Create a branch: a new workspace with the same files (metadata only; storage is shared)."""
    S.require(rights_for(ctx.db, p, ws, ""), "read", "")
    snap = _readable(ctx.db, p, ws, snapshot(ctx.db, ws["id"]))
    size = sum(e["size"] for e in snap.values())
    branch = make_workspace(name, size)
    with ctx.db.tx():
        base_seq = head_seq(ctx.db, ws["id"])
        ctx.db.exec("UPDATE workspaces SET forked_from=?, fork_seq=?, trust_policy=? WHERE id=?",
                    ws["id"], base_seq, ws.get("trust_policy"), branch["id"])
        t = now()
        for path, e in snap.items():
            seq = S.next_seq(ctx.db, branch["id"])
            fid = new_id("fil")
            ctx.db.exec("""INSERT INTO files(id, workspace_id, path, kind, current_version, created_by, created_at, updated_at)
                           VALUES (?,?,?,?,1,?,?,?)""", fid, branch["id"], path, "file", p.actor, t, t)
            ctx.db.exec("INSERT INTO file_paths(file_id, seq, path) VALUES (?,?,?)", fid, seq, path)
            vid = new_id("ver")
            ctx.db.exec("""INSERT INTO versions(id, file_id, n, blob_sha256, size, content_type, op, actor, message, created_at, seq)
                           VALUES (?,?,1,?,?,?,'copy',?,?,?,?)""", vid, fid, e["sha256"], e["size"], e["content_type"], p.actor,
                        f"branched from {ws['name']} /{path} v{e['version']}", t, seq)
            old = ctx.db.one("""SELECT p.trust, p.reasons FROM provenance p JOIN versions v ON v.id=p.version_id
                                WHERE v.file_id=? AND v.n=?""", e["file_id"], e["version"])
            from . import provenance as Pv
            import json as _json
            Pv.record(ctx.db, version_id=vid, file_id=fid, n=1, owner_id=ws["owner_id"], sha=e["sha256"], actor=p.actor,
                      prov={"actor": p.actor, "credential_id": p.token_id, "inputs": [(e["file_id"], e["version"])], "sources": [],
                            "trust": old["trust"] if old and old["trust"] != "endorsed" else "trusted",
                            "reasons": _json.loads(old["reasons"]) if old else []})
            S._ensure_parent_folders(ctx.db, branch, path, p.actor)
        S.invalidate_usage(ctx.db, branch["id"])
        S.log(ctx.db, ws["owner_id"], ws["id"], p.actor, "branch", None, branch_id=branch["id"], name=name, files=len(snap),
              base_seq=base_seq)
    return {"branch": branch["name"], "workspace_id": branch["id"], "files": len(snap), "base": base_seq}


def merge_plan(ctx: Ctx, p: Principal, branch: dict) -> tuple[dict, dict, dict, dict]:
    if not branch.get("forked_from"):
        raise ApiError(400, "NOT_A_BRANCH", f"'{branch['name']}' is not a branch")
    parent = S.get_workspace(ctx, p, branch["forked_from"])
    base = _readable(ctx.db, p, parent, snapshot(ctx.db, parent["id"], branch["fork_seq"]))
    ours = _readable(ctx.db, p, parent, snapshot(ctx.db, parent["id"]))
    theirs = snapshot(ctx.db, branch["id"])
    return parent, base, ours, theirs


def preview(ctx: Ctx, p: Principal, branch: dict) -> dict:
    parent, base, ours, theirs = merge_plan(ctx, p, branch)
    take, conflicts, auto = _three_way(ctx, parent, base, ours, theirs)
    return {"into": parent["name"], "changes": sorted(take), "auto_merged": sorted(auto), "conflicts": conflicts}


TEXT_MERGE_MAX = 2 * 1024 * 1024


def _text_of(ctx: Ctx, ns: str, e: dict | None) -> str | None:
    if e is None:
        return ""
    if e["size"] > TEXT_MERGE_MAX:
        return None
    data = ctx.blobs.open(ns, e["sha256"])
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _three_way(ctx: Ctx, parent: dict, base: dict, ours: dict, theirs: dict) -> tuple[dict, list, dict]:
    """Per path: take one side, auto-merge text line by line (diff3), or report a conflict."""
    from .merge3 import merge3
    take: dict[str, dict | None] = {}     # path -> entry to write, or None to delete
    auto: dict[str, str] = {}             # path -> merged text (clean line-level merge)
    conflicts = []
    sha = lambda m, k: m[k]["sha256"] if k in m else None   # noqa: E731
    for path in set(base) | set(ours) | set(theirs):
        b, o, t = sha(base, path), sha(ours, path), sha(theirs, path)
        if t == b or o == t:
            continue
        if o == b:
            take[path] = theirs.get(path)
            continue
        c = {"path": "/" + path, "base": b is not None, "ours": "deleted" if o is None else "changed",
             "theirs": "deleted" if t is None else "changed"}
        if o is not None and t is not None:
            ns = parent["owner_id"]
            tb, to, tt = _text_of(ctx, ns, base.get(path)), _text_of(ctx, ns, ours.get(path)), _text_of(ctx, ns, theirs.get(path))
            if None not in (tb, to, tt):
                r = merge3(tb, to, tt, labels=("ours", "theirs"))
                if r.clean:
                    auto[path] = r.text
                    continue
                c["hunks"] = r.conflicts
                c["merged_with_markers"] = r.text
        conflicts.append(c)
    return take, sorted(conflicts, key=lambda c: c["path"]), auto


def merge(ctx: Ctx, p: Principal, branch: dict, resolve: dict[str, str] | None = None, message: str = "") -> dict:
    """Merge a branch back into the workspace it was forked from, in one transaction."""
    parent, base, ours, theirs = merge_plan(ctx, p, branch)
    take, conflicts, auto = _three_way(ctx, parent, base, ours, theirs)
    resolve = {k.lstrip("/"): v for k, v in (resolve or {}).items()}
    open_conflicts = [c for c in conflicts if c["path"].lstrip("/") not in resolve]
    if open_conflicts:
        raise ApiError(409, "MERGE_CONFLICT", f"{len(open_conflicts)} conflict(s): " +
                       ", ".join(c["path"] for c in open_conflicts[:5]) + ". Resolve each with resolve={path: 'ours'|'theirs'}.",
                       conflicts=open_conflicts)
    def new_blob(path: str, text: str) -> dict:
        sha, size = ctx.blobs.put_bytes(parent["owner_id"], text.encode("utf-8"))
        like = ours.get(path) or theirs.get(path) or {}
        return {"file_id": like.get("file_id"), "version": None, "sha256": sha, "size": size,
                "content_type": like.get("content_type") or S.guess_type(path, None)}

    for path, text in auto.items():
        take[path] = new_blob(path, text)
    for path, side in resolve.items():
        if side == "theirs":
            take[path] = theirs.get(path)
        elif isinstance(side, dict) and isinstance(side.get("content"), str):
            take[path] = new_blob(path, side["content"])
        elif side != "ours":
            raise ApiError(400, "INVALID_RESOLUTION", f"resolve /{path} with 'ours', 'theirs' or {{\"content\": \"...\"}}")
    target = dict(ours)
    for path, e in take.items():
        if e is None:
            target.pop(path, None)
        else:
            target[path] = e
    msg = message or f"merge branch {branch['name']}"
    applied = _apply_target(ctx, p, parent, target, msg)
    with ctx.db.tx():
        ctx.db.exec("UPDATE workspaces SET merged_at=? WHERE id=?", now(), branch["id"])
        S.log(ctx.db, parent["owner_id"], parent["id"], p.actor, "merge", None, branch_id=branch["id"],
              **{k: len(v) for k, v in applied.items()}, resolved=len(resolve))
    return {"merged": branch["name"], "into": parent["name"], **applied, "auto_merged": sorted(auto), "resolved": resolve}
