"""A v1 database (user-owned rows, owner_id REFERENCES users, UNIQUE file paths) must
migrate to v2 and then accept *new* writes. Regression: reads worked after migration but
every insert hit a stale foreign key to users(id)."""
import sqlite3
import time

from agentcloud_server.auth import hash_password

V1 = """
CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE user_tokens (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('session','api_key')), name TEXT, token_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
  created_at REAL NOT NULL, expires_at REAL, revoked_at REAL, last_used_at REAL);
CREATE TABLE workspaces (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', quota_bytes INTEGER NOT NULL, created_at REAL NOT NULL, deleted_at REAL);
CREATE TABLE agents (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, disabled_at REAL);
CREATE TABLE agent_tokens (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE, name TEXT,
  token_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL, revoked_at REAL, last_used_at REAL);
CREATE TABLE grants (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE, path_prefix TEXT NOT NULL DEFAULT '',
  can_read INTEGER NOT NULL DEFAULT 1, can_write INTEGER NOT NULL DEFAULT 0, can_delete INTEGER NOT NULL DEFAULT 0,
  can_share INTEGER NOT NULL DEFAULT 0, approval TEXT NOT NULL DEFAULT 'none', created_by TEXT NOT NULL, created_at REAL NOT NULL,
  expires_at REAL, revoked_at REAL);
CREATE TABLE files (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE, path TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'file', current_version INTEGER NOT NULL DEFAULT 0, deleted_at REAL, created_at REAL NOT NULL,
  updated_at REAL NOT NULL, UNIQUE (workspace_id, path));
CREATE TABLE versions (id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE, n INTEGER NOT NULL,
  blob_sha256 TEXT, size INTEGER NOT NULL DEFAULT 0, content_type TEXT, op TEXT NOT NULL, actor TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '', proposal_id TEXT, created_at REAL NOT NULL, UNIQUE (file_id, n));
CREATE TABLE proposals (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, path TEXT NOT NULL, op TEXT NOT NULL, blob_sha256 TEXT,
  size INTEGER NOT NULL DEFAULT 0, content_type TEXT, base_version INTEGER NOT NULL, actor TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, decided_at REAL, decision_note TEXT, created_at REAL NOT NULL);
CREATE TABLE share_links (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, path TEXT NOT NULL, version INTEGER NOT NULL,
  token_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
  revoked_at REAL, max_downloads INTEGER, downloads INTEGER NOT NULL DEFAULT 0, last_download_at REAL);
CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, workspace_id TEXT, actor TEXT NOT NULL,
  action TEXT NOT NULL, path TEXT, detail TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
CREATE TABLE blobs (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, created_at REAL NOT NULL);
"""


def test_v1_database_migrates_and_accepts_new_writes(tmp_path):
    from conftest import Server
    data = tmp_path / "d"
    data.mkdir()
    t = time.time()
    c = sqlite3.connect(data / "agentcloud.db")
    c.executescript(V1)
    c.execute("INSERT INTO users VALUES ('usr_1','old@example.com',?,?)", (hash_password("old-password-1"), t))
    c.execute("INSERT INTO workspaces VALUES ('ws_1','usr_1','legacy','',1000000,?,NULL)", (t,))
    c.execute("INSERT INTO agents VALUES ('agt_1','usr_1','legacy-agent','',?,NULL)", (t,))
    c.execute("INSERT INTO files VALUES ('f_1','ws_1','notes.md','file',1,NULL,?,?)", (t, t))
    c.execute("INSERT INTO versions VALUES ('v_1','f_1',1,NULL,0,'text/markdown','create','agent:agt_1','',NULL,?)", (t,))
    c.commit()
    c.close()

    s = Server(data)
    tok = s.client().http.post("/v1/auth/login", json={"email": "old@example.com", "password": "old-password-1"}).json()["token"]
    dev = s.client(tok)
    assert dev.me()["organisation"]["role"] == "owner"
    assert [a["name"] for a in dev.agents()] == ["legacy-agent"]
    assert dev.workspace("ws_1").versions("notes.md")[0]["actor"] == "agent:agt_1"
    # the part that used to fail: new rows
    out = dev.provision_agent("new-agent", access="full")
    ws = dev.create_workspace("fresh")
    ws.write("a.txt", "hello")
    ws.delete("a.txt")
    ws.write("a.txt", "again")                         # a live and a deleted row share a path (old UNIQUE is gone)
    assert ws.read_text("a.txt") == "again"
    s.client(out["token"]["token"]).workspace(out["workspace"]["id"]).write("x.md", "agent write")
    s.restart()                                        # reopening a migrated DB is a no-op
    assert ws.read_text("a.txt") == "again"
