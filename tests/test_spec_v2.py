"""Technical spec v2: the §22 MVP scenario, data plane, identity, permissions, API standards."""
import hashlib
import time

import pytest

from agentcloud import AgentCloudError, Conflict, Forbidden, NotFound, Pending, Unauthenticated


def agent_client(server, credential):
    """An agent SDK client that exchanges its credential for short-lived access tokens."""
    return server.client(credential).__class__.from_client(server.client().http, credential, exchange=True)


# ---------------------------------------------------------------- §22: the 24 steps

def test_section_22_mvp_scenario(server):
    # Setup
    dev = server.signup("founder@example.com")                                        # 1 account
    project = dev.create_project("Market research")                                   # 2 project
    research = dev.agents.create("research-agent", project_id=project["id"])          # 3 agent
    ws = dev.workspaces.create("Research Workspace", project_id=project["id"],
                               storage_limit_bytes=1024 * 1024 * 1024)                # 4 workspace
    ws.set_member(research["id"], "editor")                                           # 5 assign
    cred = dev.agents.create_credential(research["id"])["token"]                       # 6 credential

    # Agent operation
    agent = agent_client(server, cred)                                                # 7 authenticates
    assert agent.me()["kind"] == "agent" and agent.exchanges == 1
    aws = agent.workspaces.get(ws.id)
    csv1 = "company,arr_musd,owner\nAcme,12,ada\nGlobex,48,alan\n"
    up = aws.files.upload("research/companies.csv", data=csv1, content_type="text/csv")  # 8 upload (verified session)
    aws.mkdir("reports")                                                              # 9 folder
    assert aws.files.read(up.id).decode() == csv1                                     # 10 retrieve
    up2 = aws.files.upload("research/companies.csv", data=csv1 + "Initech,130,grace\n")  # 11 modify
    assert up2.id == up.id and up2.version == 2                                       # 12 new version, same file

    # Persistence
    del agent, aws
    server.restart()                                                                  # 13-14 runtime gone, back
    agent = agent_client(server, cred)                                                # 15 authenticates again
    aws = agent.workspaces.get(ws.id)
    assert "Initech" in aws.files.read(up.id).decode()                                # 16 same CSV ...
    assert [v["n"] for v in aws.files.versions(up.id)] == [2, 1]                      #    ... and its history

    # Recovery
    aws.files.upload("research/companies.csv", data="company\nAcme\n", message="cleanup")  # 17 bad edit
    hist = dev.workspace(ws.id).files.versions(up.id)                                 # 18-19 developer inspects
    assert hist[0]["message"] == "cleanup" and hist[0]["actor"] == f"agent:{research['id']}"
    assert dev.workspace(ws.id).files.restore(up.id, version_id=hist[1]["id"])["op"] == "restore"  # 20 restore
    assert "Initech" in aws.files.read(up.id).decode()
    assert len(aws.files.versions(up.id)) == 4                                        # nothing erased

    # Isolation
    second = dev.agents.create("analysis-agent", project_id=project["id"])
    second_cred = dev.agents.create_credential(second["id"])["token"]
    other = agent_client(server, second_cred)
    with pytest.raises(NotFound):                                                     # 21-22 denied
        other.workspaces.get(ws.id).files.read(up.id)
    dev.workspace(ws.id).files.share(up.id, second["id"], "read")                     # 23 grant read
    assert "Initech" in other.workspaces.get(ws.id).files.read(up.id).decode()        # 24 retrieves


# ---------------------------------------------------------------- data plane

def _start(ws, path, data, **kw):
    return ws.c._json("POST", f"/v1/workspaces/{ws.id}/uploads", json={
        "path": path, "size_bytes": len(data), "checksum_sha256": hashlib.sha256(data).hexdigest(), **kw})


