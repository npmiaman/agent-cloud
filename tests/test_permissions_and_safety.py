import time

import pytest

from agentcloud import AgentCloudError, Conflict, Forbidden, NotFound, Pending


@pytest.fixture
def setup(server):
    dev = server.signup()
    ws = dev.create_workspace("proj")
    a = dev.create_agent("a")
    b = dev.create_agent("b")
    ta, tb = dev.create_agent_token(a["id"]), dev.create_agent_token(b["id"])
    return dev, ws, a, b, server.client(ta).workspace(ws.id), server.client(tb).workspace(ws.id)


def test_read_only_agent_cannot_write_or_delete(setup):
    dev, ws, a, b, A, B = setup
    ws.grant(a["id"], "", write=True)
    ws.grant(b["id"], "", read=True)
    A.write("data.csv", "x,y\n1,2\n")
    assert B.read_text("data.csv") == "x,y\n1,2\n"
    with pytest.raises(Forbidden):
        B.write("data.csv", "overwritten")
    with pytest.raises(Forbidden):
        B.delete("data.csv")
    with pytest.raises(Forbidden):  # write without delete can't delete either
        A.delete("data.csv")


def test_folder_and_file_scoped_grants(setup):
    dev, ws, a, b, A, B = setup
    ws.write("secret/salaries.csv", "ceo,1\n")
    ws.write("public/readme.md", "hi")
    ws.write("public/notes.md", "n")
    ws.grant(a["id"], "public/", write=True)
    ws.grant(b["id"], "public/readme.md")
    assert A.read_text("public/readme.md") == "hi"
    with pytest.raises(Forbidden):
        A.read("secret/salaries.csv")
    assert sorted(e["path"] for e in A.list(recursive=True) if e["kind"] == "file") == ["public/notes.md", "public/readme.md"]
    assert [e["path"] for e in A.list()] == ["public/"]  # top level only shows the folder it can see into
    with pytest.raises(Forbidden):
        A.write("secret/x.txt", "x")
    assert B.read_text("public/readme.md") == "hi"
    with pytest.raises(Forbidden):
        B.read("public/notes.md")


def test_path_traversal_and_bad_paths_rejected(setup):
    dev, ws, *_ = setup
    # SDK refuses dot segments before they hit the wire.
    for bad in ["../x", "a/../../etc/passwd", "a/./b", ".."]:
        with pytest.raises(ValueError):
            ws.write(bad, "x")
    # Server rejects the rest.
    for bad in ["a//b", "a\\b", "x\x00y", " a"]:
        with pytest.raises(AgentCloudError) as e:
            ws.write(bad, "x")
        assert e.value.status == 400, bad
    # Percent-encoded dot segments survive the client, reach the server decoded, and are rejected.
    for enc in ["a/%2e%2e/%2e%2e/etc", "%2e%2e/x", "a/%2e/b"]:
        r = ws.c.http.put(f"/v1/workspaces/{ws.id}/files/{enc}", content=b"x")
        assert r.status_code == 400, (enc, r.status_code, r.text)
    assert ws.list(recursive=True) == []


def test_other_users_workspace_invisible(server):
    alice = server.signup("alice@example.com")
    bob = server.signup("bob@example.com")
    ws = alice.create_workspace("private")
    ws.write("a.txt", "alice's")
    with pytest.raises(NotFound):
        bob.workspace(ws.id).read("a.txt")
    # Bob's agent can't be granted into Alice's workspace either.
    bob_agent = bob.create_agent("bob-agent")
    with pytest.raises(NotFound):
        ws.grant(bob_agent["id"])


