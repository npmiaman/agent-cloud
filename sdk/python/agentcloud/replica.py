"""A local replica of a workspace, for microsecond reads and search on the agent's own machine.

    from agentcloud.replica import LocalReplica

    r = LocalReplica(workspace="wsp_…", token="aca_…", base_url="http://127.0.0.1:8787")
    r.sync()                          # first call copies everything you can read; later calls fetch only changes
    r.read_text("/notes/plan.md")     # microseconds: local SQLite (memory-mapped) plus an in-memory cache
    r.search("churn analysis")        # local full-text search, with the matching line
    r.write("/notes/plan.md", "...")  # writes go to the server (versioned, audited) and update the replica

How it stays correct:
- `sync()` pulls an incremental change feed keyed by the workspace's sequence number. Moves
  and deletes are applied, only changed content is downloaded, and files you lose access to
  are removed (a change in your grants triggers a full resync).
- Reads are as fresh as the last `sync()`. Call it whenever freshness matters.
- Every local read is remembered and declared as an input on your next write, so the server's
  trust tracking still sees that your new content was derived from what you read. `trust(path)`
  tells you whether a file is labelled untrusted.

The replica stores plaintext in a local SQLite file (created with mode 0600). It's a cache of
data your credential can read anyway; treat the file like the credential.
"""
from __future__ import annotations

import base64
import collections
import os
import re
import sqlite3
from pathlib import Path

import httpx

_TERMS = re.compile(r"\w+")


