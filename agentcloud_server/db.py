"""Metadata store (control plane). Organisations, users, projects, agents, credentials,
workspaces, grants, files, versions, uploads, proposals, share links, idempotency
records and the audit log. File contents never live here (see storage.py).

Tenancy: every tenant-owned row carries `owner_id`, which is the organisation id.
Permission checks compare it to the caller's organisation.

Runs on SQLite today; the SQL is deliberately plain so moving to Postgres is a driver
swap plus `json_extract` -> `->>` and `INSERT OR IGNORE` -> `ON CONFLICT DO NOTHING`.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS organisations (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free',
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS organisation_members (
  id TEXT PRIMARY KEY,
  organisation_id TEXT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
  created_at REAL NOT NULL,
  UNIQUE (organisation_id, user_id)
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  deleted_at REAL
);

-- Bearer credentials for humans: dashboard sessions and long-lived API keys.
CREATE TABLE IF NOT EXISTS user_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('session','api_key')),
  name TEXT,
  token_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  revoked_at REAL,
  last_used_at REAL
);

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,                -- organisation id
  project_id TEXT,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  quota_bytes INTEGER NOT NULL,
  retention_days INTEGER,                -- purge deleted files after N days; NULL = keep forever
  trust_policy TEXT,                     -- JSON {"untrusted": allow|approve|block, "protected": [paths]}
  created_at REAL NOT NULL,
  deleted_at REAL
);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,                -- organisation id
  project_id TEXT,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  disabled_at REAL,
  last_seen_at REAL,              -- last MCP session start: drives the "since you were last here" briefing
  prev_seen_at REAL,              -- the session start before that (default window for cloud_changes)
  home_workspace_id TEXT,         -- where bare paths go; set when provisioned, never changed by shares
  parent_agent_id TEXT,           -- set for sub-agents minted with cloud_delegate; killing a parent kills its subtree
  budget TEXT,                    -- JSON limits: max_writes, max_bytes, max_proposals
  used_writes INTEGER NOT NULL DEFAULT 0,
  used_bytes INTEGER NOT NULL DEFAULT 0,
  used_proposals INTEGER NOT NULL DEFAULT 0,
  ingests_untrusted INTEGER NOT NULL DEFAULT 0,  -- reads the open web etc.: everything it writes is untrusted
  last_active_at REAL             -- last MCP call (throttled): a long gap starts a new session
);

-- Agent credentials. `scopes` narrows what the credential may do even where grants allow
-- more (e.g. a read-only credential for an agent that can normally write).
CREATE TABLE IF NOT EXISTS agent_tokens (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  name TEXT,
  token_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  scopes TEXT NOT NULL DEFAULT '["read","write","delete","share"]',
  created_at REAL NOT NULL,
  expires_at REAL,
  revoked_at REAL,
  last_used_at REAL,
  rotated_to TEXT
);

-- What an agent may (or may not) do in a workspace.
-- path_prefix '' = whole workspace, 'reports/' = that folder, 'leads.csv' = that file.
-- effect 'deny' removes the listed rights under its prefix and beats any allow (spec §7.4).
-- role: set when the grant is a workspace membership (owner/admin/editor/viewer/restricted).
-- approval: 'none' | 'overwrite' (overwrites/deletes need a human) | 'all'.
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  path_prefix TEXT NOT NULL DEFAULT '',
  effect TEXT NOT NULL DEFAULT 'allow' CHECK (effect IN ('allow','deny')),
  role TEXT,
  can_read INTEGER NOT NULL DEFAULT 1,
  can_write INTEGER NOT NULL DEFAULT 0,
  can_delete INTEGER NOT NULL DEFAULT 0,
  can_share INTEGER NOT NULL DEFAULT 0,
  approval TEXT NOT NULL DEFAULT 'none' CHECK (approval IN ('none','overwrite','all')),
  note TEXT,                      -- message from whoever shared it, shown to the recipient agent
  created_by TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS grants_agent ON grants(agent_id, workspace_id);

-- A file or folder. `id` is its stable identity; `path` is where it is now. Renames and
-- moves update `path` in place. Only one live row per path; deleted rows keep their
-- history and can be restored.
CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'file' CHECK (kind IN ('file','folder')),
  current_version INTEGER NOT NULL DEFAULT 0,
  created_by TEXT,
  deleted_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS files_live_path ON files(workspace_id, path) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS files_ws_path ON files(workspace_id, path);
CREATE INDEX IF NOT EXISTS files_ws_updated ON files(workspace_id, updated_at);

CREATE TABLE IF NOT EXISTS versions (
  id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  n INTEGER NOT NULL,
  blob_sha256 TEXT,             -- NULL for a delete tombstone
  size INTEGER NOT NULL DEFAULT 0,
  content_type TEXT,
  op TEXT NOT NULL CHECK (op IN ('create','update','delete','restore','move','copy')),
  actor TEXT NOT NULL,          -- 'user:<id>' or 'agent:<id>'
  message TEXT NOT NULL DEFAULT '',
  proposal_id TEXT,
  created_at REAL NOT NULL,
  UNIQUE (file_id, n)
);

-- Data-plane upload sessions (spec §16.1): CREATED -> UPLOADED -> COMMITTED, or
-- FAILED / EXPIRED / ABORTED. Reserved bytes count against quota until resolved.
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  path TEXT NOT NULL,
  content_type TEXT,
  declared_size INTEGER NOT NULL,
  declared_sha256 TEXT,
  if_version INTEGER,
  message TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK (state IN ('CREATED','UPLOADED','COMMITTED','FAILED','EXPIRED','ABORTED')),
  staged_path TEXT,
  received_size INTEGER,
  received_sha256 TEXT,
  result TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_ws_state ON uploads(workspace_id, state);

CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('write','delete')),
  blob_sha256 TEXT,
  size INTEGER NOT NULL DEFAULT 0,
  content_type TEXT,
  base_version INTEGER NOT NULL,   -- version the agent saw; 0 = file did not exist
  actor TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','superseded')),
  decided_by TEXT,
  decided_at REAL,
  decision_note TEXT,
  provenance TEXT,                 -- provenance captured when proposed, applied on approval
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_ws ON proposals(workspace_id, status);

-- Human handoff: an expiring, revocable download link to one version of one file.
CREATE TABLE IF NOT EXISTS share_links (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  file_id TEXT,
  path TEXT NOT NULL,
  version INTEGER NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  revoked_at REAL,
  max_downloads INTEGER,
  downloads INTEGER NOT NULL DEFAULT 0,
  last_download_at REAL
);
CREATE INDEX IF NOT EXISTS share_links_ws ON share_links(workspace_id);

-- Idempotency keys (spec §16.4): a retried request with the same key replays the first
-- response instead of acting twice.
CREATE TABLE IF NOT EXISTS idempotency (
  scope TEXT NOT NULL,          -- hash of the caller's credential
  key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,    -- method + path (+ body hash for small bodies)
  status INTEGER,
  body BLOB,
  content_type TEXT,
  created_at REAL NOT NULL,
  PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id TEXT NOT NULL,
  workspace_id TEXT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  path TEXT,
  file_id TEXT,
  detail TEXT NOT NULL DEFAULT '{}',
  request_id TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_ws ON activity(workspace_id, id);
CREATE INDEX IF NOT EXISTS activity_owner ON activity(owner_id, id);
CREATE INDEX IF NOT EXISTS activity_file ON activity(file_id, id);
CREATE INDEX IF NOT EXISTS activity_actor ON activity(actor, id);

-- Provenance (one signed record per file version): who wrote it, under which task and
-- model, which file versions it had read, which outside sources it cited, and whether
-- the content is trusted. Trust propagates: content derived from untrusted input is
-- untrusted until a human endorses it.
CREATE TABLE IF NOT EXISTS provenance (
  version_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL,
  n INTEGER NOT NULL,
  owner_id TEXT NOT NULL,
  blob_sha256 TEXT,
  actor TEXT NOT NULL,
  credential_id TEXT,
  task_id TEXT,
  model TEXT,
  client TEXT,
  traceparent TEXT,
  sources TEXT NOT NULL DEFAULT '[]',     -- URLs / citations the content came from
  trust TEXT NOT NULL DEFAULT 'trusted' CHECK (trust IN ('trusted','untrusted','endorsed')),
  reasons TEXT NOT NULL DEFAULT '[]',     -- why it is untrusted
  decided_by TEXT,                        -- who endorsed or quarantined it
  sig TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS provenance_file ON provenance(file_id, n);
CREATE INDEX IF NOT EXISTS provenance_blob ON provenance(owner_id, blob_sha256);

-- The read set of each version: the file versions its writer had read (lineage edges).
CREATE TABLE IF NOT EXISTS provenance_inputs (
  version_id TEXT NOT NULL,
  input_file_id TEXT NOT NULL,
  input_n INTEGER NOT NULL,
  PRIMARY KEY (version_id, input_file_id, input_n)
);
CREATE INDEX IF NOT EXISTS provenance_inputs_in ON provenance_inputs(input_file_id, input_n);

-- Leases: an agent claims a file or folder for a while so others don't write over its
-- work. Each lease gets a fencing token that only ever increases, so a writer holding
-- an expired lease can be detected and refused.
CREATE TABLE IF NOT EXISTS leases (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  path TEXT NOT NULL,                     -- file path, or folder prefix ending in '/', '' = whole workspace
  holder TEXT NOT NULL,                   -- actor
  fence INTEGER NOT NULL,
  note TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  released_at REAL
);
CREATE INDEX IF NOT EXISTS leases_ws ON leases(workspace_id, released_at, expires_at);

-- One-time dashboard login links (printed by `agentcloud up` / `agentcloud login-link`).
CREATE TABLE IF NOT EXISTS magic_links (
  code_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  used_at REAL
);

-- Outbound webhooks: signed POSTs for audit events (e.g. approval.created), retried with backoff.
CREATE TABLE IF NOT EXISTS webhooks (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  url TEXT NOT NULL,
  secret TEXT NOT NULL,
  events TEXT NOT NULL DEFAULT '["*"]',
  description TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  disabled_at REAL
);
CREATE INDEX IF NOT EXISTS webhooks_owner ON webhooks(owner_id);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id TEXT PRIMARY KEY,
  webhook_id TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
  event TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','delivered','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL,
  response_code INTEGER,
  error TEXT,
  created_at REAL NOT NULL,
  delivered_at REAL
);
CREATE INDEX IF NOT EXISTS deliveries_due ON webhook_deliveries(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS blobs (
  owner_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (owner_id, sha256)
);
"""