def test_revoked_and_expired_credentials(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    ag = dev.create_agent("x")
    ws.grant(ag["id"])
    ws.write("f.txt", "hi")
    tok = dev.http.post(f"/v1/agents/{ag['id']}/tokens", json={"name": "t"}).json()
    agent = server.client(tok["token"]).workspace(ws.id)
    assert agent.read_text("f.txt") == "hi"
    dev.revoke_agent_token(ag["id"], tok["id"])
    with pytest.raises(AgentCloudError) as e:
        agent.read("f.txt")
    assert e.value.status == 401

    short = dev.http.post(f"/v1/agents/{ag['id']}/tokens", json={"ttl_seconds": 1}).json()["token"]
    sw = server.client(short).workspace(ws.id)
    assert sw.read_text("f.txt") == "hi"
    time.sleep(1.2)
    with pytest.raises(AgentCloudError) as e:
        sw.read("f.txt")
    assert e.value.status == 401


def test_grant_expiry_and_revocation(setup):
    dev, ws, a, b, A, B = setup
    ws.write("f.txt", "hi")
    g = ws.grant(a["id"], ttl_seconds=1)
    assert A.read_text("f.txt") == "hi"
    time.sleep(1.2)
    with pytest.raises(NotFound):
        A.read("f.txt")
    g2 = ws.grant(b["id"])
    assert B.read_text("f.txt") == "hi"
    ws.revoke(g2["id"])
    with pytest.raises(NotFound):
        B.read("f.txt")


def test_agent_cannot_manage_accounts(setup):
    dev, ws, a, b, A, B = setup
    ws.grant(a["id"], write=True, delete=True, share=True)
    for method, url, body in [("POST", "/v1/workspaces", {"name": "x"}), ("POST", "/v1/agents", {"name": "x"}),
                              ("GET", "/v1/agents", None), ("POST", f"/v1/agents/{b['id']}/tokens", {}),
                              ("DELETE", f"/v1/workspaces/{ws.id}", None)]:
        r = A.c.http.request(method, url, json=body)
        assert r.status_code == 403, (method, url, r.text)
    # And can't approve changes.
    ws.grant(b["id"], write=True, approval="all")
    p = B.write("x.txt", "x")
    assert isinstance(p, Pending)
    with pytest.raises(Forbidden):
        A.c.approve(p.proposal_id)


def test_agent_to_agent_sharing_cannot_escalate(setup):
    dev, ws, a, b, A, B = setup
    ws.write("raw/leads.csv", "n\n1\n")
    ws.write("hr/pay.csv", "secret")
    ws.grant(a["id"], "raw/", read=True, write=True, share=True)
    # A shares read-only access to its folder with B.
    g = A.share(b["id"], "raw/", read=True)
    assert B.read_text("raw/leads.csv") == "n\n1\n"
    with pytest.raises(Forbidden):
        B.write("raw/leads.csv", "x")
    # Escalations refused.
    with pytest.raises(Forbidden):
        A.share(b["id"], "hr/", read=True)          # not A's to share
    with pytest.raises(Forbidden):
        A.share(b["id"], "", read=True)             # wider than A's grant
    with pytest.raises(Forbidden):
        A.share(b["id"], "raw/", delete=True)       # A has no delete
    with pytest.raises(Forbidden):
        A.share(b["id"], "raw/", share=True)        # no re-share chains
    # A can revoke what it shared; the owner sees who shared it.
    assert any(x["created_by"] == f"agent:{a['id']}" for x in ws.grants())
    A.revoke(g["id"])
    with pytest.raises(NotFound):
        B.read("raw/leads.csv")


def test_reads_by_shared_agents_are_logged(setup):
    dev, ws, a, b, A, B = setup
    ws.write("doc.md", "x")
    ws.grant(b["id"], "doc.md")
    B.read("doc.md")
    reads = [x for x in ws.activity() if x["action"] == "read"]
    assert reads and reads[0]["actor"] == f"agent:{b['id']}" and reads[0]["path"] == "doc.md"


def test_delete_is_recoverable(setup):
    dev, ws, a, b, A, B = setup
    ws.grant(a["id"], write=True, delete=True)
    A.write("report.md", "v1")
    A.delete("report.md")
    assert not A.exists("report.md")
    with pytest.raises(NotFound):
        A.read_text("report.md")
    assert A.read_text("report.md", version=1) == "v1"
    ws.restore("report.md", 1)
    assert A.read_text("report.md") == "v1"
    assert [v["op"] for v in ws.versions("report.md")] == ["restore", "delete", "create"]


def test_optimistic_concurrency(setup):
    dev, ws, a, b, A, B = setup
    ws.grant(a["id"], write=True)
    ws.grant(b["id"], write=True)
    A.write("shared.json", '{"n":1}')
    # Both read v1, A writes first, B's conditional write must not clobber A's.
    A.write("shared.json", '{"n":2}', if_version=1)
    with pytest.raises(Conflict) as e:
        B.write("shared.json", '{"n":99}', if_version=1)
    assert e.value.body["current_version"] == 2
    with pytest.raises(Conflict):
        B.write("shared.json", "{}", if_version=0)  # create-only on an existing file


def test_approval_policies(setup):
    dev, ws, a, b, A, B = setup
    ws.write("customers.csv", "id,name\n1,Acme\n")
    ws.grant(a["id"], "", write=True, delete=True, approval="overwrite")
    # New files go straight through under 'overwrite'...
    assert A.write("drafts/new.md", "hello").version == 1
    # ...overwrites wait for a human, and the original is untouched meanwhile.
    p = A.write("customers.csv", "id,name\n1,Acme\n2,Globex\n", message="add Globex")
    assert isinstance(p, Pending)
    assert ws.read_text("customers.csv") == "id,name\n1,Acme\n"
    detail = dev.proposal(p.proposal_id)
    assert detail["diff"]["added"] == 1 and detail["diff"]["csv"]["rows_after"] == 2
    res = dev.approve(p.proposal_id, note="looks right")
    assert res["applied"]["version"] == 2
    assert "Globex" in ws.read_text("customers.csv")
    assert ws.versions("customers.csv")[0]["actor"] == f"agent:{a['id']}"  # credited to the agent
    # Deletes need approval too; rejected ones change nothing.
    p2 = A.delete("customers.csv")
    assert isinstance(p2, Pending)
    dev.reject(p2.proposal_id)
    assert ws.exists("customers.csv")
    actions = [x["action"] for x in ws.activity()]
    assert "approve" in actions and "reject" in actions and "propose_write" in actions


def test_folder_grant_with_looser_policy_wins_in_its_folder(setup):
    dev, ws, a, *_ = setup
    ws.write("db.csv", "x")
    ws.grant(a["id"], "", write=True, approval="all")
    ws.grant(a["id"], "scratch/", write=True, approval="none")
    A = setup[4]
    assert isinstance(A.write("db.csv", "y"), Pending)
    assert A.write("scratch/tmp.txt", "y").version == 1


def test_stale_proposal_needs_force(setup):
    dev, ws, a, b, A, B = setup
    ws.write("plan.md", "a")
    ws.grant(a["id"], write=True, approval="all")
    p = A.write("plan.md", "agent edit")
    ws.write("plan.md", "human edit")
    with pytest.raises(Conflict):
        dev.approve(p.proposal_id)
    dev.approve(p.proposal_id, force=True)
    assert ws.read_text("plan.md") == "agent edit"


def test_quota_and_dedup(server):
    dev = server.signup()
    ws = dev.create_workspace("small", quota_bytes=100)
    ws.write("a.bin", b"x" * 60)
    ws.write("copy.bin", b"x" * 60)           # same bytes: deduplicated, free
    ws.restore("a.bin", 1)                     # no-op restore of current
    with pytest.raises(AgentCloudError) as e:
        ws.write("b.bin", b"y" * 60)
    assert e.value.status == 507
    assert ws.refresh()["used_bytes"] == 60


def test_upload_size_limit(tmp_path):
    from conftest import Server
    s = Server(tmp_path / "d", max_upload_bytes=1000)
    dev = s.signup()
    ws = dev.create_workspace("w")
    ws.write("ok.bin", b"x" * 1000)
    with pytest.raises(AgentCloudError) as e:
        ws.write("big.bin", b"x" * 1001)
    assert e.value.status == 413


def test_unchanged_write_makes_no_version(setup):
    dev, ws, *_ = setup
    ws.write("a.txt", "same")
    assert ws.write("a.txt", "same").op == "unchanged"
    assert len(ws.versions("a.txt")) == 1


def test_move_folder_keeps_identity_and_history(setup):
    """Spec Decision 2: paths are for navigation, ids are identity. Moving keeps both."""
    dev, ws, *_ = setup
    ws.write("old/a.txt", "1")
    ws.write("old/a.txt", "2")
    ws.write("old/sub/b.txt", "b")
    fid = ws.stat("old/a.txt")["id"]
    res = ws.move("old/", "new/")
    assert sorted(m["to"] for m in res["moved"] if not m["to"].endswith("/")) == ["new/a.txt", "new/sub/b.txt"]
    assert ws.stat("new/a.txt")["id"] == fid                      # same file
    assert [v["n"] for v in ws.versions("new/a.txt")] == [2, 1]   # history travelled with it
    assert ws.read_text("new/a.txt", version=1) == "1"
    assert not ws.exists("old/a.txt")
    assert [e["path"] for e in ws.list()] == ["new/"]


def test_blobs_encrypted_at_rest(server):
    dev = server.signup()
    ws = dev.create_workspace("w")
    marker = b"TOP-SECRET-PLAINTEXT-MARKER-12345"
    ws.write("s.txt", marker)
    for f in (server.data_dir / "blobs").rglob("*"):
        if f.is_file():
            assert marker not in f.read_bytes()
    assert marker not in (server.data_dir / "agentcloud.db").read_bytes()


def test_served_files_are_sandboxed(setup):
    dev, ws, *_ = setup
    ws.write("x.html", "<script>alert(1)</script>", content_type="text/html")
    r = ws.c.http.get(f"/v1/workspaces/{ws.id}/files/x.html")
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_path_conflicts(setup):
    dev, ws, *_ = setup
    ws.write("a.txt", "x")
    with pytest.raises(Conflict):
        ws.write("a.txt/b.txt", "y")
    ws.mkdir("dir")
    with pytest.raises(Conflict):
        ws.write("dir", "file where folder is")