class LocalReplica:
    def __init__(self, workspace: str, token: str | None = None, base_url: str | None = None, *,
                 path: str | os.PathLike | None = None, http: httpx.Client | None = None, cache_bytes: int = 256 * 1024 * 1024):
        token = token or os.environ.get("AGENTCLOUD_TOKEN")
        base_url = (base_url or os.environ.get("AGENTCLOUD_URL") or "http://127.0.0.1:8787").rstrip("/")
        self.http = http or httpx.Client(base_url=base_url, timeout=120)
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.workspace = workspace
        path = Path(path or Path.home() / ".agentcloud" / "replicas" / f"{workspace}.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if new:
            os.chmod(path, 0o600)
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "mmap_size=1073741824", "temp_store=MEMORY", "cache_size=-131072"):
            self.db.execute(f"PRAGMA {pragma}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS files (
              rid INTEGER PRIMARY KEY, file_id TEXT UNIQUE NOT NULL, path TEXT UNIQUE NOT NULL, version INTEGER NOT NULL,
              sha256 TEXT NOT NULL, size INTEGER NOT NULL, content_type TEXT, trust TEXT, content BLOB NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(body, tokenize='unicode61');
        """)
        self._cache: collections.OrderedDict[str, tuple[bytes, str, int]] = collections.OrderedDict()
        self._cache_size, self._cache_max = 0, cache_bytes
        self._reads: collections.deque[tuple[str, int]] = collections.deque(maxlen=200)
        self._get = "SELECT content, file_id, version FROM files WHERE path=?"

    # ---------------------------------------------------------------- reads

    @staticmethod
    def _key(path: str) -> str:
        return path.lstrip("/")

    def read(self, path: str) -> bytes | None:
        key = path.lstrip("/")
        hit = self._cache.get(key)
        if hit is None:
            row = self.db.execute(self._get, (key,)).fetchone()
            if row is None:
                return None
            hit = (row[0], row[1], row[2])
            self._remember(key, hit)
        self._reads.append((hit[1], hit[2]))
        return hit[0]

    def read_text(self, path: str, encoding: str = "utf-8") -> str | None:
        data = self.read(path)
        return None if data is None else data.decode(encoding)

    def trust(self, path: str) -> str | None:
        row = self.db.execute("SELECT trust FROM files WHERE path=?", (self._key(path),)).fetchone()
        return row[0] if row else None

    def list(self, prefix: str = "/") -> list[str]:
        p = self._key(prefix)
        rows = self.db.execute("SELECT path FROM files WHERE path >= ? AND path < ? ORDER BY path", (p, p + "\U0010ffff"))
        return ["/" + r[0] for r in rows]

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Local full-text search (BM25). Each hit has the best-matching line."""
        terms = list(dict.fromkeys(t for t in _TERMS.findall(query.lower())))
        if not terms:
            return []
        need = max(1, -(-len(terms) * 6 // 10))
        match = " OR ".join(f'"{t}"' for t in terms)
        out = []
        for rid, score in self.db.execute("SELECT rowid, bm25(fts) FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                                          (match, limit * 5)):
            row = self.db.execute("SELECT path, content, trust FROM files WHERE rid=?", (rid,)).fetchone()
            if row is None:
                continue
            text = row[1].decode("utf-8", errors="replace")
            low = text.lower()
            if sum(1 for t in terms if t in low) < need:
                continue
            best_line, best_n, best_text = 1, -1, ""
            for i, line in enumerate(text.splitlines(), 1):
                ll = line.lower()
                n = sum(1 for t in terms if t in ll)
                if n > best_n:
                    best_line, best_n, best_text = i, n, line
                    if n == len(terms):
                        break
            out.append({"path": "/" + row[0], "line": best_line, "snippet": best_text.strip()[:200],
                        "score": round(-score, 3), "trust": row[2]})
            if len(out) >= limit:
                break
        return out

    def _remember(self, key: str, hit: tuple[bytes, str, int]) -> None:
        if len(hit[0]) > self._cache_max // 16:
            return
        self._cache[key] = hit
        self._cache_size += len(hit[0])
        while self._cache_size > self._cache_max and self._cache:
            _, old = self._cache.popitem(last=False)
            self._cache_size -= len(old[0])

    def _forget(self, key: str) -> None:
        old = self._cache.pop(key, None)
        if old:
            self._cache_size -= len(old[0])

    # ---------------------------------------------------------------- sync

    def _meta(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def _set_meta(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?,?)", (k, str(v)))

    def _get_json(self, url: str, **params) -> dict:
        r = self.http.get(url, params=params, headers=self.headers)
        r.raise_for_status()
        return r.json()

    def _delete_local(self, row) -> None:
        rid, path = row
        self.db.execute("DELETE FROM files WHERE rid=?", (rid,))
        self.db.execute("DELETE FROM fts WHERE rowid=?", (rid,))
        self._forget(path)

    def _store(self, e: dict, content: bytes) -> None:
        clash = self.db.execute("SELECT rid, path FROM files WHERE path=? AND file_id != ?", (e["path"], e["file_id"])).fetchone()
        if clash:
            self._delete_local(clash)
        old = self.db.execute("SELECT rid, path FROM files WHERE file_id=?", (e["file_id"],)).fetchone()
        if old:
            self._delete_local(old)
        cur = self.db.execute("""INSERT INTO files(file_id, path, version, sha256, size, content_type, trust, content)
                                 VALUES (?,?,?,?,?,?,?,?)""", (e["file_id"], e["path"], e["version"], e["sha256"], e["size"],
                                                               e.get("content_type"), e.get("trust"), content))
        try:
            body = content.decode("utf-8") if b"\x00" not in content[:8192] else ""
        except UnicodeDecodeError:
            body = ""
        self.db.execute("INSERT INTO fts(rowid, body) VALUES (?,?)", (cur.lastrowid, body + "\n" + e["path"]))

    def sync(self) -> dict:
        """Bring the replica up to date. Returns counts of fetched, deleted and moved files."""
        ws = self.workspace
        stats = {"fetched": 0, "deleted": 0, "moved": 0}
        since = int(self._meta("seq", 0))
        gv = self._meta("gv")
        resp = self._get_json(f"/v1/workspaces/{ws}/sync", since=since, **({"gv": gv} if gv else {}))
        to_fetch: list[dict] = []
        self.db.execute("BEGIN")
        try:
            if resp["full"]:
                seq, seen = resp["seq"], set()
                while True:
                    for e in resp["changes"]:
                        seen.add(e["file_id"])
                        self._apply(e, to_fetch, stats)
                    if not resp.get("next_cursor"):
                        break
                    resp = self._get_json(f"/v1/workspaces/{ws}/sync", since=0, cursor=resp["next_cursor"])
                for rid, fid, path in self.db.execute("SELECT rid, file_id, path FROM files").fetchall():
                    if fid not in seen:
                        self._delete_local((rid, path))
                        stats["deleted"] += 1
            else:
                while True:
                    for e in resp["changes"]:
                        self._apply(e, to_fetch, stats)
                    seq = resp["seq"]
                    if not resp.get("more"):
                        break
                    resp = self._get_json(f"/v1/workspaces/{ws}/sync", since=seq, gv=resp["grants_version"])
            for i in range(0, len(to_fetch), 500):
                batch = {e["file_id"]: e for e in to_fetch[i:i + 500]}
                pending = [[fid, e["version"]] for fid, e in batch.items()]
                while pending:
                    r = self.http.post(f"/v1/workspaces/{ws}/sync/content", json={"files": pending}, headers=self.headers)
                    r.raise_for_status()
                    body = r.json()
                    for item in body["files"]:
                        if "content_b64" in item:
                            self._store(batch[item["file_id"]], base64.b64decode(item["content_b64"]))
                            stats["fetched"] += 1
                    pending = body["remaining"]
            self._set_meta("seq", seq)
            self._set_meta("gv", resp["grants_version"])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        stats["seq"] = seq
        return stats

    def _apply(self, e: dict, to_fetch: list, stats: dict) -> None:
        row = self.db.execute("SELECT rid, path, version, sha256 FROM files WHERE file_id=?", (e["file_id"],)).fetchone()
        if e.get("deleted"):
            if row:
                self._delete_local((row[0], row[1]))
                stats["deleted"] += 1
            return
        if row and row[2] == e["version"] and row[3] == e["sha256"]:
            if row[1] != e["path"]:
                clash = self.db.execute("SELECT rid, path FROM files WHERE path=? AND rid != ?", (e["path"], row[0])).fetchone()
                if clash:
                    self._delete_local(clash)
                self.db.execute("UPDATE files SET path=?, trust=? WHERE rid=?", (e["path"], e.get("trust"), row[0]))
                self._forget(row[1])
                stats["moved"] += 1
            elif e.get("trust") is not None:
                self.db.execute("UPDATE files SET trust=? WHERE rid=?", (e["trust"], row[0]))
            return
        to_fetch.append(e)

    # ---------------------------------------------------------------- writes

    def write(self, path: str, data: bytes | str, *, message: str = "", content_type: str | None = None) -> dict:
        """Write through to the server (versioned, audited, permission-checked), then update the
        replica. Recent local reads are declared as inputs so trust labels propagate."""
        body = data.encode("utf-8") if isinstance(data, str) else data
        key = self._key(path)
        headers = dict(self.headers)
        if self._reads:
            headers["X-Agent-Inputs"] = ",".join(f"{fid}:{n}" for fid, n in dict.fromkeys(self._reads))
        if message:
            headers["X-Message"] = message
        if content_type:
            headers["Content-Type"] = content_type
        r = self.http.put(f"/v1/workspaces/{self.workspace}/files/{key}", content=body, headers=headers)
        r.raise_for_status()
        res = r.json()
        if res.get("status") != "pending_approval" and res.get("id"):
            self.db.execute("BEGIN")
            try:
                self._store({"file_id": res["id"], "path": key, "version": res["version"], "sha256": res["sha256"],
                             "size": res["size"], "content_type": res.get("content_type"), "trust": res.get("trust")}, body)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self._forget(key)
        return res

    def close(self) -> None:
        self.db.close()