def _statements(sql: str) -> list[str]:
    """Split a SQL script on ';' after removing -- comments (which may contain ';')."""
    import re
    return [st for st in re.sub(r"--[^\n]*", "", sql).split(";") if st.strip()]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def now() -> float:
    return time.time()


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Serialise writers: SQLite allows one writer anyway; this avoids busy errors
        # and makes read-check-write sequences (versioning, quotas) atomic.
        self.write_lock = threading.RLock()
        c = self.connect()
        existed = bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone())
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if existed and version < 2:
            self._migrate_v1_to_v2(c)
        c.executescript(SCHEMA)
        self._drop_stale_owner_fks(c)
        self._add_columns(c)
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            # WAL + NORMAL is crash-safe (a power cut can lose the last commit, never corrupt);
            # big page cache, memory-mapped reads, in-memory temp tables.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = conn
        return conn

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.connect().execute(sql, args).fetchone()

    def all(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.connect().execute(sql, args).fetchall()

    def exec(self, sql: str, *args) -> sqlite3.Cursor:
        return self.connect().execute(sql, args)

    @contextmanager
    def tx(self):
        """Serialised write transaction. Re-entrant: a nested call joins the outer one."""
        with self.write_lock:
            conn = self.connect()
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------ migrations

    @staticmethod
    def _cols(c, table):
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

    ADDED_COLUMNS = [("agents", "last_seen_at", "REAL"), ("agents", "prev_seen_at", "REAL"), ("grants", "note", "TEXT"),
                     ("agents", "home_workspace_id", "TEXT"),
                     # v0.5: delegation, budgets, trust, stateless sessions
                     ("agents", "parent_agent_id", "TEXT"), ("agents", "budget", "TEXT"),
                     ("agents", "used_writes", "INTEGER NOT NULL DEFAULT 0"),
                     ("agents", "used_bytes", "INTEGER NOT NULL DEFAULT 0"),
                     ("agents", "used_proposals", "INTEGER NOT NULL DEFAULT 0"),
                     ("agents", "ingests_untrusted", "INTEGER NOT NULL DEFAULT 0"),
                     ("agents", "last_active_at", "REAL"),
                     ("workspaces", "trust_policy", "TEXT"),
                     ("proposals", "provenance", "TEXT")]

    def _add_columns(self, c: sqlite3.Connection):
        """Additive, idempotent column migrations for existing databases."""
        for table, col, decl in self.ADDED_COLUMNS:
            if col not in self._cols(c, table):
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def _drop_stale_owner_fks(self, c: sqlite3.Connection):
        """v1 declared workspaces/agents.owner_id REFERENCES users(id). In v2 owner_id is an
        organisation id, so that constraint must go, or every new insert fails. Rebuild the
        table from the current schema definition (also repairs already-migrated databases)."""
        for table in ("workspaces", "agents"):
            if not any(r[2] == "users" for r in c.execute(f"PRAGMA foreign_key_list({table})")):
                continue
            ddl = next(stmt for stmt in _statements(SCHEMA) if f"CREATE TABLE IF NOT EXISTS {table} (" in stmt)
            ddl = ddl.replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {table}_rebuild (")
            c.execute("PRAGMA foreign_keys=OFF")
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(ddl)
                new_cols = self._cols(c, f"{table}_rebuild")
                cols = ",".join(col for col in self._cols(c, table) if col in new_cols)
                c.execute(f"INSERT INTO {table}_rebuild({cols}) SELECT {cols} FROM {table}")
                c.execute(f"DROP TABLE {table}")
                c.execute(f"ALTER TABLE {table}_rebuild RENAME TO {table}")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            finally:
                c.execute("PRAGMA foreign_keys=ON")

    def _migrate_v1_to_v2(self, c: sqlite3.Connection):
        """v1 (user-owned, path-identified files) -> v2 (organisations, projects, stable file ids)."""
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("BEGIN IMMEDIATE")
        try:
            adds = {
                "workspaces": [("project_id", "TEXT"), ("retention_days", "INTEGER")],
                "agents": [("project_id", "TEXT"), ("metadata", "TEXT NOT NULL DEFAULT '{}'")],
                "agent_tokens": [("scopes", "TEXT NOT NULL DEFAULT '[\"read\",\"write\",\"delete\",\"share\"]'"), ("rotated_to", "TEXT")],
                "grants": [("effect", "TEXT NOT NULL DEFAULT 'allow'"), ("role", "TEXT")],
                "activity": [("request_id", "TEXT"), ("file_id", "TEXT")],
                "share_links": [("file_id", "TEXT")],
            }
            for table, cols in adds.items():
                if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                have = self._cols(c, table)
                for name, decl in cols:
                    if name not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            # files: drop UNIQUE(workspace_id, path) so a deleted file and a new live file can share a path
            c.execute("""CREATE TABLE files_v2 (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                         path TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'file' CHECK (kind IN ('file','folder')),
                         current_version INTEGER NOT NULL DEFAULT 0, created_by TEXT, deleted_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            c.execute("""INSERT INTO files_v2(id,workspace_id,path,kind,current_version,deleted_at,created_at,updated_at)
                         SELECT id,workspace_id,path,kind,current_version,deleted_at,created_at,updated_at FROM files""")
            c.execute("DROP TABLE files")
            c.execute("ALTER TABLE files_v2 RENAME TO files")
            # versions: allow op='copy'
            c.execute("""CREATE TABLE versions_v2 (id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                         n INTEGER NOT NULL, blob_sha256 TEXT, size INTEGER NOT NULL DEFAULT 0, content_type TEXT,
                         op TEXT NOT NULL CHECK (op IN ('create','update','delete','restore','move','copy')), actor TEXT NOT NULL,
                         message TEXT NOT NULL DEFAULT '', proposal_id TEXT, created_at REAL NOT NULL, UNIQUE (file_id, n))""")
            c.execute("INSERT INTO versions_v2 SELECT id,file_id,n,blob_sha256,size,content_type,op,actor,message,proposal_id,created_at FROM versions")
            c.execute("DROP TABLE versions")
            c.execute("ALTER TABLE versions_v2 RENAME TO versions")
            # blobs become per-organisation
            c.execute("DROP TABLE IF EXISTS blobs")
            # one organisation (+ default project) per existing user; re-point ownership to it
            # (executescript would COMMIT our transaction, so run the CREATEs one by one)
            head = SCHEMA.split("CREATE TABLE IF NOT EXISTS user_tokens")[0]
            for stmt in _statements(head):
                if stmt.strip().startswith("CREATE TABLE IF NOT EXISTS") and "TABLE IF NOT EXISTS users " not in stmt:
                    c.execute(stmt)
            t = now()
            for u in c.execute("SELECT id, email FROM users").fetchall():
                org, proj = new_id("org"), new_id("prj")
                c.execute("INSERT INTO organisations(id,name,created_at) VALUES (?,?,?)", (org, u["email"].split("@")[0], t))
                c.execute("INSERT INTO organisation_members(id,organisation_id,user_id,role,created_at) VALUES (?,?,?,?,?)",
                          (new_id("mem"), org, u["id"], "owner", t))
                c.execute("INSERT INTO projects(id,owner_id,name,created_at) VALUES (?,?,?,?)", (proj, org, "Default", t))
                for table in ("workspaces", "agents"):
                    c.execute(f"UPDATE {table} SET project_id=?, owner_id=? WHERE owner_id=?", (proj, org, u["id"]))
                c.execute("UPDATE activity SET owner_id=? WHERE owner_id=?", (org, u["id"]))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.execute("PRAGMA foreign_keys=ON")