def test_upload_session_state_machine_and_integrity(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    data = b"x" * 5000
    s = _start(ws, "a.bin", data)
    assert s["state"] == "CREATED" and "sig=" in s["upload_url"]
    anon = server.client().http
    # completing before uploading is refused
    with pytest.raises(Conflict):
        dev._json("POST", f"/v1/uploads/{s['upload_id']}/complete", json={})
    # tampered signature / wrong size are refused; nothing is committed
    assert anon.put(s["upload_url"].replace("sig=", "sig=0"), content=data).status_code == 403
    assert anon.put(s["upload_url"], content=data[:-1]).status_code == 400
    assert anon.put(s["upload_url"], content=data + b"!").status_code == 413
    r = anon.put(s["upload_url"], content=data)                     # the signed URL needs no bearer token
    assert r.status_code == 200 and r.json()["state"] == "UPLOADED"
    assert not ws.exists("a.bin")                                   # still not visible
    done = dev._json("POST", f"/v1/uploads/{s['upload_id']}/complete", json={})
    assert done["state"] == "COMMITTED" and done["result"]["version"] == 1
    again = dev._json("POST", f"/v1/uploads/{s['upload_id']}/complete", json={})   # idempotent
    assert again["result"] == done["result"] and len(ws.versions("a.bin")) == 1
    assert ws.read("a.bin") == data


def test_upload_checksum_mismatch_fails_safely(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    data = b"real bytes"
    s = ws.c._json("POST", f"/v1/workspaces/{ws.id}/uploads", json={"path": "f", "size_bytes": len(data)})
    server.client().http.put(s["upload_url"], content=data)
    with pytest.raises(AgentCloudError) as e:
        dev._json("POST", f"/v1/uploads/{s['upload_id']}/complete", json={"checksum_sha256": "0" * 64})
    assert e.value.code == "CHECKSUM_MISMATCH"
    assert dev._json("GET", f"/v1/uploads/{s['upload_id']}")["state"] == "FAILED"
    assert not ws.exists("f")


def test_quota_reservation_blocks_concurrent_overcommit(server):
    dev = server.signup()
    ws = dev.create_workspace("small", quota_bytes=10_000)
    a = _start(ws, "a.bin", b"a" * 6000)                            # reserves 6000
    with pytest.raises(AgentCloudError) as e:
        _start(ws, "b.bin", b"b" * 6000)                            # would exceed with the reservation
    assert e.value.code == "QUOTA_EXCEEDED"
    dev._json("POST", f"/v1/uploads/{a['upload_id']}/abort")        # releases it
    assert _start(ws, "b.bin", b"b" * 6000)["state"] == "CREATED"


def test_large_file_streams_through_sessions(tmp_path):
    from conftest import Server
    s = Server(tmp_path / "d", max_upload_bytes=1024 * 1024)        # simple PUT capped at 1 MiB
    dev = s.signup()
    ws = dev.create_workspace("w")
    big = tmp_path / "big.bin"
    big.write_bytes(bytes(range(256)) * (5 * 4096 + 7))            # ~5.2 MiB, not chunk-aligned
    with pytest.raises(AgentCloudError) as e:
        ws.write("big.bin", big.read_bytes())
    assert e.value.status == 413
    ref = ws.files.upload("big.bin", big)                          # session path takes it, streamed
    out = ws.files.download(ref.id, tmp_path / "back.bin")         # signed URL, checksum verified
    assert out.read_bytes() == big.read_bytes()


def test_signed_download_urls(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    ws.write("r.txt", "secret report")
    fid = ws.stat("r.txt")["id"]
    d = dev._json("GET", f"/v1/files/{fid}/download")
    anon = server.client().http
    assert anon.get(d["url"]).content == b"secret report"
    assert anon.get(d["url"].replace("sig=", "sig=f")).status_code == 403
    exp_url = d["url"].replace(f"exp={d['expires_at']}", f"exp={d['expires_at'] - 10_000}")
    assert anon.get(exp_url).status_code == 403
    r = dev.http.get(f"/v1/files/{fid}/download?redirect=true", follow_redirects=False)
    assert r.status_code == 307 and "/v1/data/files/" in r.headers["location"]


def test_blob_truncation_is_detected(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    data = bytes(range(256)) * 9000                                 # > 2 chunks
    ws.write("t.bin", data)
    blob = next(p for p in (server.data_dir / "blobs").rglob("*") if p.is_file() and len(p.name) == 64)
    raw = blob.read_bytes()
    blob.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(Exception):
        server.app.state.ctx.blobs.open(dev.me()["organisation"]["id"], blob.name)


# ---------------------------------------------------------------- identity & permissions

def test_stable_ids_and_grants_follow_moves(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    ag = dev.agents.create("reader")
    tok = dev.agents.create_credential(ag["id"])["token"]
    ws.write("drafts/plan.md", "v1")
    fid = ws.stat("drafts/plan.md")["id"]
    ws.files.share(fid, ag["id"], "read")
    link = ws.link("drafts/plan.md")
    ws.files.rename(fid, "final-plan.md")
    ws.files.move(fid, "published/final-plan.md")
    assert ws.files.get(fid)["path"] == "published/final-plan.md"
    agent = server.client(tok).workspace(ws.id)
    assert agent.read_text("published/final-plan.md") == "v1"      # grant followed the file
    assert server.client().http.get(link["url_path"]).content == b"v1"  # so did the share link
    folder_id = ws.stat("published/")["id"]
    assert dev._json("GET", f"/v1/folders/{folder_id}")["entries"][0]["id"] == fid


def test_deny_rules_beat_broader_allows(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    ag = dev.agents.create("researcher")
    tok = dev.agents.create_credential(ag["id"])["token"]
    ws.write("research/public/notes.md", "ok")
    ws.write("research/confidential/deal.md", "secret")
    ws.grant(ag["id"], "research/", read=True, write=True)
    ws.deny(ag["id"], "research/confidential/")
    A = server.client(tok).workspace(ws.id)
    assert A.read_text("research/public/notes.md") == "ok"
    with pytest.raises(Forbidden):
        A.read("research/confidential/deal.md")
    with pytest.raises(Forbidden):
        A.write("research/confidential/new.md", "x")
    assert not any("confidential" in e["path"] for e in A.list(recursive=True))
    assert not any("deal" in r["path"] for r in server.client(tok).search("secret"))
    with pytest.raises(Forbidden):  # agents can't write deny rules
        A.c._json("POST", f"/v1/workspaces/{ws.id}/grants", json={"agent_id": ag["id"], "path": "x/", "effect": "deny"})


def test_workspace_roles(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    ag = dev.agents.create("a")
    A = server.client(dev.agents.create_credential(ag["id"])["token"]).workspace(ws.id)
    ws.write("f.txt", "x")
    ws.set_member(ag["id"], "viewer")
    assert A.read_text("f.txt") == "x"
    with pytest.raises(Forbidden):
        A.write("f.txt", "y")
    ws.set_member(ag["id"], "editor")                               # replaces, doesn't stack
    A.write("f.txt", "y")
    with pytest.raises(Forbidden):
        A.delete("f.txt")
    assert [m["role"] for m in ws.members()] == ["editor"]
    ws.remove_member(ag["id"])
    with pytest.raises(NotFound):
        A.read("f.txt")


def test_credential_scopes_narrow_grants(server):
    dev = server.signup()
    out = dev.provision_agent("writer", access="full")
    ro = dev.agents.create_credential(out["agent"]["id"], scopes=["read"])["token"]
    W = server.client(ro).workspace(out["workspace"]["id"])
    server.client(out["token"]["token"]).workspace(out["workspace"]["id"]).write("a.txt", "x")
    assert W.read_text("a.txt") == "x"
    with pytest.raises(Forbidden):
        W.write("a.txt", "y")
    tools = server.client().http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                      headers={"Authorization": f"Bearer {ro}"}).json()["result"]["tools"]
    assert "cloud_write" not in [t["name"] for t in tools]


def test_access_token_exchange_and_revocation(server):
    dev = server.signup()
    out = dev.provision_agent("x")
    cred = out["token"]["token"]
    http = server.client().http
    tok = http.post("/v1/auth/agent/token", headers={"Authorization": f"Bearer {cred}"}).json()
    assert tok["access_token"].startswith("acx_") and tok["expires_in"] == 900
    me = lambda t: http.get("/v1/me", headers={"Authorization": f"Bearer {t}"})
    assert me(tok["access_token"]).json()["id"] == out["agent"]["id"]
    assert me(tok["access_token"][:-2] + "xx").status_code == 401   # tampered
    dev.agents.revoke_credential(out["agent"]["id"], out["token"]["id"])
    assert me(tok["access_token"]).status_code == 401               # revocation is immediate
    assert http.post("/v1/auth/agent/token", headers={"Authorization": f"Bearer {dev.token}"}).status_code == 401


def test_sdk_refreshes_expired_access_tokens(tmp_path):
    from conftest import Server
    s = Server(tmp_path / "d", access_token_ttl=1)
    dev = s.signup()
    out = dev.provision_agent("x")
    agent = agent_client(s, out["token"]["token"])
    agent.workspaces.get(out["workspace"]["id"])
    agent._access = (agent._access[0], time.time() + 3600)          # SDK thinks it's still fresh...
    time.sleep(1.2)                                                 # ...but the server says expired
    assert agent.workspaces.get(out["workspace"]["id"]).id == out["workspace"]["id"]
    assert agent.exchanges == 2                                     # refreshed once on 401, transparently


def test_credential_rotation_with_grace(server):
    dev = server.signup()
    out = dev.provision_agent("x")
    old, aid = out["token"], out["agent"]["id"]
    new = dev.agents.rotate_credential(aid, old["id"], grace_seconds=1)
    assert new["replaces"] == old["id"]
    http = server.client().http
    ok = lambda t: http.get("/v1/me", headers={"Authorization": f"Bearer {t}"}).status_code == 200
    assert ok(old["token"]) and ok(new["token"])
    time.sleep(1.2)
    assert not ok(old["token"]) and ok(new["token"])
    hard = dev.agents.rotate_credential(aid, new["id"], grace_seconds=0)
    assert not ok(new["token"]) and ok(hard["token"])


def test_organisations_members_and_isolation(server):
    alice = server.signup("alice@example.com")
    bob = server.signup("bob@example.com")
    carol = server.signup("carol@example.com")
    ws = alice.create_workspace("team")
    ws.write("plan.md", "alice's plan")
    with pytest.raises(NotFound):
        bob.workspace(ws.id).read("plan.md")
    alice.add_org_member("bob@example.com", "member")
    orgs = bob.me()["organisations"]
    alice_org = alice.me()["organisation"]["id"]
    assert {o["id"] for o in orgs} >= {alice_org}
    bob_in_alice = server.client(bob.token).__class__.from_client(server.client().http, bob.token)
    bob_in_alice.http.headers["X-Organisation"] = alice_org
    assert bob_in_alice.workspace(ws.id).read_text("plan.md") == "alice's plan"
    with pytest.raises(Forbidden):                                  # members can't add members
        bob_in_alice.add_org_member("carol@example.com")
    with pytest.raises(Conflict):                                   # can't remove the last owner
        alice._json("DELETE", f"/v1/organisation/members/{alice.me()['id']}")
    with pytest.raises(NotFound):
        carol.workspace(ws.id).read("plan.md")


def test_projects(server):
    dev = server.signup()
    p = dev.create_project("Sales")
    ws = dev.workspaces.create("pipeline", project_id=p["id"])
    dev.workspaces.create("elsewhere")
    assert [w.id for w in dev.workspaces.list(project_id=p["id"])] == [ws.id]
    with pytest.raises(Conflict):
        dev._json("DELETE", f"/v1/projects/{p['id']}")
    with pytest.raises(NotFound):
        dev.workspaces.create("bad", project_id="prj_nope")


# ---------------------------------------------------------------- API standards

def test_error_format_and_request_ids(server):
    dev = server.signup()
    r = dev.http.get("/v1/workspaces/wsp_missing")
    body = r.json()["error"]
    assert r.status_code == 404 and body["code"] == "WORKSPACE_NOT_FOUND" and body["request_id"].startswith("req_")
    assert r.headers["x-request-id"] == body["request_id"]
    ws = dev.create_workspace("w")
    r = dev.http.put(f"/v1/workspaces/{ws.id}/files/a.txt", content=b"x", headers={"X-Request-ID": "req_trace123"})
    assert r.headers["x-request-id"] == "req_trace123"
    assert any(a["request_id"] == "req_trace123" and a["event"] == "file.created" for a in ws.activity())
    assert dev.http.post("/v1/workspaces", json={}).json()["error"]["code"] == "INVALID_REQUEST"
    assert server.client().http.get("/v1/me").json()["error"]["code"] == "UNAUTHENTICATED"


def test_idempotency_keys(server):
    dev = server.signup()
    h = {"Idempotency-Key": "create-ws-1"}
    a = dev.http.post("/v1/workspaces", json={"name": "once"}, headers=h)
    b = dev.http.post("/v1/workspaces", json={"name": "once"}, headers=h)
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert b.headers.get("idempotent-replayed") == "true"
    assert [w.info["name"] for w in dev.workspaces()].count("once") == 1
    c = dev.http.post("/v1/workspaces", json={"name": "different"}, headers=h)
    assert c.status_code == 422 and c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_rate_limiting(tmp_path):
    from conftest import Server
    s = Server(tmp_path / "d", rate_limit_per_minute=5)
    dev = s.signup()
    codes = [dev.http.get("/v1/me").status_code for _ in range(8)]
    assert 429 in codes and codes[0] == 200
    r = dev.http.get("/v1/me")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["error"]["code"] == "RATE_LIMITED"


def test_pagination_search_copy_and_explicit_proposals(server):
    dev = server.signup()
    ag = dev.provision_agent("a", access="read_write")
    ws = dev.workspace(ag["workspace"]["id"])
    for i in range(7):
        ws.write(f"docs/{i:02d}.md", f"doc {i}")
    ws.write("data/x.csv", "a,b\n")
    page = dev._json("GET", f"/v1/workspaces/{ws.id}/files", params={"path": "docs/", "limit": 3})
    assert len(page["entries"]) == 3 and page["next_cursor"]
    assert len([e for e in ws.files.list("docs/") if e["kind"] == "file"]) == 7      # SDK follows cursors
    assert [e["path"] for e in ws.files.list(ext="csv")] == ["data/x.csv"]
    by_agent = [e for e in ws.files.list(created_by=f"agent:{ag['agent']['id']}")]
    assert by_agent == []
    fid = ws.stat("docs/00.md")["id"]
    copy = ws.files.copy(fid, "archive/00.md")
    assert copy["id"] != fid and ws.read_text("archive/00.md") == "doc 0"
    A = server.client(ag["token"]["token"]).workspace(ws.id)
    prop = A.files.propose(fid, "proposed doc 0", message="please review")
    assert prop["status"] == "pending_approval" and ws.read_text("docs/00.md") == "doc 0"
    dev.approve(prop["proposal_id"])
    assert ws.read_text("docs/00.md") == "proposed doc 0"
    r = dev.http.get("/v1/activity", params={"limit": 2})
    assert len(r.json()) == 2 and r.headers.get("x-next-cursor")


def test_worker_expires_uploads_gcs_orphans_and_applies_retention(server):
    from agentcloud_server import worker
    ctx = server.app.state.ctx
    dev = server.signup()
    org = dev.me()["organisation"]["id"]
    ws = dev.create_workspace("w", retention_days=1)
    # expired upload frees its reservation
    s = _start(ws, "big.bin", b"z" * 1000)
    ctx.db.exec("UPDATE uploads SET expires_at=? WHERE id=?", time.time() - 1, s["upload_id"])
    # orphan blob: stored but never committed (e.g. DB failed after the object landed)
    orphan, _ = ctx.blobs.put_bytes(org, b"orphaned bytes")
    # a long-deleted file
    ws.write("old.txt", "bye")
    ws.delete("old.txt")
    ctx.db.exec("UPDATE files SET deleted_at=? WHERE path='old.txt'", time.time() - 3 * 86400)
    ws.write("keep.txt", "keep me")
    stats = worker.run_once(ctx, blob_grace=0)
    assert stats["uploads_expired"] == 1 and stats["files_purged"] == 1 and stats["blobs_deleted"] >= 1
    assert not ctx.blobs.exists(org, orphan)
    assert dev._json("GET", f"/v1/uploads/{s['upload_id']}")["state"] == "EXPIRED"
    assert ws.read_text("keep.txt") == "keep me"                    # live data untouched
    assert not any(e["path"] == "old.txt" for e in ws.list(include_deleted=True))
    assert worker.run_once(ctx, blob_grace=0)["blobs_deleted"] == 0  # idempotent


def test_recent_deleted_and_storage_views(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    ws.write("a.txt", "a"); ws.write("b.txt", "bb")
    ws.delete("a.txt")
    assert [r["path"] for r in dev._json("GET", "/v1/recent-files")][0] == "b.txt"
    assert [r["path"] for r in dev._json("GET", "/v1/deleted-files")] == ["a.txt"]
    st = dev.storage()
    assert st["workspaces"][0]["deleted_retained_bytes"] == 1 and st["total_used_bytes"] == 3


def test_idempotency_large_bodies_and_token_refresh(server):
    """Regression (found by the TS SDK e2e): >1 MB bodies were fingerprinted by length only,
    and keys were scoped to the raw token instead of the caller."""
    dev = server.signup()
    out = dev.provision_agent("w", access="full")
    ws_id, cred = out["workspace"]["id"], out["token"]["token"]
    http = server.client().http
    url = f"/v1/workspaces/{ws_id}/files/big.bin"
    a, b = b"a" * 1_100_000, b"b" * 1_100_000
    h = {"Authorization": f"Bearer {cred}", "Idempotency-Key": "k1"}
    assert http.put(url, content=a, headers=h).status_code == 201
    r = http.put(url, content=b, headers=h)                         # same size, different bytes
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # a retry after the agent's access token refreshed is still recognised as the same caller
    tok = lambda: http.post("/v1/auth/agent/token", headers={"Authorization": f"Bearer {cred}"}).json()["access_token"]
    body = {"path": "x.txt", "size_bytes": 1}
    first = http.post(f"/v1/workspaces/{ws_id}/uploads", json=body, headers={"Authorization": f"Bearer {tok()}", "Idempotency-Key": "k2"})
    time.sleep(1.1)                                                 # new token (different exp/signature)
    retry = http.post(f"/v1/workspaces/{ws_id}/uploads", json=body, headers={"Authorization": f"Bearer {tok()}", "Idempotency-Key": "k2"})
    assert retry.headers.get("idempotent-replayed") == "true" and retry.json()["upload_id"] == first.json()["upload_id"]
